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

from ..config import get_settings
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
