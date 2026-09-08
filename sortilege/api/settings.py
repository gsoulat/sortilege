"""Reglages et diagnostics.

Deux natures de reglages, deliberement separees :

- **Deploiement** (lecture seule) : points de montage, cles d'API, secrets. Ils
  viennent de l'environnement et doivent correspondre aux volumes du
  conteneur ; les modifier depuis l'UI produirait une configuration qui ne
  survit pas a un redemarrage.
- **Usage** (modifiable) : quelles sources scanner, ou ranger chaque type de
  media, avec quel gabarit. Ces choix appartiennent a l'utilisateur.

Aucune valeur secrete n'est renvoyee, seulement des booleens « configure ou
non » : cette reponse traverse le reseau et finit dans la console du
navigateur.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..core.ai import PROVIDERS
from ..core.preferences import KINDS, AISettings, PreferenceError, Preferences
from ..core.probe import ffprobe_available
from .deps import get_store

router = APIRouter(prefix="/api/settings", tags=["reglages"])


class AIIn(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    threshold: float | None = None
    batch_size: int | None = None

    api_key: str | None = None
    """Ecriture seule. Absent ou vide = on conserve la cle existante, ce qui
    permet de modifier le modele ou le seuil sans avoir a la ressaisir — et
    sans qu'elle ait a transiter une seconde fois."""


class PreferencesIn(BaseModel):
    custom_sources: list[str] | None = None
    enabled_sources: list[str] | None = None
    destinations: dict[str, str] = Field(default_factory=dict)
    templates: dict[str, str] = Field(default_factory=dict)
    ai: AIIn | None = None


@router.get("/preferences")
def read_preferences() -> dict[str, object]:
    """Preferences courantes, et les choix possibles pour les alimenter."""
    conf = get_settings()
    store = get_store()
    prefs = store.load()

    mounted = {str(p) for p in conf.source_roots}

    return {
        "custom_sources": prefs.custom_sources,
        "enabled_sources": prefs.enabled_sources,
        "destinations": prefs.destinations,
        "templates": {k: prefs.template_for(k) for k in KINDS},
        "available_sources": [
            {
                "path": str(p),
                "exists": p.is_dir(),
                # Une racine montee ne peut pas etre retiree depuis l'interface :
                # elle vient du docker-compose, pas des preferences.
                "mounted": str(p) in mounted,
            }
            for p in store.all_sources(prefs)
        ],
        "library_root": str(conf.library_root),
        "resolved_destinations": {k: str(store.destination_root(k)) for k in KINDS},
        "ai": {
            "enabled": prefs.ai.enabled,
            "provider": prefs.ai.provider,
            "model": prefs.ai.model,
            "base_url": prefs.ai.base_url,
            "threshold": prefs.ai.threshold,
            "batch_size": prefs.ai.batch_size,
            # La cle ne sort jamais : cette reponse finit dans la console du
            # navigateur et dans son cache.
            "api_key_set": bool(prefs.ai.api_key),
        },
        "ai_providers": [
            {
                "key": p.key,
                "label": p.label,
                "base_url": p.base_url,
                "default_model": p.default_model,
                "needs_key": p.needs_key,
                "hint": p.hint,
            }
            for p in PROVIDERS
        ],
    }


@router.put("/preferences")
def write_preferences(body: PreferencesIn) -> dict[str, object]:
    store = get_store()
    current = store.load()

    # None = « ne touche pas a ce champ ». Une liste vide reste une valeur
    # significative (aucune source ajoutee, ou toutes les sources activees).
    ai = current.ai
    if body.ai is not None:
        patch = body.ai.model_dump(exclude_none=True)
        # Une cle vide signifie « ne change pas », pas « efface » : l'interface
        # renvoie le formulaire entier a chaque enregistrement et ne connait
        # pas la valeur actuelle.
        if not patch.get("api_key"):
            patch.pop("api_key", None)
        ai = AISettings(**{**asdict(current.ai), **patch})

    merged = Preferences(
        custom_sources=(
            current.custom_sources if body.custom_sources is None else body.custom_sources
        ),
        enabled_sources=(
            current.enabled_sources if body.enabled_sources is None else body.enabled_sources
        ),
        destinations={**current.destinations, **body.destinations},
        templates={**current.templates, **body.templates},
        ai=ai,
    )

    try:
        store.save(merged)
    except PreferenceError as exc:
        # 400 et non 500 : c'est une saisie a corriger, pas une panne.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return read_preferences()


@router.get("/browse")
def browse(path: str | None = None) -> dict[str, object]:
    """Sous-dossiers d'un chemin, pour choisir une source sans la saisir.

    Confine aux racines montees : impossible de remonter au-dessus.
    """
    try:
        return get_store().browse(path)
    except PreferenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def read_settings() -> dict[str, object]:
    s = get_settings()

    return {
        "paths": {
            "source_roots": [str(p) for p in s.source_roots],
            "library_root": str(s.library_root),
        },
        "behaviour": {
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

    return checks
