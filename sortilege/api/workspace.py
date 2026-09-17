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
from pathlib import Path
from typing import Literal

from fastapi import APIRouter

from ..core import quality
from ..core.planner import Plan
from ..core.workspace import (
    HEAVY_RATIO,
    WorkspaceEntry,
    build,
    in_space,
    summarize,
    tab_counts,
)
from . import collection, library, review
from .deps import get_journal, get_store

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
        # Le type LU pour ce fichier, qui peut differer de celui de l'oeuvre :
        # c'est justement lui qu'on corrige quand une serie a ete prise pour un
        # film.
        "kind": plan.kind,
        "title": plan.title,
        "year": plan.year,
        "poster_url": plan.poster_url,
        "reasons": plan.reasons,
        "error": plan.error,
        "manual": plan.manual,
        # D'ou vient l'identification. Un score nu demande de faire confiance
        # sans savoir a qui.
        "identified_by": plan.identified_by,
        # Le dernier rangement refuse, et pourquoi. Absent, un fichier en echec
        # redevenait un « pret » muet des le rechargement suivant.
        "failure": plan.apply_failure,
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


def _source_bytes(entry: WorkspaceEntry, sizes: dict[str, int]) -> int:
    """Poids de ce qui reste a ranger pour cette oeuvre.

    La taille vient du scan, deja en memoire. Un plan dont la source n'y figure
    pas -- calcule par le cycle automatique sur son propre scan -- est mesure
    sur le disque ; un fichier introuvable pese zero plutot que de faire
    echouer toute la liste.
    """
    total = sum(f.size_bytes for f in entry.pending.unplanned)
    for plan in entry.pending.plans:
        taille = sizes.get(str(plan.source))
        if taille is None:
            try:
                taille = Path(plan.source).stat().st_size
            except OSError:
                taille = 0
        total += taille
    return total


def _source_out(entry: WorkspaceEntry, sizes: dict[str, int]) -> dict[str, object]:
    """Les chiffres de la SOURCE pour une oeuvre, ceux que Ranger affiche.

    Separes de ``owned`` plutot que deduits a l'ecran : sur une oeuvre a la fois
    possedee et en attente, Ranger montrait « 28 fichiers · 49,8 Go » -- la
    mediatheque -- pour quatre episodes a ranger.
    """
    pending = entry.pending
    owned = entry.owned
    return {
        "file_count": pending.total,
        "total_bytes": _source_bytes(entry, sizes),
        "ready": len(pending.ready),
        "review": len(pending.review) + len(pending.rejected),
        "unplanned": len(pending.unplanned),
        "failed": pending.failed,
        # De quoi arbitrer sans quitter Ranger : ce qui est deja la. Un compte,
        # pas les totaux de la mediatheque.
        "in_library": None
        if owned is None
        else {"files": owned.file_count, "episodes": owned.owned_count},
    }


def _entry_out(entry: WorkspaceEntry, sizes: dict[str, int]) -> dict[str, object]:
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
        # « 2,5 fois le poids habituel » sur une serie de neuf episodes ne dit
        # pas QUEL episode est en cause. Sans le nom du fichier, l'avertissement
        # se regarde sans rien pouvoir en faire — et c'est au fichier qu'on agit.
        "heavy_files": [
            {"path": f.relative_path, "size_bytes": f.size_bytes, "ratio": round(f.ratio, 1)}
            for f in entry.heavy_files
        ],
        # Distinct du surpoids : un episode peut peser le double des autres tout
        # en respectant la strategie, et un fichier parfaitement dans la moyenne
        # peut etre en 2160p alors que la strategie demande du 1080p.
        "off_strategy": [
            {
                "path": c.relative_path,
                "resolution": c.resolution,
                "target": c.target,
                "size_bytes": c.size_bytes,
                "savings_bytes": c.savings_bytes,
                "reason": c.reason,
            }
            for c in entry.off_strategy
        ],
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
            # Les fichiers eux-memes, pour les livres seulement : c'est ce qui
            # permet d'ouvrir le lecteur. Les exposer pour tous les types
            # gonflerait la reponse — interrogee toutes les deux secondes — de
            # milliers de chemins que personne n'affiche.
            "books": (
                [
                    {"path": f.relative_path, "size_bytes": f.size_bytes}
                    for refs in owned.slots.values()
                    for f in refs
                ]
                if owned.kind == "book"
                else []
            ),
            "duplicates": [
                {
                    "label": g.label,
                    "wasted_bytes": g.wasted_bytes,
                    "keep": g.best.relative_path,
                    "redundant": [f.relative_path for f in g.redundant],
                    # Le detail de chaque exemplaire, pour que le choix soit
                    # VERIFIABLE. « garde celui-ci » sans dire ce qu'il vaut ni
                    # ce que valent les autres demande une confiance aveugle
                    # avant une suppression.
                    "files": [
                        {
                            "path": f.relative_path,
                            "resolution": f.resolution,
                            "size_bytes": f.size_bytes,
                            "kept": f is g.best,
                        }
                        for f in sorted(g.files, key=lambda f: f.rank, reverse=True)
                    ],
                    "strategy": quality.strategy(g.best.strategy_key).label,
                }
                for g in owned.duplicates
            ],
        },
        "source": _source_out(entry, sizes),
        "pending": {
            "ready": [_plan_out(p) for p in entry.pending.ready],
            "review": [_plan_out(p, with_alternatives=True) for p in entry.pending.review],
            # Les ecartes portent AUSSI leurs alternatives : ils sont
            # arbitrables au meme titre que les autres. Un plan ecarte sans
            # alternative a proposer serait une impasse — le fichier resterait
            # indefiniment dans la source, sans rien pour le sortir de la.
            "rejected": [_plan_out(p, with_alternatives=True) for p in entry.pending.rejected],
            "unplanned_count": len(entry.pending.unplanned),
            "unplanned": [
                f.relative_path or f.path.name
                for f in entry.pending.unplanned[:MAX_UNPLANNED_SHOWN]
            ],
            "total": entry.pending.total,
        },
    }


