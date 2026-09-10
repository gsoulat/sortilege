"""Bibliotheque : lancer un scan et consulter ce qui a ete trouve.

Le scan tourne en TACHE DE FOND. Une bibliotheque reelle prend plusieurs
minutes a analyser quand ffprobe est actif ; le faire dans la requete HTTP
laissait l'utilisateur devant un bouton fige, et surtout exposait a un
delai d'attente du navigateur qui aurait perdu le resultat d'un travail
pourtant termine.

Le resultat est garde en memoire pour l'instant, en attendant la persistance.
Rien de precieux ne s'y trouve : le seul etat durable — ce qui a REELLEMENT ete
deplace — vit dans le journal sur disque.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from threading import Lock, Thread

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..core.companions import find_empty_dirs, find_orphan_dirs, trash_destination, trash_root_for
from ..core.journal import _move, prune_empty_dirs
from ..core.scanner import ScanResult, scan
from ..core.snapshot import SCAN_KEY, SnapshotError, scan_in, scan_out
from .deps import get_memory, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["bibliotheque"])


@dataclass
class ScanJob:
    """Etat d'un scan en cours ou termine."""

    running: bool = False
    processed: int = 0
    total: int = 0
    current: str = ""
    phase: str = "idle"
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str | None = None
    result: ScanResult | None = None
    deep: bool = True
    _lock: Lock = field(default_factory=Lock, repr=False)

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return end - self.started_at

    @property
    def eta_seconds(self) -> float | None:
        """Temps restant estime, par simple extrapolation lineaire.

        Les fichiers ne coutent pas tous pareil (ffprobe depend de la taille et
        du conteneur), donc l'estimation bouge. Elle reste bien plus utile
        qu'aucune indication : ce que l'utilisateur veut savoir, c'est « des
        secondes ou des minutes ».
        """
        if not self.running or self.processed <= 0 or self.total <= 0:
            return None
        rate = self.elapsed / self.processed
        return max(0.0, rate * (self.total - self.processed))


_job = ScanJob()


class ResetRequest(BaseModel):
    confirm: bool = False


@router.post("/reset")
def reset_workspace(body: ResetRequest) -> dict[str, object]:
    """Efface l'etat de travail pour repartir d'un scan neuf.

    Ce qui part est RECONSTRUCTIBLE : l'instantane du scan, les plans calcules,
    l'avancement des lots, les apercus en cache. Un scan les refait.

    Ce qui reste ne se refait pas, et c'est la distinction qui compte :

    - **le journal d'annulation**, seul chemin de retour pour tout ce qui a
      deja ete deplace — l'effacer condamnerait des milliers de fichiers a
      rester ou ils sont ;
    - **les identifications retenues**, tranchees une a une a la main ;
    - **les preferences** : sources, destinations, gabarits.

    Une purge qui emporterait le journal serait une catastrophe silencieuse.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail="La remise a zero doit etre confirmee.")

    from . import collection, media, review

    plans = review.forget_everything()

    _job.result = None
    _job.error = None
    _job.processed = 0
    _job.total = 0
    _job.phase = "termine"
    get_memory().save_blob(SCAN_KEY, None)

    collection.forget_index()
    vignettes = media.purge_thumbnails()

    logger.info("etat de travail efface : %s plan(s), %s vignette(s)", plans, vignettes)
    return {"cleared_plans": plans, "cleared_thumbnails": vignettes, **_status()}


class OrphanPruneRequest(BaseModel):
    confirm: bool = False
    mode: str = "trash"
    """« trash » met le contenu en corbeille, « delete » le supprime."""


class PruneRequest(BaseModel):
    confirm: bool = False
    """Obligatoire. La liste se consulte d'abord : un balayage destructeur sur
    des centaines de dossiers ne doit pas partir du meme geste que celui qui
    sert a le regarder."""


@router.get("/orphan-dirs")
def read_orphan_dirs() -> dict[str, object]:
    """Dossiers sans aucune video, ne contenant que ses accessoires.

    Ranger un film emporte la video et ses compagnons, mais un dossier de
    release garde souvent ce qui n'accompagnait RIEN : une jaquette au nom de
    la release, un .nfo, un .xml de metadonnees. Le dossier n'est donc jamais
    vide au sens strict, et le balayage precedent ne le voit pas — il n'est
    plus qu'une coquille.
    """
    store = get_store()
    orphelins = find_orphan_dirs(store.resolved_sources())
    return {
        "count": len(orphelins),
        "bytes": sum(o.bytes for o in orphelins),
        "dirs": [
            {
                "path": str(o.path),
                "files": [f.name for f in o.files[:8]],
                "file_count": len(o.files),
                "bytes": o.bytes,
            }
            for o in orphelins[:200]
        ],
    }


@router.post("/orphan-dirs/prune")
def prune_orphan_dirs(body: OrphanPruneRequest) -> dict[str, object]:
    """Evacue le contenu de ces dossiers, puis les supprime.

    Ce ne sont pas des dossiers vides : ils contiennent de vrais fichiers. Ils
    partent donc en CORBEILLE par defaut, ou l'on peut encore aller les
    rechercher — une jaquette perdue est sans consequence, mais c'est le genre
    de certitude qu'on n'a qu'apres coup.

    ``mode=delete`` supprime directement, pour qui veut recuperer la place sans
    seconde corvee de vidage.

    Rien n'est journalise, deliberement : le journal d'annulation sert a
    retrouver des VIDEOS deplacees, et y verser des centaines de jaquettes le
    diluerait au point de le rendre illisible le jour ou l'on en a vraiment
    besoin. La corbeille joue ce role ici.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Le nettoyage doit etre confirme.")

    conf = get_settings()
    store = get_store()
    racines = store.resolved_sources()
    lot = time.strftime("%Y-%m-%d")

    dossiers, fichiers, echecs = 0, 0, []
    for orphelin in find_orphan_dirs(racines):
        rate = False
        for fichier in orphelin.files:
            try:
                if body.mode == "delete":
                    fichier.unlink()
                else:
                    corbeille = trash_root_for(fichier, conf.library_root, racines)
                    cible = trash_destination(corbeille, lot, fichier)
                    if cible.exists():
                        cible.unlink()
                    _move(fichier, cible)
                fichiers += 1
            except OSError as exc:
                echecs.append(f"{fichier} : {exc}")
                rate = True
        if not rate:
            dossiers += prune_empty_dirs(orphelin.path, racines)

    logger.info("coquilles nettoyees : %s dossier(s), %s fichier(s)", dossiers, fichiers)
    return {
        "removed_dirs": dossiers,
        "handled_files": fichiers,
        "failed": echecs[:20],
        **read_orphan_dirs(),
    }


