"""Reglages et diagnostics.

Volontairement en LECTURE SEULE. Toute la configuration vient de
l'environnement : c'est ce qui permet de reconstruire le conteneur a
l'identique et d'auditer un deploiement depuis le seul .env. Une UI qui
ecrirait ailleurs creerait un second etat de verite, invisible dans le compose.

Aucune valeur secrete n'est renvoyee, seulement des booleens « configure ou
non » : cette reponse traverse le reseau et finit dans la console du
navigateur.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..config import get_settings
from ..core.probe import ffprobe_available

router = APIRouter(prefix="/api/settings", tags=["reglages"])


@router.get("")
def read_settings() -> dict[str, object]:
    s = get_settings()

    return {
        "paths": {
            "source_roots": [str(p) for p in s.source_roots],
            "library_root": str(s.library_root),
        },
        "behaviour": {
            "dry_run": s.dry_run,
            "auto_apply_threshold": s.auto_apply_threshold,
            "reject_threshold": s.reject_threshold,
        },
        "providers": [
            {
                "name": "TheMovieDB",
                "role": "Films et séries",
                "configured": bool(s.tmdb_api_key),
                "required": True,
                "hint": "TMDB_API_KEY",
            },
            {
                "name": "AniList",
                "role": "Animes",
                "configured": True,
                "required": False,
                "hint": "Aucune clé requise",
            },
            {
                "name": "TheTVDB",
                "role": "Séries (source secondaire)",
                "configured": bool(s.tvdb_api_key),
                "required": False,
                "hint": "TVDB_API_KEY",
            },
        ],
        "ai": {
            "enabled": s.ai_enabled,
            "configured": bool(s.anthropic_api_key),
            "model": s.ai_model,
            "batch_size": s.ai_batch_size,
        },
        "diagnostics": _diagnostics(s),
    }


def _diagnostics(s) -> list[dict[str, object]]:
    """Etat de sante lisible : ce qui marche, ce qui manque, et l'effet concret.

    Chaque entree dit ce qui est degrade, pas seulement ce qui est absent — un
    « ffprobe manquant » sans consequence enoncee n'aide personne a decider
    s'il faut agir.
    """
    checks: list[dict[str, object]] = []

    ffprobe = ffprobe_available()
    checks.append(
        {
            "name": "ffprobe",
            "ok": ffprobe,
            "detail": "Durée, résolution réelle et tags des conteneurs lisibles"
            if ffprobe
            else "Absent : identification privée de la durée et des tags, "
            "donc plus de fichiers en revue manuelle",
        }
    )

    roots_ok = all(p.is_dir() for p in s.source_roots) if s.source_roots else False
    checks.append(
        {
            "name": "Racines sources",
            "ok": roots_ok,
            "detail": "Accessibles"
            if roots_ok
            else "Introuvables ou non montées : le scan ne trouvera rien",
        }
    )

    library_ok = s.library_root.is_dir()
    checks.append(
        {
            "name": "Racine de bibliothèque",
            "ok": library_ok,
            "detail": "Accessible" if library_ok else "Introuvable ou non montée",
        }
    )

    checks.append(
        {
            "name": "TheMovieDB",
            "ok": bool(s.tmdb_api_key),
            "detail": "Clé présente"
            if s.tmdb_api_key
            else "Sans clé, aucun candidat n'est proposé et rien ne peut être identifié",
        }
    )

    checks.append(
        {
            "name": "Mode simulation",
            "ok": True,
            "detail": "Actif : aucun fichier ne sera déplacé"
            if s.dry_run
            else "Désactivé : les fichiers seront réellement déplacés",
        }
    )

    return checks
