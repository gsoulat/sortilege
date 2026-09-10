"""Trois vues sur la bibliotheque : ce qu'on a, ce qui manque, ce qui est double.

L'index est reconstruit a la demande plutot que tenu a jour en continu : sans
persistance, un index vivant serait perdu a chaque redemarrage, et le
reconstruire prend le meme temps. Il tourne en tache de fond, comme le scan.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..core.collection import Work, fill_known_episodes, group
from ..core.companions import TRASH_DIRNAME, trash_destination
from ..core.journal import MoveRecord, _move
from ..core.matching import title_similarity
from ..core.scanner import scan
from ..core.trash import MIN_AGE_DAYS, inventory, purge, total_bytes
from ..providers.tmdb import TMDBProvider
from .deps import get_journal, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/collection", tags=["collection"])


@dataclass
class IndexJob:
    running: bool = False
    processed: int = 0
    total: int = 0
    current: str = ""
    built_at: float = 0.0
    error: str | None = None
    works: list[Work] = field(default_factory=list)


_job = IndexJob()
_lock = asyncio.Lock()


class TrashRequest(BaseModel):
    paths: list[str]
    """Chemins RELATIFS a la racine de bibliotheque, tels que l'index les
    expose. Un chemin absolu venu du client serait une porte ouverte."""


def forget_index() -> None:
    """Oublie l'index de bibliotheque. « Relire la bibliotheque » le refait."""
    _job.works = []
    _job.built_at = 0.0


def current_works() -> list[Work]:
    """Index courant de la bibliotheque, tel que la derniere construction l'a
    laisse. Vide tant qu'aucune n'a eu lieu."""
    return list(_job.works)


async def _enrich(works: list[Work], tmdb: TMDBProvider | None) -> None:
    """Ajoute affiche et episodes connus. Sans cle, l'index reste utilisable.

    La collection et les doublons ne dependent d'aucun fournisseur — seuls les
    manques en ont besoin. Une bibliotheque sans cle TMDB doit donc rester
    consultable, avec une fonction en moins plutot qu'un ecran vide.
    """
    if tmdb is None:
        return

    for index, work in enumerate(works, start=1):
        _job.processed = index
        _job.current = work.title

        try:
            if work.kind == "movie":
                candidates = await tmdb.search_movie(work.title, work.year)
            else:
                candidates = await tmdb.search_series(work.title, work.year)
        except Exception:
            # Un echec d identification ne doit pas vider tout l index : l oeuvre
            # reste listee, simplement sans affiche.
            logger.warning("identification impossible pour %s", work.title)
            continue

        if not candidates:
            continue

        # Meme calcul de similarite que le pipeline : l'index ne doit pas
        # identifier autrement que le rangement, sinon la grille montrerait une
        # oeuvre et le plan en rangerait une autre.
        chosen = max(candidates, key=lambda c: (title_similarity(work.title, c), c.popularity))
        work.poster_url = chosen.poster_url
        work.provider = chosen.provider
        work.external_id = chosen.external_id
        work.identified = True
        if work.year is None:
            work.year = chosen.year

        if work.kind == "movie":
            continue

        for season in work.seasons.values():
            if season.number <= 0:
                continue
            episodes = await tmdb.get_season(chosen.external_id, season.number)
            fill_known_episodes(season, episodes)


async def _build() -> None:
    conf = get_settings()

    roots = [conf.library_root]
    result = scan(roots, deep=False, library_root=conf.library_root)

    works = group(result.files, get_store().load().quality)
    _job.total = len(works)
    _job.processed = 0

    tmdb = TMDBProvider(conf.tmdb_api_key) if conf.tmdb_api_key else None
    try:
        await _enrich(works, tmdb)
    finally:
        if tmdb is not None:
            await tmdb.aclose()

    _job.works = works
    _job.built_at = time.time()
    logger.info("index construit : %s oeuvres", len(works))


@router.post("/build")
async def build() -> dict[str, object]:
    """Reconstruit l'index de la bibliotheque."""
    if _lock.locked():
        return _status()

    async with _lock:
        _job.running = True
        _job.error = None
        _job.processed = 0
        _job.total = 0
        _job.current = ""
        try:
            await _build()
        except Exception as exc:
            _job.error = f"{type(exc).__name__}: {exc}"
            logger.exception("la construction de l'index a echoue")
        finally:
            _job.running = False
            _job.current = ""

    return _status()


def _status() -> dict[str, object]:
    return {
        "running": _job.running,
        "processed": _job.processed,
        "total": _job.total,
        "current": _job.current,
        "built_at": _job.built_at or None,
        "error": _job.error,
        "count": len(_job.works),
    }


