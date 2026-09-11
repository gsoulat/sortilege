"""Traitement automatique : detecter, scanner, planifier, appliquer.

C'est la boucle qui transforme Sortilege d'un outil qu'on ouvre en un service
qu'on oublie. Elle enchaine les memes etapes que les boutons — aucun chemin
parallele, donc aucun risque que l'automatique et le manuel divergent.

Trois garde-fous gouvernent son comportement :

1. **Rien ne part avant d'etre stable.** Un fichier doit avoir ete vu deux fois
   avec la meme taille et ne plus avoir bouge depuis un delai. Traiter un
   telechargement en cours deplacerait un fichier incomplet.
2. **Le deplacement reel reste opt-in.** Par defaut la boucle prepare la file
   et s'arrete la. Deplacer des fichiers sans personne devant est un
   engagement plus lourd que de les identifier.
3. **Un cycle ne chevauche jamais le precedent.** Sur une grosse
   bibliotheque, un cycle peut durer plus longtemps que l'intervalle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace

from fastapi import APIRouter

from ..config import get_settings
from ..core.companions import TRASH_DIRNAME
from ..core.journal import ApplyResult, apply_plan
from ..core.notify import cycle_notification, failure_notification, send
from ..core.pipeline import BATCH_SIZE, Pipeline
from ..core.planner import Plan
from ..core.scanner import scan
from ..core.scoring import Decision, Policy
from ..core.vpn import egress_allowed
from ..core.watch import Watcher
from ..providers.anilist import AniListProvider
from ..providers.tmdb import TMDBProvider
from . import library, review
from .deps import get_journal, get_memory, get_store, scan_rules, tmdb_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automation", tags=["automatisation"])

MESSAGE_SORTIE_REFUSEE = (
    "Cycle arrêté : la sortie par le tunnel n'est pas confirmée (politique « exiger »)."
)
"""Ce que dit un cycle arrete par la politique de sortie. Phrase FIXE.

La raison detaillee du refus cite l'adresse publique mesuree, et le message
d'un cycle voyage : historique, ecran, notifications. Elle va donc au log
local, et le rapport ne garde que ce que n'importe qui peut lire."""

TYPES_SOUS_TITRES = ("movie", "episode", "anime")
"""Les plans dont on cherche les sous-titres : les videos, pas les livres."""


@dataclass
class CycleReport:
    at: float = 0.0
    detected: int = 0
    scanned: int = 0
    planned: int = 0
    applied: int = 0
    queued: int = 0
    remaining: int = 0
    """Fichiers eligibles laisses au cycle suivant. Le dire evite de croire
    qu'un cycle « termine » a tout traite."""

    message: str = ""

    failed: bool = False
    """Le cycle s'est arrete sur un refus, pas sur une exception.

    Distinct de ``_state.error``, qui ne couvre que les pannes imprevues. Un
    refus de sortie reseau est une decision, pas un bug : il ne produit ni
    trace ni exception, et sans ce drapeau il passerait pour un cycle normal
    qui n'avait rien a faire — precisement l'etat muet qu'on chasse."""

    egress_refused: bool = False
    """Le refus vient de la politique de sortie reseau.

    Un tel refus ne produit AUCUNE notification : prevenir Discord que la
    sortie n'est pas protegee, c'est emettre precisement par cette sortie. Il
    reste lisible dans l'historique des cycles."""


@dataclass
class AutomationState:
    running: bool = False
    last_run: float = 0.0
    next_run: float = 0.0
    last: CycleReport | None = None
    history: list[CycleReport] = field(default_factory=list)
    error: str | None = None


_state = AutomationState()
_watcher = Watcher()
_task: asyncio.Task | None = None
_lock = asyncio.Lock()


