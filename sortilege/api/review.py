"""File de revue : planifier, arbitrer, appliquer, annuler.

Les plans sont gardes en memoire pour l'instant, comme le dernier scan. C'est
assume et temporaire : rien de precieux ne s'y trouve, puisque le seul etat
durable — ce qui a REELLEMENT ete deplace — vit dans le journal sur disque.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from threading import Lock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..core.companions import TRASH_DIRNAME, trash_root_for
from ..core.journal import (
    apply_plan,
    evacuate_ranged_source,
    group_by_work,
    undo_last,
    undo_plans,
    undo_work,
)
from ..core.mediaserver import refresh_library
from ..core.pipeline import BATCH_SIZE, Pipeline
from ..core.planner import Plan
from ..core.renaming import rename_plans
from ..core.renaming import summarize as rename_summary
from ..core.scanner import scan
from ..core.scoring import Decision, Policy
from ..core.snapshot import PLANS_KEY, SnapshotError, plans_in, plans_out
from ..core.store import Decision as RememberedDecision
from ..core.store import title_key
from ..providers.anilist import AniListProvider
from ..providers.tmdb import TMDBProvider
from .deps import get_journal, get_memory, get_store
from .library import last_scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["revue"])

_plans: dict[str, Plan] = {}
_lock = Lock()

# Reference conservee sur la tache de fond : asyncio ne garde qu'une reference
# FAIBLE vers les taches en cours. Sans cette variable, le ramasse-miettes
# peut supprimer le calcul en plein vol, sans erreur ni trace.
_plan_task: asyncio.Task | None = None


class ApplyRequest(BaseModel):
    plan_ids: list[str] | None = None
    """Plans a appliquer. Absent = tous ceux marques AUTO."""

    include_review: bool = False
    """Applique aussi les plans en attente d'arbitrage. Reserve a un clic
    explicite sur une selection : c'est exactement ce que l'outil s'interdit
    de faire seul."""

    dry_run: bool = True
    """Simuler plutot que deplacer.

    Par defaut a True : une requete qui omettrait ce champ simule, elle ne
    deplace pas. Le defaut d'une operation irreversible doit etre l'inaction —
    c'est la seule protection dont on ait besoin, et elle ne peut pas se
    desynchroniser de l'interface comme le faisait une variable
    d'environnement."""


class UndoRequest(BaseModel):
    count: int = 1
    """Nombre d'operations a defaire, de la plus recente. Ignore si un critere
    plus precis est fourni."""

    work: str | None = None
    """Cle d'oeuvre : annule TOUT ce qui concerne cette serie ou ce film.

    C'est le cas d'usage reel — une serie mal identifiee au milieu de sept
    cents deplacements corrects — que « tout annuler » ne couvrait pas."""

    plan_ids: list[str] | None = None
    """Fichiers precis. Un plan porte la video et ses compagnons : annuler un
    episode remet aussi son sous-titre en place."""


def _library_titles() -> set[str]:
    """Titres des oeuvres deja rangees.

    Ils servent de voisins a la deduction de franchise : sans eux, importer
    « Star Trek: Picard » seul ne le rangerait pas avec les Star Trek deja en
    bibliotheque. Un index absent n'est pas une erreur — on retombe alors sur
    les seuls titres du lot en cours.
    """
    from . import collection

    return {w.title for w in collection.current_works() if w.title}


def _ai_resolver():
    """Construit le resolveur choisi dans les reglages, ou None.

    Renvoyer None plutot que lever : un resolveur mal configure doit degrader
    vers la revue manuelle, pas empecher un calcul de plans.
    """
    from ..core.ai import build_resolver

    ai = get_store().load().ai
    if not ai.enabled:
        return None
    return build_resolver(ai.provider, ai.api_key, ai.model, ai.base_url)


def _persist_plans() -> None:
    """Ecrit la file de plans sur disque. Ne leve jamais.

    Chaque plan represente des appels reseau deja payes. Sur mille fichiers,
    les refaire apres un redemarrage coute plusieurs minutes et une part du
    quota TMDB — pour un resultat identique.

    Ce qui est enregistre reste une VUE : un plan relu est verifie a
    l'application comme n'importe quel plan frais. La reprise economise du
    calcul, elle ne court-circuite aucun controle.
    """
    try:
        with _lock:
            payload = plans_out(list(_plans.values()))
            done = sorted(_job.done_paths)
        get_memory().save_blob(PLANS_KEY, {**payload, "done_paths": done})
    except Exception:
        logger.exception("enregistrement des plans impossible")


