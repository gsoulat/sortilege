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
from dataclasses import dataclass, field

from fastapi import APIRouter

from ..config import get_settings
from ..core.companions import TRASH_DIRNAME
from ..core.journal import apply_plan
from ..core.pipeline import Pipeline
from ..core.scanner import scan
from ..core.scoring import Decision, Policy
from ..core.watch import Watcher
from ..providers.anilist import AniListProvider
from ..providers.tmdb import TMDBProvider
from . import library, review
from .deps import get_journal, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/automation", tags=["automatisation"])


@dataclass
class CycleReport:
    at: float = 0.0
    detected: int = 0
    scanned: int = 0
    planned: int = 0
    applied: int = 0
    queued: int = 0
    message: str = ""


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
        report.message = "aucune source selectionnee"
        return report

    _watcher.quiet_seconds = auto.quiet_seconds
    fresh = _watcher.poll(roots)
    report.detected = len(fresh)

    if not fresh and not forced:
        report.message = "rien de nouveau"
        return report

    # --- Scan ---
    result = scan(roots, deep=True, library_root=conf.library_root)
    library.adopt_scan(result, deep=True)
    report.scanned = result.total

    if not result.files:
        report.message = "aucun fichier a traiter"
        return report

    if not conf.tmdb_api_key:
        report.message = "aucune cle TheMovieDB : identification impossible"
        return report

    # --- Plan ---
    pipeline = Pipeline(
        tmdb=TMDBProvider(conf.tmdb_api_key),
        anilist=AniListProvider(),
        library_root=conf.library_root,
        templates={k: prefs.template_for(k) for k in ("movie", "episode", "anime")},
        policy=Policy(
            auto_apply_threshold=conf.auto_apply_threshold,
            reject_threshold=conf.reject_threshold,
        ),
        ai=review._ai_resolver(),
        ai_batch_size=prefs.ai.batch_size,
        ai_threshold=prefs.ai.threshold,
    )
    try:
        plans = await pipeline.plan_all(result.files)
    finally:
        await pipeline.aclose()

    review.adopt_plans(plans)
    report.planned = len(plans)

    confident = [p for p in plans if p.decision is Decision.AUTO]
    report.queued = sum(1 for p in plans if p.decision is Decision.REVIEW)

    # --- Application ---
    if not auto.apply_auto:
        report.message = (
            f"{len(confident)} plan(s) prets, {report.queued} en attente d'arbitrage. "
            "Application automatique desactivee."
        )
        return report

    journal = get_journal()
    trash_root = conf.library_root / TRASH_DIRNAME
    results = [apply_plan(p, journal, dry_run=False, trash_root=trash_root) for p in confident]
    report.applied = sum(1 for r in results if r.ok)

    review.drop_applied([r.plan_id for r in results if r.ok])
    _watcher.mark_processed([p.source for p in plans if p.decision is not Decision.AUTO])

    failed = len(results) - report.applied
    report.message = f"{report.applied} range(s), {report.queued} en attente"
    if failed:
        report.message += f", {failed} en echec"
    return report


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
            _state.running = True
            try:
                report = await run_cycle()
                _state.last = report
                _state.error = None
                _state.history = [report, *_state.history][:20]
                logger.info("cycle automatique : %s", report.message)
            except Exception as exc:
                _state.error = f"{type(exc).__name__}: {exc}"
                logger.exception("le cycle automatique a echoue")
            finally:
                _state.running = False
                _state.last_run = time.time()


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
        "queued": report.queued,
        "message": report.message,
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