async def run_cycle(*, forced: bool = False) -> CycleReport:
    """Un tour complet. Ne leve jamais : la boucle doit survivre a un echec.

    ``forced`` ignore la detection et traite tout ce qui est present — c'est le
    bouton « Lancer maintenant », qui doit faire quelque chose meme quand rien
    de neuf n'est arrive.
    """
    report = CycleReport(at=time.time())
    conf = get_settings()
    store = get_store()
    prefs = store.load()
    auto = prefs.automation

    if not _lock.locked():
        pass  # informatif : le verrou est pris par l'appelant

    roots = store.resolved_sources()
    if not roots:
        report.message = "aucune source sélectionnée"
        return report

    # Les memes regles pour la surveillance et pour le scan : sans cela, un
    # fichier exclu reveillerait un cycle a chaque intervalle pour etre ecarte
    # juste apres — une boucle de travail nul, indefiniment.
    regles = scan_rules()

    _watcher.quiet_seconds = auto.quiet_seconds
    fresh = _watcher.poll(roots, rules=regles)
    report.detected = len(fresh)

    if not fresh and not forced:
        report.message = "rien de nouveau"
        return report

    # --- Scan ---
    result = scan(roots, deep=True, library_root=conf.library_root, rules=regles)
    library.adopt_scan(result, deep=True)
    report.scanned = result.total

    if not result.files:
        report.message = "aucun fichier à traiter"
        return report

    cle = tmdb_key()
    if not cle:
        report.message = "aucune clé TheMovieDB : identification impossible"
        return report

    # Meme garde-fou que le traitement manuel, et il compte DAVANTAGE ici :
    # personne ne regarde. Un cycle qui interroge les fournisseurs en clair
    # toutes les quinze minutes pendant qu'on croit le tunnel monte revele bien
    # plus qu'un lot lance a la main.
    sortie = await egress_allowed(prefs.vpn.policy, reference_ip=prefs.vpn.reference_ip or None)
    if not sortie.allowed:
        # La raison detaillee cite l'adresse publique mesuree : elle va au log
        # local, jamais dans le rapport, qui s'affiche et peut voyager.
        logger.warning("cycle automatique arrete, sortie refusee : %s", sortie.reason)
        report.message = MESSAGE_SORTIE_REFUSEE
        report.failed = True
        report.egress_refused = True
        return report
    if sortie.warn:
        logger.warning("cycle automatique, sortie non confirmee : %s", sortie.reason)

    # --- Plan ---
    pipeline = Pipeline(
        tmdb=TMDBProvider(cle, prefs.metadata.language),
        anilist=AniListProvider(),
        library_root=conf.library_root,
        templates={k: prefs.template_for(k) for k in ("movie", "episode", "anime")},
        destination_for=store.destination_root,
        policy=Policy(
            auto_apply_threshold=conf.auto_apply_threshold,
            reject_threshold=conf.reject_threshold,
        ),
        ai=review._ai_resolver(),
        ai_threshold=prefs.ai.threshold,
        memory=get_memory(),
        known_titles=review._library_titles(),
    )
    # PAR LOT, comme le traitement manuel. Sans cette borne, un premier cycle
    # sur une bibliotheque constituee planifiait plusieurs milliers de fichiers
    # d'un seul tenant : des heures d'appels au fournisseur, pendant lesquelles
    # rien n'est applicable. Un lot par tour grignote l'arriere sans bloquer.
    deja = review.planned_paths()
    eligible = [
        f
        for f in result.files
        if not f.in_library and f.skipped_reason is None and str(f.path) not in deja
    ]
    lot = eligible[:BATCH_SIZE]
    report.remaining = max(0, len(eligible) - len(lot))

    if not lot:
        report.message = "rien de nouveau à identifier"
        return report

    try:
        plans = await pipeline.plan_all(lot)
    finally:
        await pipeline.aclose()

    # On AJOUTE au lieu de remplacer : le cycle effacait la file entiere a
    # chaque tour, faisant disparaitre sans un mot tout ce qui attendait un
    # arbitrage humain.
    review.merge_plans(plans, [f.path for f in lot])
    report.planned = len(plans)

    confident = [p for p in plans if p.decision is Decision.AUTO]
    report.queued = sum(1 for p in plans if p.decision is Decision.REVIEW)

    # --- Application ---
    if not auto.apply_auto:
        report.message = (
            f"{len(confident)} plan(s) prêt(s), {report.queued} en attente d'arbitrage. "
            "Application automatique désactivée."
        )
        return report

    # La planification a pu durer plus longtemps que le cache de la mesure :
    # la decision prise avant elle ne vaut plus au moment d'aller chercher des
    # affiches. On la reprend donc ici. Deplacer un fichier reste local et
    # continue ; seul ce qui irait chercher quelque chose dehors s'arrete.
    reseau_ok = True
    if confident:
        reglage_vpn = store.load().vpn
        avant = await egress_allowed(
            reglage_vpn.policy, reference_ip=reglage_vpn.reference_ip or None
        )
        if not avant.allowed:
            reseau_ok = False
            logger.warning(
                "sortie refusee avant l'application, ni affiches ni sous-titres : %s",
                avant.reason,
            )
    fiches = prefs.local_metadata if reseau_ok else replace(prefs.local_metadata, artwork=False)

    journal = get_journal()
    trash_root = conf.library_root / TRASH_DIRNAME
    results = [
        apply_plan(
            p,
            journal,
            dry_run=False,
            trash_root=trash_root,
            # Borne le nettoyage des dossiers vides : on ne remonte jamais
            # jusqu'a une source, sans quoi ranger le dernier fichier la
            # ferait disparaitre.
            source_roots=roots,
            local_metadata=fiches,
        )
        for p in confident
    ]
    report.applied = sum(1 for r in results if r.ok)

    review.drop_applied([r.plan_id for r in results if r.ok])
    _watcher.mark_processed([p.source for p in plans if p.decision is not Decision.AUTO])

    failed = len(results) - report.applied
    report.message = f"{report.applied} rangé(s), {report.queued} en attente"
    if report.remaining:
        report.message += f", {report.remaining} pour le prochain tour"
    if failed:
        report.message += f", {failed} en échec"

    if report.applied:
        if reseau_ok:
            report.message += await _sous_titres(confident, results)
        else:
            report.message += (
                ", affiches et sous-titres non récupérés (sortie par le tunnel non confirmée)"
            )
        # Sans garde de sortie : le serveur multimedia est sur le reseau local,
        # rien ne quitte la maison. Apres les sous-titres, pour qu'il les
        # decouvre en meme temps que les fichiers.
        await review._tell_media_server()
    return report