def restore_plans() -> bool:
    """Relit la file enregistree. Vrai si quelque chose a ete repris."""
    raw = get_memory().load_blob(PLANS_KEY)
    if raw is None:
        return False
    try:
        plans = plans_in(raw)
    except SnapshotError as exc:
        logger.warning("plans enregistres ignores : %s", exc)
        return False

    with _lock:
        _plans.clear()
        _plans.update({p.id: p for p in plans})
        # Sans les chemins deja traites, « Traiter les 100 suivants »
        # recalculerait le premier lot au lieu d'avancer.
        _job.done_paths = set(raw.get("done_paths") or [])
    logger.info("file reprise depuis le disque : %s plans", len(plans))
    return True


def current_plans() -> list[Plan]:
    """Instantane de la file. Une copie, pas la structure vivante.

    Le calcul tourne en tache de fond et publie au fil de l'eau : rendre le
    dictionnaire lui-meme exposerait l'appelant a une modification en cours
    d'iteration.
    """
    with _lock:
        return list(_plans.values())


def adopt_plans(plans: list[Plan]) -> None:
    """Remplace la file par un lot calcule ailleurs (cycle automatique)."""
    with _lock:
        _plans.clear()
        _plans.update({p.id: p for p in plans})
    _persist_plans()


def drop_applied(plan_ids: list[str]) -> None:
    with _lock:
        for pid in plan_ids:
            _plans.pop(pid, None)
    _persist_plans()


def _plan_out(plan: Plan) -> dict[str, object]:
    return {
        "id": plan.id,
        "source": str(plan.source),
        "filename": plan.source.name,
        "destination": str(plan.destination) if plan.destination else None,
        "kind": plan.kind,
        "score": round(plan.score, 3),
        "decision": str(plan.decision),
        "reasons": plan.reasons,
        "title": plan.title,
        "year": plan.year,
        "provider": plan.provider,
        "external_id": plan.external_id,
        "poster_url": plan.poster_url,
        "error": plan.error,
        "is_noop": plan.is_noop,
        "manual": plan.manual,
        "alternatives": [
            {
                "provider": c.provider,
                "external_id": c.external_id,
                "title": c.title,
                "original_title": c.original_title,
                "year": c.year,
                "poster_url": c.poster_url,
                "overview": c.overview,
            }
            for c in plan.alternatives
        ],
    }


@dataclass
class PlanJob:
    """Avancement du calcul des plans.

    Meme raison que pour le scan : sur 426 fichiers l'operation dure des
    minutes — recherches TMDB, titres d'episodes, eventuelle passe IA — et un
    bouton fige ne dit pas si l'outil travaille ou s'il est bloque.
    """

    running: bool = False
    processed: int = 0
    total: int = 0
    current: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str | None = None

    done_paths: set[str] = field(default_factory=set)
    """Fichiers deja planifies, pour ne pas les repasser aux fournisseurs.

    Par chemin et non par position : appliquer des plans retire des fichiers du
    scan, et un simple compteur designerait ensuite les mauvais."""

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return (self.finished_at or time.monotonic()) - self.started_at

    @property
    def eta_seconds(self) -> float | None:
        if not self.running or self.processed <= 0 or self.total <= 0:
            return None
        return max(0.0, (self.elapsed / self.processed) * (self.total - self.processed))


_job = PlanJob()


def _plan_status() -> dict[str, object]:
    return {
        "running": _job.running,
        "processed": _job.processed,
        "total": _job.total,
        "current": _job.current,
        "elapsed": round(_job.elapsed, 1),
        "eta": round(_job.eta_seconds, 1) if _job.eta_seconds is not None else None,
        "error": _job.error,
        "batch_size": BATCH_SIZE,
    }


def plan_status() -> dict[str, object]:
    """Etat du calcul, pour la vue unifiee. Appelee dans le processus."""
    return _plan_status()