@router.get("/empty-dirs")
def read_empty_dirs() -> dict[str, object]:
    """Dossiers vides sous les sources, sans rien supprimer.

    Le nettoyage a la volee ne rattrape que ce qu'il vient de vider : les
    carcasses des rangements anterieurs restent, et le client de telechargement
    en cree de son cote.
    """
    store = get_store()
    vides = find_empty_dirs(store.resolved_sources())
    return {
        "count": len(vides),
        # Bornee : trois cents chemins suffisent a juger, et la liste entiere
        # ne se lit pas de toute facon.
        "dirs": [str(d) for d in vides[:300]],
    }


@router.post("/empty-dirs/prune")
def prune_empty_dirs_endpoint(body: PruneRequest) -> dict[str, object]:
    """Supprime les dossiers vides trouves. Ne touche a aucun fichier.

    Ils sont supprimes et non mis en corbeille : un dossier vide ne contient
    rien a recuperer. Les racines sont exclues, et l'ordre — les plus profonds
    d'abord — permet a un parent devenu vide de partir dans la meme passe.
    """
    if not body.confirm:
        raise HTTPException(status_code=400, detail="Le nettoyage doit etre confirme.")

    store = get_store()
    supprimes, echecs = 0, []
    for chemin in find_empty_dirs(store.resolved_sources()):
        try:
            chemin.rmdir()
            supprimes += 1
        except OSError as exc:
            echecs.append(f"{chemin} : {exc}")

    logger.info("dossiers vides supprimes : %s", supprimes)
    return {"removed": supprimes, "failed": echecs[:20], **read_empty_dirs()}


def _reconcile(result: ScanResult) -> None:
    """Recale la file et le cache sur ce que le scan vient de trouver.

    Importe ici et non en tete du module : review importe library, et l'inverse
    au chargement fermerait le cycle.
    """
    from . import media, review

    present = {str(f.path) for f in result.files}
    review.reconcile_with_scan(present)
    media.purge_thumbnails()


def _persist_scan() -> None:
    """Ecrit le scan courant sur disque. Ne leve jamais.

    Un scan de mille fichiers coute plusieurs minutes de disque ; le reperdre
    a chaque redemarrage du conteneur — donc a chaque mise a jour d'image —
    suffit a rendre l'outil penible. Un echec d'ecriture, lui, ne justifie
    pas de perdre le scan qu'on vient tout juste de terminer.
    """
    if _job.result is None:
        return
    try:
        get_memory().save_blob(SCAN_KEY, scan_out(_job.result, deep=_job.deep))
    except Exception:
        logger.exception("enregistrement du scan impossible")


