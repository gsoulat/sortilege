"""Vue unique de la mediatheque.

Un seul endpoint agrege ce que trois ecrans montraient separement : la
collection rangee, les fichiers en attente, et les plans calcules. Il ne
declenche RIEN — le scan, l'indexation et le calcul restent des actions
explicites, avec leurs propres endpoints. Celui-ci se contente de dire ou en
sont les choses a l'instant ou on le demande.

C'est ce qui rend l'affichage progressif possible sans machinerie : l'interface
interroge cette route pendant qu'un travail tourne, et voit la liste se remplir.
Pas de flux a maintenir ouvert, pas d'etat partage entre le client et le
serveur — le serveur repond ce qu'il sait, et il en sait un peu plus a chaque
appel.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter

from ..core.planner import Plan
from ..core.workspace import HEAVY_RATIO, WorkspaceEntry, build, summarize
from . import collection, library, review
from .deps import get_journal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workspace", tags=["mediatheque"])

MAX_UNPLANNED_SHOWN = 20
"""Au-dela, une liste de fichiers non identifies cesse d'informer.

Le compte total reste exact ; seuls les exemples sont tronques. Renvoyer six
mille chemins ferait peser plusieurs megaoctets sur chaque rafraichissement,
pour une information que personne ne lit ligne a ligne."""


def _plan_out(plan: Plan, *, with_alternatives: bool = False) -> dict[str, object]:
    """Un plan, tel que la liste l'affiche.

    Les candidats alternatifs ne sont joints QUE pour ce qui demande un
    arbitrage. Ils ne servent qu'a l'ouverture du selecteur, et ils multiplient
    la reponse par neuf : sur trois cents plans, cette route — interrogee
    toutes les deux secondes — transferait quarante megaoctets par minute pour
    des donnees que personne ne regardait.
    """
    out: dict[str, object] = {
        "id": plan.id,
        "source": str(plan.source),
        "destination": str(plan.destination) if plan.destination else None,
        "score": plan.score,
        "decision": str(plan.decision),
        "title": plan.title,
        "year": plan.year,
        "poster_url": plan.poster_url,
        "reasons": plan.reasons,
        "error": plan.error,
        "manual": plan.manual,
    }
    if with_alternatives:
        out["alternatives"] = [
            {
                "provider": c.provider,
                "external_id": c.external_id,
                "title": c.title,
                "year": c.year,
                "poster_url": c.poster_url,
                # Tronque : le selecteur affiche deux lignes, pas un synopsis.
                "overview": c.overview[:200],
            }
            for c in plan.alternatives
        ]
    return out


def _entry_out(entry: WorkspaceEntry) -> dict[str, object]:
    owned = entry.owned
    return {
        "key": entry.key,
        "title": entry.title,
        "kind": entry.kind,
        "year": entry.year,
        "poster_url": entry.poster_url or (owned.poster_url if owned else ""),
        "is_new": entry.is_new,
        # Poids RELATIF au type : une serie de trente episodes pese forcement
        # plus qu'un film, seul le poids d'un fichier se compare.
        "bytes_per_file": entry.bytes_per_file,
        "heaviness": round(entry.heaviness, 2),
        "owned": None
        if owned is None
        else {
            "file_count": owned.file_count,
            "total_bytes": owned.total_bytes,
            "owned_count": owned.owned_count,
            "missing_count": owned.missing_count,
            "identified": owned.identified,
            "seasons": [
                {
                    "number": s.number,
                    "owned": sorted(s.owned),
                    "missing": s.missing,
                    "complete": s.complete,
                }
                for s in sorted(owned.seasons.values(), key=lambda s: s.number)
            ],
            "duplicates": [
                {
                    "label": g.label,
                    "wasted_bytes": g.wasted_bytes,
                    "keep": g.best.relative_path,
                    "redundant": [f.relative_path for f in g.redundant],
                }
                for g in owned.duplicates
            ],
        },
        "pending": {
            "ready": [_plan_out(p) for p in entry.pending.ready],
            "review": [_plan_out(p, with_alternatives=True) for p in entry.pending.review],
            "rejected": [_plan_out(p) for p in entry.pending.rejected],
            "unplanned_count": len(entry.pending.unplanned),
            "unplanned": [
                f.relative_path or f.path.name
                for f in entry.pending.unplanned[:MAX_UNPLANNED_SHOWN]
            ],
            "total": entry.pending.total,
        },
    }


@router.get("")
def read_workspace(limit: int = 200, offset: int = 0) -> dict[str, object]:
    """Etat courant de la mediatheque, oeuvre par oeuvre.

    ``limit`` et ``offset`` bornent les lignes RENVOYEES, jamais celles
    comptees : les compteurs de la barre d'action portent sur la totalite, sans
    quoi « Executer 42 prets » mentirait des que la liste depasse la page.

    La pagination est necessaire moins pour le nombre d'oeuvres que pour ce que
    chacune traine : plans, saisons, doublons. Tout envoyer a chaque
    rafraichissement — toutes les deux secondes — coutait des megaoctets pour
    des lignes hors de l'ecran.
    """
    scan = library.last_scan()
    plans = list(review.current_plans())
    # Les chemins DEJA PASSES par le calcul, et non ceux des plans vivants : un
    # plan applique quitte la file, et s'y fier ferait retomber son fichier
    # dans « pas encore planifie ». Le compteur ne descendait jamais.
    planned = review.planned_paths() | {str(p.source) for p in plans}

    pending = (
        []
        if scan is None
        else [
            f
            for f in scan.files
            if not f.in_library
            and f.skipped_reason is None
            and str(f.path) not in planned
            # Le scan est un instantane : un fichier range ou evacue depuis
            # figure encore dedans. L'annoncer « en attente » ferait promettre
            # un travail qui n'aura pas lieu.
            and f.path.exists()
        ]
    )

    entries = build(collection.current_works(), plans, pending)
    counts = summarize(entries)

    page = entries[offset : offset + max(1, limit)]

    return {
        "counts": counts,
        "offset": offset,
        "limit": limit,
        "total": len(entries),
        "has_more": offset + len(page) < len(entries),
        "heavy_ratio": HEAVY_RATIO,
        # Ce que l'on peut encore defaire. La vue en a besoin pour proposer
        # l'annulation sans imposer un second appel a chaque rafraichissement.
        "journal_size": len(get_journal().read_all()),
        "shown": len(page),
        "works": [_entry_out(e) for e in page],
        # Les trois travaux qui alimentent la vue. L'interface s'en sert pour
        # savoir s'il faut continuer a interroger — et pour dire ce qui tourne.
        "jobs": {
            "scan": library.scan_status(),
            "plan": review.plan_status(),
            "index": collection.status(),
        },
    }