@router.post("/plan")
async def build_plans(limit: int = 100, reset: bool = False) -> dict[str, object]:
    """Demarre le calcul des plans et rend la main immediatement.

    Par LOT : sur un millier de fichiers, tout planifier d'un coup produit une
    file que personne ne relira, et fait attendre de longues minutes avant le
    premier resultat exploitable. Un lot de cent se traite, puis on demande le
    suivant.

    Le calcul tourne en tache de fond : meme un lot peut depasser le delai
    d'attente d'un navigateur, et le resultat serait perdu alors que le serveur
    a fini son travail.
    """
    conf = get_settings()
    scan = last_scan()

    if scan is None or not scan.files:
        raise HTTPException(
            status_code=400,
            detail="Aucun scan disponible. Lance d'abord un scan depuis la bibliothèque.",
        )
    if not conf.tmdb_api_key:
        raise HTTPException(
            status_code=400,
            detail="Aucune clé TheMovieDB : sans fournisseur, il n'y a aucun candidat à comparer.",
        )

    store = get_store()
    prefs = store.load()

    pipeline = Pipeline(
        tmdb=TMDBProvider(conf.tmdb_api_key),
        anilist=AniListProvider(),
        library_root=conf.library_root,
        templates={k: prefs.template_for(k) for k in ("movie", "episode", "anime")},
        destination_for=store.destination_root,
        policy=Policy(
            auto_apply_threshold=conf.auto_apply_threshold,
            reject_threshold=conf.reject_threshold,
        ),
        ai=_ai_resolver(),
        ai_batch_size=prefs.ai.batch_size,
        ai_threshold=prefs.ai.threshold,
        memory=get_memory(),
        known_titles=_library_titles(),
    )

    if _job.running:
        # Ni erreur ni second calcul : on renvoie l'etat en cours. Cliquer deux
        # fois doit montrer la progression, pas afficher un refus.
        return {**_queue(), "started": False}

    # Seuls les fichiers eligibles comptent : ceux deja ranges et les
    # echantillons ecartes ne seront jamais planifies, les inclure dans le
    # « reste a traiter » annoncerait un travail qui n'arrivera pas.
    eligible = [f for f in scan.files if not f.in_library and f.skipped_reason is None]
    pending = [f for f in eligible if str(f.path) not in _job.done_paths]

    if reset:
        _job.done_paths.clear()
        with _lock:
            _plans.clear()
        _persist_plans()
        pending = eligible

    if not pending:
        return {**_queue(), "started": False, "detail": "Tous les fichiers ont ete planifies."}

    batch = pending[: max(1, limit)]

    def progress(processed: int, total: int, name: str) -> None:
        _job.processed = processed
        _job.total = total
        _job.current = name

    _job.running = True
    _job.processed = 0
    _job.total = 0
    _job.current = ""
    _job.error = None
    _job.started_at = time.monotonic()
    _job.finished_at = 0.0

    def publish(plan: Plan) -> None:
        """Rend un plan visible des qu'il est pret.

        Sans cela, mille fichiers signifiaient plusieurs minutes d'ecran vide
        alors que les premiers resultats etaient exploitables tout de suite.
        L'identifiant etant derive de la source, republier un plan ameliore le
        remplace au lieu de le dupliquer.
        """
        with _lock:
            _plans[plan.id] = plan

    async def work() -> None:
        try:
            plans = await pipeline.plan_all(batch, on_progress=progress, on_plan=publish)
        except Exception as exc:
            # Une tache de fond qui leve mourrait en silence : sans cette
            # capture, l'interface attendrait indefiniment un calcul disparu.
            _job.error = f"{type(exc).__name__}: {exc}"
            logger.exception("le calcul des plans a echoue")
            return
        finally:
            await pipeline.aclose()
            _job.running = False
            _job.finished_at = time.monotonic()
            _job.current = ""

        with _lock:
            # On AJOUTE : les lots precedents restent a arbitrer, les effacer
            # perdrait le travail deja fait.
            _plans.update({p.id: p for p in plans})
        _job.done_paths.update(str(f.path) for f in batch)
        _persist_plans()
        logger.info("lot planifie : %s plans", len(plans))

    global _plan_task
    _plan_task = asyncio.create_task(work(), name="sortilege-plan")
    return {**_queue(), "started": True}


