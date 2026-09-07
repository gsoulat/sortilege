"""Bibliotheque : lancer un scan et consulter ce qui a ete trouve.

Le resultat est garde en memoire pour l'instant. C'est assume et temporaire :
la persistance arrivera avec les modeles SQLModel et le journal d'annulation.
Une bibliotheque scannee tient largement en memoire, et rien n'est encore
applique au disque, donc rien de precieux n'est perdu au redemarrage.
"""

from __future__ import annotations

import logging
from threading import Lock

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..core.scanner import ScanResult, scan
from .deps import get_store
from .schemas import ScanOut, scan_to_out

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/library", tags=["bibliotheque"])

_last_scan: ScanResult | None = None
_last_deep: bool = True
_lock = Lock()


@router.post("/scan", response_model=ScanOut)
def run_scan(deep: bool = True, limit: int | None = 500) -> ScanOut:
    """Parcourt les racines sources.

    ``deep`` lit aussi le contenu des fichiers (ffprobe, .nfo) : bien plus
    informatif, et bien plus lent. ``limit`` borne le nombre de fichiers pour
    qu'un premier scan sur une grosse bibliotheque reste rapide.
    """
    global _last_scan, _last_deep

    settings = get_settings()
    if not settings.source_roots:
        raise HTTPException(
            status_code=400,
            detail="Aucune racine source configuree (SORTILEGE_SOURCE_ROOTS).",
        )

    # Les racines effectivement parcourues dependent des preferences : on peut
    # exclure un montage reseau lent qu'on ne veut pas reparcourir a chaque fois.
    roots = get_store().resolved_sources()
    if not roots:
        raise HTTPException(
            status_code=400,
            detail="Aucune source selectionnee. Active au moins une racine dans les réglages.",
        )

    # Un scan est du travail bloquant : deux scans concurrents doubleraient la
    # charge disque pour un resultat identique.
    if not _lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Un scan est deja en cours.")

    try:
        result = scan(roots, deep=deep, limit=limit, library_root=settings.library_root)
        _last_scan = result
        _last_deep = deep
        logger.info(
            "scan termine : %s fichiers, %s ignores, %s erreurs",
            result.total,
            result.skipped,
            len(result.errors),
        )
        return scan_to_out(result, deep=deep)
    finally:
        _lock.release()


@router.get("", response_model=ScanOut)
def get_library() -> ScanOut:
    """Dernier scan connu. Vide tant qu'aucun scan n'a ete lance."""
    if _last_scan is None:
        return ScanOut(total=0, skipped=0, errors=[], files=[], deep=_last_deep)
    return scan_to_out(_last_scan, deep=_last_deep)