async def _sous_titres(plans: list[Plan], results: list[ApplyResult]) -> str:
    """Cherche les sous-titres des fichiers ranges ; rend la fin du message.

    Meme contrat que le traitement manuel : APRES le deplacement, pour que le
    sous-titre se depose a cote du fichier, et avec le titre du plan plutot que
    le nom de fichier, qu'on vient justement de corriger. La recherche ne leve
    jamais et porte sa propre garde de sortie.
    """
    ranges = {r.plan_id for r in results if r.ok}
    demandes = [
        (p.destination, p.title, p.year, p.values.get("season"), p.values.get("episode"))
        for p in plans
        if p.id in ranges and p.kind in TYPES_SOUS_TITRES and p.destination is not None
    ]
    if not demandes:
        return ""
    rapport = await review._recuperer_sous_titres(demandes)
    if rapport.get("refused"):
        logger.warning("sous-titres non recherches : %s", rapport["refused"])
        return ", sous-titres non recherchés (sortie par le tunnel non confirmée)"
    if rapport.get("unavailable"):
        logger.warning("sous-titres indisponibles : %s", rapport["unavailable"])
        return f", sous-titres indisponibles ({rapport['unavailable']})"
    ecrits = int(rapport.get("written") or 0)
    return f", {ecrits} sous-titre(s) déposé(s)" if ecrits else ""


async def _notify(notification) -> None:
    """Envoie une notification si le canal est configure. Ne leve jamais.

    Le garde-fou est ici plutot que chez l'appelant : une notification est un
    a-cote, et aucun point d'appel ne doit avoir a s'en proteger.

    La politique de sortie vaut aussi pour Discord : un message part par la
    meme route que le reste, et sous « exiger » il n'a pas plus que les autres
    le droit de sortir en clair. Verifiee en dernier, pour ne rien mesurer
    quand le canal est coupe. Une politique illisible leve, et l'exception
    est avalee plus bas : le doute ferme le canal au lieu de l'ouvrir.
    """
    if notification is None:
        return
    reglages = get_store().load()
    prefs = reglages.notifications
    if not prefs.enabled or not prefs.webhook_url:
        return
    if notification.level == "error" and not prefs.on_failure:
        return
    try:
        sortie = await egress_allowed(
            reglages.vpn.policy, reference_ip=reglages.vpn.reference_ip or None
        )
        if not sortie.allowed:
            logger.warning("notification non envoyee, sortie refusee : %s", sortie.reason)
            return
        await send(prefs.webhook_url, notification)
    except Exception:
        logger.exception("envoi de la notification impossible")