def _queue() -> dict[str, object]:
    conf = get_settings()
    plans = list(_plans.values())

    by_decision = {d: [p for p in plans if p.decision is d] for d in Decision}

    return {
        "ready": bool(plans),
        "blockers": _blockers(),
        "counts": {str(d): len(v) for d, v in by_decision.items()},
        "auto": [_plan_out(p) for p in by_decision[Decision.AUTO]],
        "items": [_plan_out(p) for p in by_decision[Decision.REVIEW]],
        "rejected": [_plan_out(p) for p in by_decision[Decision.REJECT]],
        "policy": {
            "auto_apply_threshold": conf.auto_apply_threshold,
            "reject_threshold": conf.reject_threshold,
        },
        "journal_size": len(get_journal().read_all()),
        "remaining": _remaining(),
        "planned": len(_job.done_paths),
    }


def _remaining() -> int:
    """Fichiers eligibles pas encore planifies."""
    scan = last_scan()
    if scan is None:
        return 0
    return sum(
        1
        for f in scan.files
        if not f.in_library and f.skipped_reason is None and str(f.path) not in _job.done_paths
    )


def _blockers() -> list[dict[str, str]]:
    conf = get_settings()
    blockers: list[dict[str, str]] = []

    if not conf.tmdb_api_key:
        blockers.append(
            {
                "title": "Aucune clé TheMovieDB",
                "detail": "Sans fournisseur de métadonnées, il n'y a aucun candidat "
                "à comparer, donc rien à scorer.",
                "where": "TMDB_API_KEY dans le .env",
            }
        )

    if last_scan() is None:
        blockers.append(
            {
                "title": "Aucun scan effectué",
                "detail": "La file se remplit à partir des fichiers trouvés par un scan.",
                "where": "Onglet Bibliothèque → Lancer un scan",
            }
        )

    return blockers


async def _tell_media_server() -> None:
    """Demande au serveur multimedia de relire sa bibliotheque. Ne leve jamais.

    Le fichier est deja a sa place : ne pas avoir prevenu est un desagrement,
    pas une perte. Le rangement ne doit donc rien risquer sur cet appel.
    """
    prefs = get_store().load().media_server
    if not prefs.enabled or not prefs.base_url or not prefs.api_key:
        return
    try:
        await refresh_library(prefs.base_url, prefs.api_key)
    except Exception:
        logger.exception("rafraichissement du serveur multimedia impossible")


@router.post("/apply")
async def apply(body: ApplyRequest) -> dict[str, object]:
    """Applique des plans. En mode simulation, verifie sans rien deplacer."""
    conf = get_settings()
    journal = get_journal()

    with _lock:
        if body.plan_ids is not None:
            selected = [_plans[pid] for pid in body.plan_ids if pid in _plans]
        else:
            allowed = {Decision.AUTO}
            if body.include_review:
                allowed.add(Decision.REVIEW)
            selected = [p for p in _plans.values() if p.decision in allowed]

    # La corbeille vit sous la racine de bibliotheque : elle doit etre sur le
    # meme volume que les fichiers evacues, sinon chaque reste serait recopie
    # au lieu d'etre deplace.
    trash_root = conf.library_root / TRASH_DIRNAME

    simulate = body.dry_run

    results = [apply_plan(p, journal, dry_run=simulate, trash_root=trash_root) for p in selected]

    # Un plan applique quitte la file : le laisser inviterait a le rejouer, et
    # sa source n'existe plus.
    if not simulate:
        with _lock:
            for r in results:
                if r.ok:
                    _plans.pop(r.plan_id, None)
        _persist_plans()
        if any(r.ok for r in results):
            await _tell_media_server()

    return {
        "dry_run": simulate,
        "applied": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [
            {
                "plan_id": r.plan_id,
                "ok": r.ok,
                "source": r.source,
                "destination": r.destination,
                "message": r.message,
                "simulated": r.simulated,
                "reason": r.reason,
            }
            for r in results
        ],
    }


class EvacuateRequest(BaseModel):
    plan_ids: list[str] | None = None
    """Plans concernes. Absent = tous ceux dont la destination est deja
    occupee par un fichier identique."""


@dataclass
class EvacuateJob:
    """Avancement de la mise en corbeille.

    Meme raison que pour le scan et le calcul : trois cents deplacements durent
    assez longtemps pour qu'un bouton fige ne dise plus rien — ni si l'outil
    travaille, ni s'il est bloque. Et une requete synchrone aussi longue
    finirait par expirer cote navigateur, perdant un resultat que le serveur a
    pourtant produit.
    """

    running: bool = False
    processed: int = 0
    total: int = 0
    current: str = ""
    evacuated: int = 0
    failed: int = 0
    error: str | None = None
    results: list[dict] = field(default_factory=list)


