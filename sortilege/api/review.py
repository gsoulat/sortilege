"""File de revue : les fichiers dont le score est ambigu.

La file est vide tant que le pipeline n'est pas complet. Plutot que de la
remplir d'exemples factices, cet endpoint dit precisement ce qui manque : une
UI qui montre de fausses donnees fait perdre bien plus de temps qu'une UI qui
avoue son etat.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..core.scoring import Signals, compute_score

router = APIRouter(prefix="/api/review", tags=["revue"])


def _scoring_ready() -> bool:
    """compute_score est-il implemente ?

    On le teste en l'appelant plutot qu'en inspectant le code : c'est le
    comportement reel qui compte, et ce test passera tout seul le jour ou la
    fonction sera ecrite.
    """
    probe_signals = Signals(
        title_similarity=0.9,
        parse_quality=0.8,
        year_match=True,
        episode_match=None,
        provider_agreement=2,
        provider_popularity=0.5,
        ai_confidence=None,
        ambiguous_candidates=1,
    )
    try:
        compute_score(probe_signals)
    except NotImplementedError:
        return False
    except Exception:
        # Une implementation qui leve n'est pas plus prete qu'une absente : dans
        # les deux cas la file ne peut pas se remplir.
        return False
    return True


@router.get("")
def get_queue() -> dict[str, object]:
    s = get_settings()
    blockers: list[dict[str, str]] = []

    if not _scoring_ready():
        blockers.append(
            {
                "title": "compute_score() n'est pas implémenté",
                "detail": "C'est la fonction qui combine les signaux en une confiance. "
                "Sans elle, aucun fichier ne peut être classé entre application "
                "automatique et revue.",
                "where": "sortilege/core/scoring.py",
            }
        )

    if not s.tmdb_api_key:
        blockers.append(
            {
                "title": "Aucune clé TheMovieDB",
                "detail": "Sans fournisseur de métadonnées, il n'y a aucun candidat "
                "à comparer, donc rien à scorer.",
                "where": "TMDB_API_KEY dans le .env",
            }
        )

    blockers.append(
        {
            "title": "L'assemblage du pipeline reste à écrire",
            "detail": "core/matching.py doit transformer le nom analysé, la sonde du "
            "fichier et les candidats des fournisseurs en un objet Signals.",
            "where": "sortilege/core/matching.py",
        }
    )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "items": [],
        "policy": {
            "auto_apply_threshold": s.auto_apply_threshold,
            "reject_threshold": s.reject_threshold,
        },
    }