async def _tour() -> CycleReport | None:
    """Un cycle programme, et ce qu'on en dit. A appeler sous ``_lock``.

    Separe de la boucle pour etre verifiable : la boucle attend l'horloge, ce
    tour contient toute la decision de notifier ou non.
    """
    _state.running = True
    try:
        report = await run_cycle()
        _state.last = report
        _state.error = None
        _state.history = [report, *_state.history][:20]
        logger.info("cycle automatique : %s", report.message)
        if report.egress_refused:
            # Aucun message : prevenir Discord que la sortie n'est pas
            # protegee, c'est emettre par cette sortie. Le refus reste lisible
            # dans l'historique des cycles.
            logger.info("refus de sortie : aucune notification envoyee")
        elif report.failed:
            # Le canal d'echec, et non celui du cycle : un cycle refuse
            # n'a rien range, donc n'aurait rien dit du tout.
            await _notify(failure_notification(report.message))
        else:
            await _notify(cycle_notification(report))
        return report
    except Exception as exc:
        _state.error = f"{type(exc).__name__}: {exc}"
        logger.exception("le cycle automatique a echoue")
        await _notify(failure_notification(_state.error))
        return None
    finally:
        _state.running = False
        _state.last_run = time.time()


async def _loop() -> None:
    """Boucle de fond. Relit les preferences a chaque tour.

    Relire plutot que capturer : activer la surveillance ou changer
    l'intervalle depuis l'interface doit prendre effet sans redemarrage.
    """
    while True:
        auto = get_store().load().automation
        interval = max(1, auto.interval_minutes) * 60
        _state.next_run = time.time() + interval
        await asyncio.sleep(interval)

        if not get_store().load().automation.enabled:
            continue

        if _lock.locked():
            # Le cycle precedent dure encore : on saute ce tour plutot que de
            # lancer deux scans concurrents sur le meme disque.
            logger.info("cycle precedent encore en cours, tour ignore")
            continue

        async with _lock:
            await _tour()


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="sortilege-automation")


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        _task = None


def _report_out(report: CycleReport | None) -> dict[str, object] | None:
    if report is None:
        return None
    return {
        "at": report.at,
        "detected": report.detected,
        "scanned": report.scanned,
        "planned": report.planned,
        "applied": report.applied,
        "remaining": report.remaining,
        "queued": report.queued,
        "message": report.message,
        "failed": report.failed,
    }


@router.get("")
def status() -> dict[str, object]:
    auto = get_store().load().automation
    return {
        "enabled": auto.enabled,
        "interval_minutes": auto.interval_minutes,
        "quiet_seconds": auto.quiet_seconds,
        "apply_auto": auto.apply_auto,
        "running": _state.running,
        "last_run": _state.last_run or None,
        "next_run": _state.next_run or None,
        "error": _state.error,
        "last": _report_out(_state.last),
        "history": [_report_out(r) for r in _state.history],
    }


@router.post("/run")
async def run_now() -> dict[str, object]:
    """Lance un cycle immediatement, sans attendre l'horloge."""
    if _lock.locked():
        return {"started": False, "detail": "Un cycle est deja en cours.", **status()}

    async with _lock:
        _state.running = True
        try:
            report = await run_cycle(forced=True)
            _state.last = report
            _state.error = None
            _state.history = [report, *_state.history][:20]
        except Exception as exc:
            _state.error = f"{type(exc).__name__}: {exc}"
            logger.exception("le cycle manuel a echoue")
        finally:
            _state.running = False
            _state.last_run = time.time()

    return {"started": True, **status()}