_evac_job = EvacuateJob()
_evac_task: asyncio.Task | None = None


@router.get("/evacuate/status")
def evacuate_status() -> dict[str, object]:
    return {
        "running": _evac_job.running,
        "processed": _evac_job.processed,
        "total": _evac_job.total,
        "current": _evac_job.current,
        "evacuated": _evac_job.evacuated,
        "failed": _evac_job.failed,
        "error": _evac_job.error,
        "results": [r for r in _evac_job.results if not r["ok"]][:200],
    }


@router.post("/evacuate")
async def evacuate(body: EvacuateRequest) -> dict[str, object]:
    """Met en corbeille les sources dont le fichier est deja range.

    Apres un rangement rejoue, une copie subsiste dans les telechargements :
    elle occupe la place et revient a chaque scan. Elle part en CORBEILLE, pas
    a la poubelle — l'operation est journalisee, donc annulable, et une source
    de taille differente est refusee plutot que confondue avec un doublon.

    Le travail part en tache de fond et l'avancement se suit : sur trois cents
    fichiers l'operation dure, et rendre la main tout de suite evite qu'une
    requete expire alors que le serveur, lui, a fini.
    """
    if _evac_job.running:
        return {"started": False, **evacuate_status()}

    conf = get_settings()
    journal = get_journal()

    with _lock:
        if body.plan_ids is not None:
            selected = [_plans[pid] for pid in body.plan_ids if pid in _plans]
        else:
            selected = [
                p
                for p in _plans.values()
                if p.destination is not None and p.destination.exists() and p.source.is_file()
            ]

    _evac_job.running = True
    _evac_job.processed = 0
    _evac_job.total = len(selected)
    _evac_job.evacuated = 0
    _evac_job.failed = 0
    _evac_job.current = ""
    _evac_job.error = None
    _evac_job.results = []

    def work() -> None:
        """Les deplacements sont bloquants : ils tournent dans un thread pour
        ne pas figer la boucle d'evenements pendant plusieurs minutes."""
        try:
            for plan in selected:
                _evac_job.current = plan.source.name
                # Corbeille choisie PAR FICHIER : sur un NAS ou telechargements
                # et bibliotheque sont deux partages, une corbeille commune
                # imposerait de recopier chaque fichier au lieu de le renommer.
                result = evacuate_ranged_source(
                    plan,
                    journal,
                    trash_root_for(plan.source, conf.library_root, conf.source_roots),
                )
                _evac_job.processed += 1
                if result.ok:
                    _evac_job.evacuated += 1
                    # Un plan dont la source est partie n'a plus lieu d'etre :
                    # le garder le ferait reproposer a chaque affichage, avec
                    # le meme echec.
                    with _lock:
                        _plans.pop(result.plan_id, None)
                else:
                    _evac_job.failed += 1
                _evac_job.results.append(
                    {
                        "plan_id": result.plan_id,
                        "ok": result.ok,
                        "source": result.source,
                        "destination": result.destination,
                        "message": result.message,
                        "reason": result.reason,
                    }
                )
        except Exception as exc:
            _evac_job.error = f"{type(exc).__name__}: {exc}"
            logger.exception("l'evacuation a echoue")
        finally:
            _evac_job.running = False
            _evac_job.current = ""
            _persist_plans()

    global _evac_task
    _evac_task = asyncio.create_task(asyncio.to_thread(work), name="sortilege-evacuate")
    return {"started": True, **evacuate_status()}


@router.post("/rename-library")
async def rename_library() -> dict[str, object]:
    """Propose de remettre la bibliotheque en conformite avec le gabarit.

    Ne deplace RIEN : les plans rejoignent la file d'arbitrage, ou ils se
    valident comme les autres. Un renommage de masse sur une bibliotheque
    constituee est l'operation la plus risquee de l'application ; elle ne doit
    pas se declencher d'un seul clic.

    Aucune identification n'est refaite : on repart de ce que le fichier dit
    deja de lui-meme. Reinterroger un fournisseur ferait courir le risque
    qu'une mauvaise reponse renomme des fichiers corrects.
    """
    conf = get_settings()
    store = get_store()
    prefs = store.load()

    result = scan([conf.library_root], deep=False, library_root=conf.library_root)
    plans: list[Plan] = []
    for kind in ("movie", "episode", "anime"):
        subset = [f for f in result.files if _kind_of(f) == kind]
        plans.extend(
            rename_plans(
                subset,
                template=prefs.template_for(kind),
                destination_root=store.destination_root(kind),
            )
        )

    with _lock:
        for plan in plans:
            _plans[plan.id] = plan
    _persist_plans()

    return {"proposed": len(plans), **rename_summary(plans), **_queue()}