@router.get("")
def read_workspace(
    limit: int = 200,
    offset: int = 0,
    espace: Literal["all", "source", "library"] = "all",
) -> dict[str, object]:
    """Etat courant de la mediatheque, oeuvre par oeuvre.

    ``espace`` choisit les lignes : ``source`` (Ranger, ce qui reste a ranger),
    ``library`` (Ma mediatheque, ce qu'on possede), ``all`` (tout, le defaut).
    Le tri se fait ICI, avant la pagination : l'ecran le faisait sur une page
    deja tronquee et melangee, d'ou un onglet Ranger rempli d'oeuvres de la
    mediatheque et des compteurs de type qui comptaient les deux. ``tab``
    compte les lignes de cet espace ; ``counts`` reste global, pour la barre
    d'action.

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
    # Seulement les plans VIVANTS, et un fichier encore present sur le disque :
    # la regle est celle de « Identifier », partagee et non recopiee. Leur
    # divergence laissait des fichiers comptes « a identifier » que le calcul
    # refusait de reprendre.
    pending = [] if scan is None else review.awaiting_identification(scan.files, plans)
    sizes = {} if scan is None else {str(f.path): f.size_bytes for f in scan.files}

    reglages = get_store().load().quality
    entries = build(
        collection.current_works(),
        plans,
        pending,
        reglages,
    )
    counts = summarize(entries)
    lignes = [e for e in entries if in_space(e, espace)]

    page = lignes[offset : offset + max(1, limit)]

    return {
        "counts": counts,
        # La strategie de chaque type, en clair : « supprimer les doublons
        # selon la strategie » sans dire laquelle demande une confiance
        # aveugle avant une suppression.
        "strategies": {
            kind: quality.strategy(getattr(reglages, kind)).label
            for kind in ("movie", "episode", "anime")
            if any(e.owned and e.owned.kind == kind for e in entries)
        },
        "espace": espace,
        "tab": tab_counts(lignes),
        # Fichiers ranges que l'index ne montre pas encore. Sans ce compte, ce
        # qui venait de quitter Ranger n'apparaissait nulle part.
        "ranged_since_index": collection.ranged_since_index(),
        "offset": offset,
        "limit": limit,
        "total": len(lignes),
        "has_more": offset + len(page) < len(lignes),
        "heavy_ratio": HEAVY_RATIO,
        # Ce que l'on peut encore defaire. La vue en a besoin pour proposer
        # l'annulation sans imposer un second appel a chaque rafraichissement.
        "journal_size": len(get_journal().read_all()),
        "shown": len(page),
        "works": [_entry_out(e, sizes) for e in page],
        # Ce qui empeche l'outil de produire quoi que ce soit. Joint ICI plutot
        # que laisse a un second appel : une liste vide et sa raison doivent
        # arriver ensemble, sinon l'ecran affirme « rien a ranger » pendant tout
        # l'intervalle qui separe les deux requetes.
        "blockers": review.blockers(),
        # Ce que le dernier scan n'a PAS pu faire. Une liste vide se lit « c'est
        # deja range » ; ces deux champs disent quand elle se lit plutot « on n'a
        # pas regarde la ou tu crois ».
        "diagnostics": {
            "scanned": scan is not None,
            "errors": list(scan.errors) if scan else [],
            "skipped": scan.skipped if scan else 0,
        },
        # Les trois travaux qui alimentent la vue. L'interface s'en sert pour
        # savoir s'il faut continuer a interroger — et pour dire ce qui tourne.
        "jobs": {
            "scan": library.scan_status(),
            "plan": review.plan_status(),
            "index": collection.status(),
        },
    }
