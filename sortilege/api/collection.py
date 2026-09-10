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
from .deps import get_journal, get_store, scan_rules, tmdb_key, tmdb_language

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


class DuplicateGroupIn(BaseModel):
    """Un groupe de doublons : l'exemplaire garde, et ceux a supprimer.

    Le chemin GARDE est exige, et non deduit. Supprimer sans filet n'a de sens
    que si l'on a verifie qu'il reste bien quelque chose : sans cette donnee, un
    bogue d'affichage ou un appel malforme effacerait le dernier exemplaire.
    """

    keep: str
    paths: list[str]


class DeleteDuplicatesRequest(BaseModel):
    groups: list[DuplicateGroupIn]

    confirm: bool = False
    """Obligatoire. La suppression ne se rattrape pas, elle ne doit pas pouvoir
    arriver par un champ oublie."""


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
    result = scan(roots, deep=False, library_root=conf.library_root, rules=scan_rules())

    works = group(result.files, get_store().load().quality)
    _job.total = len(works)
    _job.processed = 0

    cle = tmdb_key()
    tmdb = TMDBProvider(cle, tmdb_language()) if cle else None
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


@router.post("/duplicates/delete")
def delete_duplicates(body: DeleteDuplicatesRequest) -> dict[str, object]:
    """Supprime des doublons SANS passer par la corbeille.

    La corbeille reste le geste par defaut, et c'est le bon quand on ne fait
    que soupconner un doublon. Mais quand on cherche de la place, deplacer six
    cents gigaoctets vers une corbeille qu'il faudra vider ensuite double le
    travail sans rien proteger de plus : le fichier garde est LA, verifie a
    chaque suppression.

    Rien n'est journalise. Une ligne de journal qui ne pourrait rien defaire
    serait un mensonge poli ; le dire est plus honnete.
    """
    if not body.confirm:
        raise HTTPException(400, "suppression non confirmee")

    conf = get_settings()
    resultats: list[dict[str, object]] = []
    liberes = 0

    for groupe in body.groups:
        try:
            garde = _resolve_in_library(groupe.keep, conf.library_root)
        except ValueError as exc:
            resultats.append({"path": groupe.keep, "ok": False, "message": str(exc)})
            continue

        # La condition qui rend l'operation acceptable : on ne supprime un
        # exemplaire que si celui qu'on garde existe REELLEMENT sur le disque.
        if not garde.is_file():
            resultats.append(
                {
                    "path": groupe.keep,
                    "ok": False,
                    "message": "l'exemplaire a garder est introuvable — rien n'a ete supprime",
                }
            )
            continue

        for relative in groupe.paths:
            try:
                cible = _resolve_in_library(relative, conf.library_root)
            except ValueError as exc:
                resultats.append({"path": relative, "ok": False, "message": str(exc)})
                continue

            if cible == garde:
                resultats.append(
                    {"path": relative, "ok": False, "message": "c'est l'exemplaire garde"}
                )
                continue
            if not cible.is_file():
                resultats.append({"path": relative, "ok": False, "message": "fichier introuvable"})
                continue

            try:
                taille = cible.stat().st_size
                cible.unlink()
            except OSError as exc:
                resultats.append({"path": relative, "ok": False, "message": str(exc)})
                continue

            liberes += taille
            logger.info("doublon supprime : %s (%s octets)", cible, taille)
            resultats.append({"path": relative, "ok": True, "message": "supprime"})

    return {
        "deleted": sum(1 for r in resultats if r["ok"]),
        "failed": sum(1 for r in resultats if not r["ok"]),
        "freed_bytes": liberes,
        "results": resultats,
    }


class PruneDuplicatesRequest(BaseModel):
    confirm: bool = False
    """Obligatoire, comme pour la suppression unitaire."""

    trash: bool = False
    """Passer par la corbeille plutot que supprimer. La suppression directe est
    le defaut ici : quand on nettoie toute une bibliotheque d'un coup, deplacer
    des centaines de gigaoctets vers une corbeille qu'il faudra vider ensuite
    double le travail sans rien proteger de plus."""


@router.post("/duplicates/prune")
def prune_duplicates(body: PruneDuplicatesRequest) -> dict[str, object]:
    """Ne garde qu'un exemplaire par emplacement, celui que la STRATEGIE designe.

    Le choix n'est pas refait ici : ``collection.group`` a deja classe chaque
    exemplaire selon la strategie du type — 2160p d'abord en « Qualité
    maximale », 720p d'abord en « Économie de place ». Refaire l'arbitrage a cet
    endroit reviendrait a entretenir deux regles pour une meme question, et
    elles finiraient par diverger.

    Cette route travaille sur l'index du SERVEUR et non sur ce que l'interface
    affiche : une page tronquee a deux cents oeuvres ferait oublier les autres,
    silencieusement.
    """
    if not body.confirm:
        raise HTTPException(400, "operation non confirmee")

    groupes = [
        DuplicateGroupIn(keep=g.best.relative_path, paths=[f.relative_path for f in g.redundant])
        for work in current_works()
        for g in work.duplicates
        if g.redundant
    ]
    if not groupes:
        return {"deleted": 0, "failed": 0, "freed_bytes": 0, "results": []}

    logger.info(
        "nettoyage des doublons : %s emplacement(s), %s exemplaire(s) en trop",
        len(groupes),
        sum(len(g.paths) for g in groupes),
    )

    if body.trash:
        sortie = trash_duplicates(TrashRequest(paths=[p for g in groupes for p in g.paths]))
        return {**sortie, "deleted": sortie["trashed"], "freed_bytes": 0}
    return delete_duplicates(DeleteDuplicatesRequest(groups=groupes, confirm=True))


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
