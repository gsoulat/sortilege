"""File de revue : planifier, arbitrer, appliquer, annuler.

Les plans sont gardes en memoire pour l'instant, comme le dernier scan. C'est
assume et temporaire : rien de precieux ne s'y trouve, puisque le seul etat
durable — ce qui a REELLEMENT ete deplace — vit dans le journal sur disque.
"""

from __future__ import annotations

import logging
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
    deplace pas. Le defaut d'une operation irreversible doit etre l'inaction.

    SORTILEGE_DRY_RUN peut forcer la simulation mais jamais l'inverse."""


class UndoRequest(BaseModel):
    count: int = 1


def _ai_resolver():
    """Construit le resolveur, ou None s'il n'est pas utilisable.

    L'import est tardif : le paquet `anthropic` est une dependance optionnelle,
    et une installation sans lui doit fonctionner normalement plutot que de
    planter a l'import du module.
    """
    conf = get_settings()
    if not conf.ai_enabled or not conf.anthropic_api_key:
        return None
    try:
        from ..core.ai_resolver import AIResolver

        return AIResolver(conf.anthropic_api_key, conf.ai_model)
    except ImportError:
        logger.warning(
            "SORTILEGE_AI_ENABLED=true mais le paquet `anthropic` est absent ; "
            "resolveur IA desactive"
        )
        return None


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
    }


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
        ai_batch_size=conf.ai_batch_size,
    )

    try:
        plans = await pipeline.plan_all(scan.files)
    finally:
        await pipeline.aclose()

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
        "dry_run_locked": conf.dry_run,
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

    # La variable d'environnement ne peut que RENFORCER la simulation, jamais
    # l'inverse : un verrou pose volontairement sur une instance ne doit pas
    # etre contournable par un clic dans l'interface.
    simulate = body.dry_run or conf.dry_run

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
        # L'utilisateur a demande une execution mais la configuration l'a
        # refusee : sans ce drapeau, l'interface annoncerait une simulation
        # sans dire pourquoi.
        "locked": conf.dry_run and not body.dry_run,
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