def restore_scan() -> bool:
    """Relit le dernier scan enregistre. Vrai si quelque chose a ete repris.

    Appele au demarrage. Un instantane illisible — schema d'une version
    anterieure, champ disparu — ramene simplement a « pas de scan » : perdre
    une reprise est un desagrement, ne pas demarrer est une panne.
    """
    raw = get_memory().load_blob(SCAN_KEY)
    if raw is None:
        return False
    try:
        result, deep = scan_in(raw)
    except SnapshotError as exc:
        logger.warning("scan enregistre ignore : %s", exc)
        return False

    adopt_scan(result, deep=deep, persist=False)
    logger.info("scan repris depuis le disque : %s fichiers", result.total)
    return True


def adopt_scan(result: ScanResult, *, deep: bool, persist: bool = True) -> None:
    """Enregistre un scan produit hors de l'endpoint.

    Le cycle automatique passe par ici plutot que d'appeler l'API : l'interface
    doit montrer le meme etat, qu'un humain ou l'horloge ait declenche le scan.
    """
    _job.result = result
    _job.deep = deep
    _job.processed = result.total
    _job.total = result.total
    _job.finished_at = time.monotonic()
    _job.phase = "termine"
    if persist:
        _persist_scan()


def last_scan() -> ScanResult | None:
    """Dernier scan termine, pour les autres routeurs.

    Un accesseur plutot qu'un import de la variable : ``from .library import
    _job`` capturerait la valeur au moment de l'import.
    """
    return _job.result


def _run(roots, deep: bool, limit: int | None, library_root) -> None:
    """Corps du scan, execute dans un thread."""
    try:
        _job.phase = "recensement"

        def progress(processed: int, total: int, name: str) -> None:
            _job.processed = processed
            _job.total = total
            _job.current = name
            _job.phase = "analyse"

        result = scan(
            roots,
            deep=deep,
            limit=limit,
            library_root=library_root,
            on_progress=progress,
        )
        _job.result = result
        _job.error = None
        _persist_scan()
        _reconcile(result)
        logger.info(
            "scan termine : %s fichiers, %s ignores, %s erreurs",
            result.total,
            result.skipped,
            len(result.errors),
        )
    except Exception as exc:
        # Un thread qui leve mourrait en silence : sans cette capture, l'UI
        # attendrait indefiniment un scan qui n'existe plus.
        logger.exception("le scan a echoue")
        _job.error = f"{type(exc).__name__}: {exc}"
    finally:
        _job.running = False
        _job.phase = "termine"
        _job.finished_at = time.monotonic()
        _job.current = ""


@router.post("/scan")
def start_scan(deep: bool = True, limit: int | None = None) -> dict[str, object]:
    """Demarre un scan et rend la main immediatement."""
    settings = get_settings()
    if not settings.source_roots:
        raise HTTPException(
            status_code=400,
            detail="Aucune racine source configurée (SORTILEGE_SOURCE_ROOTS).",
        )

    roots = get_store().resolved_sources()
    if not roots:
        raise HTTPException(
            status_code=400,
            detail="Aucune source sélectionnée. Active au moins une source dans les réglages.",
        )

    with _job._lock:
        if _job.running:
            # Ni une erreur ni un second scan : on renvoie l'etat du scan en
            # cours. Cliquer deux fois doit montrer la progression, pas
            # afficher un refus.
            return _status()

        _job.running = True
        _job.processed = 0
        _job.total = 0
        _job.current = ""
        _job.phase = "recensement"
        _job.error = None
        _job.started_at = time.monotonic()
        _job.finished_at = 0.0
        _job.deep = deep

    Thread(
        target=_run,
        args=(roots, deep, limit, settings.library_root),
        daemon=True,
        name="sortilege-scan",
    ).start()

    return _status()


def scan_status() -> dict[str, object]:
    """Etat du scan, pour la vue unifiee.

    Fonction et non route : elle est appelee dans le processus, par
    ``/api/workspace`` qui agrege les trois travaux en une seule reponse.
    """
    return _status()


def _status() -> dict[str, object]:
    return {
        "running": _job.running,
        "phase": _job.phase,
        "processed": _job.processed,
        "total": _job.total,
        "current": _job.current,
        "elapsed": round(_job.elapsed, 1),
        "eta": round(_job.eta_seconds, 1) if _job.eta_seconds is not None else None,
        "error": _job.error,
        "has_result": _job.result is not None,
        "found": _job.result.total if _job.result else 0,
    }
