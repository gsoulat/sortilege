"""File de revue : planifier, arbitrer, appliquer, annuler.

Les plans sont gardes en memoire pour l'instant, comme le dernier scan. C'est
assume et temporaire : rien de precieux ne s'y trouve, puisque le seul etat
durable — ce qui a REELLEMENT ete deplace — vit dans le journal sur disque.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from threading import Lock

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..core.companions import TRASH_DIRNAME
from ..core.journal import apply_plan, undo_last
from ..core.pipeline import Pipeline
from ..core.planner import Plan
from ..core.scoring import Decision, Policy
from ..providers.anilist import AniListProvider
from ..providers.tmdb import TMDBProvider
from .deps import get_journal, get_store
from .library import last_scan

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/review", tags=["revue"])

_plans: dict[str, Plan] = {}
_lock = Lock()


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
    }


@router.get("/plan/status")
def plan_status() -> dict[str, object]:
    return _plan_status()


@router.post("/plan")
async def build_plans() -> dict[str, object]:
    """Confronte le dernier scan aux fournisseurs et calcule les plans."""
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
        policy=Policy(
            auto_apply_threshold=conf.auto_apply_threshold,
            reject_threshold=conf.reject_threshold,
        ),
        ai=_ai_resolver(),
        ai_batch_size=prefs.ai.batch_size,
        ai_threshold=prefs.ai.threshold,
    )

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

    try:
        plans = await pipeline.plan_all(scan.files, on_progress=progress)
    except Exception as exc:
        _job.error = f"{type(exc).__name__}: {exc}"
        logger.exception("le calcul des plans a echoue")
        raise HTTPException(status_code=500, detail=_job.error) from exc
    finally:
        await pipeline.aclose()
        _job.running = False
        _job.finished_at = time.monotonic()
        _job.current = ""

    with _lock:
        _plans.clear()
        _plans.update({p.id: p for p in plans})

    logger.info("plans calcules : %s", len(plans))
    return _queue()


@router.get("")
def get_queue() -> dict[str, object]:
    return _queue()


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
    }


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


@router.post("/apply")
def apply(body: ApplyRequest) -> dict[str, object]:
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
            }
            for r in results
        ],
    }


class ChooseRequest(BaseModel):
    provider: str
    external_id: str


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
    scanned = next((f for f in (scan.files if scan else []) if f.path == plan.source), None)
    if scanned is None:
        raise HTTPException(
            status_code=409,
            detail="Le fichier n'est plus dans le dernier scan. Relance un scan.",
        )

    store = get_store()
    prefs = store.load()
    pipeline = Pipeline(
        tmdb=TMDBProvider(conf.tmdb_api_key) if conf.tmdb_api_key else None,
        anilist=AniListProvider(),
        library_root=conf.library_root,
        templates={k: prefs.template_for(k) for k in ("movie", "episode", "anime")},
        policy=Policy(
            auto_apply_threshold=conf.auto_apply_threshold,
            reject_threshold=conf.reject_threshold,
        ),
    )

    try:
        rebuilt = await pipeline.replan_with(scanned, chosen, plan)
    finally:
        await pipeline.aclose()

    with _lock:
        _plans.pop(plan_id, None)
        _plans[rebuilt.id] = rebuilt

    return {"plan": _plan_out(rebuilt), "queue": _queue()}


@router.post("/undo")
def undo(body: UndoRequest) -> dict[str, object]:
    results = undo_last(get_journal(), max(1, body.count))
    return {
        "undone": sum(1 for r in results if r.ok),
        "failed": sum(1 for r in results if not r.ok),
        "results": [
            {"ok": r.ok, "from": r.source, "to": r.destination, "message": r.message}
            for r in results
        ],
        "journal_size": len(get_journal().read_all()),
    }


@router.get("/journal")
def read_journal(limit: int = 50) -> dict[str, object]:
    records = get_journal().read_all()
    recent = records[-limit:][::-1]
    return {
        "total": len(records),
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