def status() -> dict[str, object]:
    """Etat de l'indexation, pour la vue unifiee.

    Fonction et non route : elle est appelee dans le processus. Les trois
    lectures qui l'accompagnaient — collection, manquants, doublons — servaient
    l'ancienne vue « Ma collection » et sont mortes avec elle. La vue unifiee
    les obtient par ``/api/workspace``, qui les rassemble avec le reste.
    """
    return _status()


@router.post("/duplicates/trash")
def trash_duplicates(body: TrashRequest) -> dict[str, object]:
    """Deplace des doublons vers la corbeille. NE SUPPRIME RIEN.

    Meme mecanisme que les restes de release : le fichier reste sur le disque,
    l'operation est journalisee, et l'annulation la defait. Supprimer un
    fichier de 40 Go sur une erreur de detection serait irreparable ; le
    deplacer coute un dossier a vider quand on a verifie.
    """
    conf = get_settings()
    journal = get_journal()
    trash_root = conf.library_root / TRASH_DIRNAME
    batch = time.strftime("%Y-%m-%d")

    moved: list[dict[str, object]] = []
    for relative in body.paths:
        # Le chemin vient du client : il est resolu SOUS la racine et verifie,
        # jamais utilise tel quel.
        try:
            source = _resolve_in_library(relative, conf.library_root)
        except ValueError as exc:
            moved.append({"path": relative, "ok": False, "message": str(exc)})
            continue

        if not source.is_file():
            moved.append({"path": relative, "ok": False, "message": "fichier introuvable"})
            continue

        target = trash_destination(trash_root, batch, source)
        try:
            method = _move(source, target)
        except OSError as exc:
            moved.append({"path": relative, "ok": False, "message": str(exc)})
            continue

        journal.append(
            MoveRecord(
                timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                plan_id="doublon",
                source=str(source),
                destination=str(target),
                method=method,
                kind="trash",
            )
        )
        moved.append({"path": relative, "ok": True, "message": "mis en corbeille"})

    return {
        "trashed": sum(1 for m in moved if m["ok"]),
        "failed": sum(1 for m in moved if not m["ok"]),
        "results": moved,
    }


class PurgeRequest(BaseModel):
    older_than_days: int = 30
    """Age minimal d'un lot pour etre supprime. 0 = tout, sans exception."""

    confirm_all: bool = False
    """Obligatoire pour un vidage integral.

    Supprimer ce qui vient d'etre evacue est parfaitement legitime — c'est meme
    le geste attendu quand on cherche de la place — mais cela ne doit pas
    pouvoir arriver par un reglage laisse a zero. La protection est explicite
    et vient de l'appelant, plutot qu'un plancher silencieux qui laisserait en
    place ce qu'on vient de demander de supprimer."""


@router.get("/trash")
def read_trash() -> dict[str, object]:
    """Ce que contient la corbeille, lot par lot.

    Sortilege ne supprime jamais : restes de release, doublons et copies deja
    rangees s'y accumulent. C'est la bonne regle, mais elle a une consequence
    que rien ne traitait — la corbeille ne se vide pas toute seule.
    """
    conf = get_settings()
    batches = inventory(conf.library_root / TRASH_DIRNAME)
    return {
        "min_age_days": MIN_AGE_DAYS,
        "total_bytes": total_bytes(batches),
        "total_files": sum(b.files for b in batches),
        "batches": [
            {
                "day": b.day.isoformat(),
                "age_days": b.age_days,
                "files": b.files,
                "bytes": b.bytes,
            }
            for b in batches
        ],
    }


@router.post("/trash/purge")
def purge_trash(body: PurgeRequest) -> dict[str, object]:
    """Supprime DEFINITIVEMENT les lots plus vieux que le delai donne.

    Le seul endroit de l'application qui supprime reellement, et le seul qui
    rende de la place. Un vidage integral demande une confirmation explicite —
    non pour dissuader, mais pour qu'il ne se declenche pas par un champ laisse
    a zero.
    """
    if body.older_than_days <= 0 and not body.confirm_all:
        raise HTTPException(
            status_code=400,
            detail="Un vidage integral doit etre confirme explicitement.",
        )

    conf = get_settings()
    result = purge(conf.library_root / TRASH_DIRNAME, body.older_than_days)
    return {
        "removed_batches": result.removed_batches,
        "removed_files": result.removed_files,
        "freed_bytes": result.freed_bytes,
        "errors": result.errors,
        **read_trash(),
    }


def _resolve_in_library(relative: str, library_root: Path) -> Path:
    if relative.startswith("/") or any(
        part in ("..", ".") for part in relative.replace("\\", "/").split("/")
    ):
        raise ValueError("chemin refuse")
    resolved = (library_root / relative).resolve(strict=False)
    root = library_root.resolve()
    if root not in resolved.parents:
        raise ValueError("chemin hors de la bibliotheque")
    return resolved