def _kind_of(scanned) -> str:
    from ..core.parser import MediaKind

    return {MediaKind.MOVIE: "movie", MediaKind.ANIME: "anime"}.get(scanned.parsed.kind, "episode")


class ConfirmRequest(BaseModel):
    whole_series: bool = False
    """Confirme aussi les autres episodes de la meme oeuvre.

    Faux par defaut, contrairement a « Ce n'est pas ca » : elargir la portee en
    silence est precisement le defaut qu'on cherche a eviter — on ne saurait
    pas ce qu'on vient de valider. Qui veut toute une serie le demande, et
    l'interface l'ecrit sur son bouton."""

    plan_ids: list[str] | None = None
    """Fichiers a confirmer, quand l'appelant les connait exactement.

    Prime sur ``whole_series`` : deduire l'ensemble d'un titre echoue des qu'une
    meme oeuvre a ete lue sous deux orthographes, alors que l'interface, elle,
    sait precisement ce qu'elle affiche."""


@router.post("/{plan_id}/confirm")
def confirm(plan_id: str, body: ConfirmRequest) -> dict[str, object]:
    """Valide l'identification proposee, sans rien changer d'autre.

    Il manquait le geste symetrique de « Ce n'est pas ca ». Un plan a 78 % est
    tres souvent correct — le score dit l'incertitude de la MACHINE, pas celle
    de la personne qui regarde. Sans ce bouton, la seule facon d'accepter etait
    de cocher puis d'executer, ce qui melange deux decisions distinctes :
    « c'est la bonne oeuvre » et « range-le maintenant ».

    Le choix est retenu, comme une correction : c'est ce qui fait qu'un scan
    suivant ne repose pas la question.
    """
    with _lock:
        plan = _plans.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan inconnu ou deja applique.")
    if not plan.title:
        raise HTTPException(
            status_code=400,
            detail="Ce fichier n'a aucune identification a confirmer.",
        )

    with _lock:
        if body.plan_ids:
            targets = [_plans[pid] for pid in body.plan_ids if pid in _plans] or [plan]
        elif body.whole_series and plan.kind != "movie" and plan.title:
            targets = [
                p
                for p in _plans.values()
                if p.title == plan.title and p.kind == plan.kind and p.decision is not Decision.AUTO
            ] or [plan]
        else:
            targets = [plan]

        for target in targets:
            # AUTO et `manual` : un accord explicite vaut mieux que n'importe
            # quel score, et le repasser au calcul reviendrait a douter de la
            # personne qui vient de decider.
            target.decision = Decision.AUTO
            target.manual = True
            target.score = 1.0
            target.reasons = ["identification confirmee a la main"]

    if plan.provider and plan.external_id:
        get_memory().remember(
            RememberedDecision(
                kind=plan.kind,
                title_key=title_key(plan.title),
                provider=plan.provider,
                external_id=plan.external_id,
                title=plan.title,
                year=plan.year,
                poster_url=plan.poster_url,
            )
        )

    _persist_plans()
    return {"confirmed": len(targets), "queue": _queue()}


class ChooseRequest(BaseModel):
    provider: str
    external_id: str

    whole_series: bool = True
    """Applique le choix a TOUS les episodes de la meme serie.

    L'identification porte sur l'oeuvre, pas sur le fichier : corriger episode
    par episode reviendrait a repondre douze fois a la meme question. Les films
    ignorent ce champ, chacun etant une decision independante."""


@router.post("/{plan_id}/choose")
async def choose(plan_id: str, body: ChooseRequest) -> dict[str, object]:
    """Impose un candidat choisi par l'utilisateur et recalcule la destination.

    Aucun signal automatique ne separe « Dark Matter » 2015 de celui de 2024 :
    meme titre, meme type, deux oeuvres reelles. Le score ne peut pas trancher
    — seul un humain le peut, et c'est le role de cet endpoint.

    Le plan resultant est marque `manual` et passe en AUTO : un choix explicite
    vaut mieux que n'importe quel score, le repasser au calcul reviendrait a
    douter de la personne qui vient de decider.
    """
    conf = get_settings()

    with _lock:
        plan = _plans.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan inconnu ou deja applique.")

    chosen = next(
        (
            c
            for c in plan.alternatives
            if c.provider == body.provider and c.external_id == body.external_id
        ),
        None,
    )
    if chosen is None:
        raise HTTPException(status_code=400, detail="Ce candidat n'est pas propose pour ce plan.")

    scan = last_scan()
    by_path = {f.path: f for f in (scan.files if scan else [])}
    if plan.source not in by_path:
        raise HTTPException(
            status_code=409,
            detail="Le fichier n'est plus dans le dernier scan. Relance un scan.",
        )

    # Tous les plans de la meme oeuvre, pour corriger la serie entiere d'un
    # coup. Le regroupement se fait sur le titre RETENU, celui qui est faux —
    # c'est bien lui qui identifie l'ensemble a rectifier.
    with _lock:
        if body.whole_series and plan.kind != "movie":
            targets = [
                p
                for p in _plans.values()
                if p.kind == plan.kind and p.title == plan.title and p.source in by_path
            ]
        else:
            targets = [plan]

    store = get_store()
    prefs = store.load()
    pipeline = Pipeline(
        tmdb=TMDBProvider(conf.tmdb_api_key) if conf.tmdb_api_key else None,
        anilist=AniListProvider(),
        library_root=conf.library_root,
        templates={k: prefs.template_for(k) for k in ("movie", "episode", "anime")},
        destination_for=store.destination_root,
        policy=Policy(
            auto_apply_threshold=conf.auto_apply_threshold,
            reject_threshold=conf.reject_threshold,
        ),
    )

    rebuilt: list[Plan] = []
    try:
        for target in targets:
            # Une copie du candidat par episode : l'enrichissement y ecrit le
            # titre de l'episode, et un objet partage les ecraserait les uns
            # apres les autres.
            per_file = replace(chosen, extra=dict(chosen.extra))
            rebuilt.append(await pipeline.replan_with(by_path[target.source], per_file, target))
    finally:
        await pipeline.aclose()

    # Le choix est RETENU : la meme question ne sera plus posee au prochain
    # scan. C'est ce qui fait converger la file au lieu de la voir se remplir
    # a l'identique a chaque fois.
    if plan.title:
        get_memory().remember(
            RememberedDecision(
                kind=plan.kind,
                title_key=title_key(plan.title),
                provider=chosen.provider,
                external_id=chosen.external_id,
                title=chosen.title,
                year=chosen.year,
                poster_url=chosen.poster_url,
            )
        )

    with _lock:
        for target in targets:
            _plans.pop(target.id, None)
        for plan_out in rebuilt:
            _plans[plan_out.id] = plan_out
    _persist_plans()

    return {"corrected": len(rebuilt), "queue": _queue(), "remembered": bool(plan.title)}


@router.post("/undo")
def undo(body: UndoRequest) -> dict[str, object]:
    """Annule des deplacements. Du plus precis au plus large.

    L'ordre des criteres n'est pas arbitraire : une requete qui nomme une
    oeuvre veut cette oeuvre, pas « les N dernieres operations ». Retomber sur
    le compte serait la pire des interpretations, puisqu'elle defait autre
    chose que ce qui etait demande.
    """
    journal = get_journal()
    if body.plan_ids:
        results = undo_plans(journal, body.plan_ids)
    elif body.work:
        results = undo_work(journal, body.work)
    else:
        results = undo_last(journal, max(1, body.count))
    return {
        "undone": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [
            {"ok": r.ok, "from": r.source, "to": r.destination, "message": r.message}
            for r in results
        ],
        "journal_size": len(get_journal().read_all()),
        "remaining": _remaining(),
        "planned": len(_job.done_paths),
    }


@router.get("/journal")
def read_journal(limit: int = 50) -> dict[str, object]:
    records = get_journal().read_all()
    recent = records[-limit:][::-1]
    return {
        "total": len(records),
        # Regroupe par oeuvre : c'est a cette maille qu'on decide d'annuler,
        # pas a celle du fichier ni a celle de la session entiere.
        "works": group_by_work(records),
        "entries": [
            {
                "timestamp": r.timestamp,
                "source": r.source,
                "destination": r.destination,
                "method": r.method,
            }
            for r in recent
        ],
    }
