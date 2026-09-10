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
from ..core.mediaserver import refresh_library
from ..core.notify import Notification, send
from ..core.preferences import (
    KINDS,
    AISettings,
    AutomationSettings,
    MediaServerSettings,
    NotificationSettings,
    OversizeSettings,
    PreferenceError,
    Preferences,
    QualitySettings,
    TranscodeSettings,
)
from ..core.probe import ffprobe_available
from ..core.quality import STRATEGIES as QUALITY_STRATEGIES
from .deps import get_memory, get_store

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


class AutomationIn(BaseModel):
    enabled: bool | None = None
    interval_minutes: int | None = None
    quiet_seconds: int | None = None
    apply_auto: bool | None = None


class NotificationsIn(BaseModel):
    enabled: bool | None = None
    on_failure: bool | None = None

    webhook_url: str | None = None
    """Ecriture seule, comme la cle d'IA. Absent ou vide = on conserve l'URL
    existante — activer ou couper les notifications ne doit pas obliger a la
    ressaisir. Une chaine « - » vide explicitement le champ."""


class MediaServerIn(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None

    api_key: str | None = None
    """Ecriture seule, comme les autres cles. « - » vide explicitement."""


class QualityIn(BaseModel):
    movie: str | None = None
    episode: str | None = None
    anime: str | None = None

    max_movie_mb: int | None = None
    max_episode_mb: int | None = None
    max_anime_mb: int | None = None


class OversizeIn(BaseModel):
    enabled: bool | None = None
    threshold_gb: float | None = None
    destinations: dict[str, str] | None = None


class TranscodeIn(BaseModel):
    enabled: bool | None = None
    start_hour: int | None = None
    end_hour: int | None = None
    codec: str | None = None
    crf: int | None = None
    preset: str | None = None


class PreferencesIn(BaseModel):
    custom_sources: list[str] | None = None
    enabled_sources: list[str] | None = None
    destinations: dict[str, str] = Field(default_factory=dict)
    templates: dict[str, str] = Field(default_factory=dict)
    ai: AIIn | None = None
    automation: AutomationIn | None = None
    oversize: OversizeIn | None = None
    notifications: NotificationsIn | None = None
    media_server: MediaServerIn | None = None
    quality: QualityIn | None = None
    transcode: TranscodeIn | None = None


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
        "automation": asdict(prefs.automation),
        "quality": asdict(prefs.quality),
        "quality_strategies": [
            {"key": s.key, "label": s.label, "summary": s.summary, "order": list(s.order)}
            for s in QUALITY_STRATEGIES
        ],
        "media_server": {
            "enabled": prefs.media_server.enabled,
            "base_url": prefs.media_server.base_url,
            # La cle ne sort jamais : elle donne acces au serveur multimedia.
            "api_key_set": bool(prefs.media_server.api_key),
        },
        "transcode": {
            "enabled": prefs.transcode.enabled,
            "start_hour": prefs.transcode.start_hour,
            "end_hour": prefs.transcode.end_hour,
            "codec": prefs.transcode.codec,
            "crf": prefs.transcode.crf,
            "preset": prefs.transcode.preset,
        },
        "notifications": {
            "enabled": prefs.notifications.enabled,
            "on_failure": prefs.notifications.on_failure,
            # L'URL ne sort jamais : elle vaut un droit d'ecriture sur le
            # canal, et cette reponse finit dans le cache du navigateur.
            "webhook_set": bool(prefs.notifications.webhook_url),
        },
        "oversize": {
            **asdict(prefs.oversize),
            "resolved": {
                k: str(
                    store.destination_root(k, prefs.oversize.threshold_bytes())
                    if prefs.oversize.enabled
                    else store.destination_root(k)
                )
                for k in KINDS
            },
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

    auto = current.automation
    if body.automation is not None:
        auto = AutomationSettings(
            **{**asdict(current.automation), **body.automation.model_dump(exclude_none=True)}
        )

    over = current.oversize
    if body.oversize is not None:
        patch = body.oversize.model_dump(exclude_none=True)
        destinations = {**current.oversize.destinations, **(patch.pop("destinations", None) or {})}
        over = OversizeSettings(
            **{**asdict(current.oversize), **patch, "destinations": destinations}
        )

    trans = current.transcode
    if body.transcode is not None:
        patch = body.transcode.model_dump(exclude_none=True)
        # Les bornes sont ramenees dans le cadran plutot que refusees : une
        # heure a 25 est une faute de frappe, pas une intention, et bloquer
        # l'enregistrement entier pour ca serait disproportionne.
        for champ in ("start_hour", "end_hour"):
            if champ in patch:
                patch[champ] = max(0, min(23, int(patch[champ])))
        if "crf" in patch:
            # Hors de cette plage, x264 produit soit un fichier enorme, soit une
            # image inutilisable.
            patch["crf"] = max(14, min(30, int(patch["crf"])))
        if patch.get("codec") not in (None, "libx264", "libx265"):
            patch.pop("codec")
        if patch.get("preset") not in (None, "veryfast", "fast", "medium", "slow"):
            patch.pop("preset")
        trans = TranscodeSettings(**{**asdict(current.transcode), **patch})

    notif = current.notifications
    if body.notifications is not None:
        patch = body.notifications.model_dump(exclude_none=True)
        url = patch.pop("webhook_url", "").strip()
        if url == "-":
            patch["webhook_url"] = ""
        elif url:
            patch["webhook_url"] = url
        notif = NotificationSettings(**{**asdict(current.notifications), **patch})

    server = current.media_server
    if body.media_server is not None:
        patch = body.media_server.model_dump(exclude_none=True)
        key = patch.pop("api_key", "").strip()
        if key == "-":
            patch["api_key"] = ""
        elif key:
            patch["api_key"] = key
        server = MediaServerSettings(**{**asdict(current.media_server), **patch})

    qualite = current.quality
    if body.quality is not None:
        # Un budget negatif n'a pas de sens et un budget minuscule rendrait
        # tous les fichiers fautifs : on ramene dans le cadran plutot que de
        # refuser l'enregistrement entier.
        for champ in ("max_movie_mb", "max_episode_mb", "max_anime_mb"):
            valeur = getattr(body.quality, champ, None)
            if valeur is not None:
                setattr(body.quality, champ, max(0, min(200_000, int(valeur))))
        qualite = QualitySettings(
            **{**asdict(current.quality), **body.quality.model_dump(exclude_none=True)}
        )

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
        automation=auto,
        oversize=over,
        notifications=notif,
        transcode=trans,
        media_server=server,
        quality=qualite,
    )

    try:
        store.save(merged)
    except PreferenceError as exc:
        # 400 et non 500 : c'est une saisie a corriger, pas une panne.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return read_preferences()


@router.post("/notifications/test")
async def test_notification() -> dict[str, object]:
    """Envoie un message d'essai dans le canal configure.

    Un webhook peut etre valide de forme et revoque cote Discord ; seul un
    envoi reel le dit. On rapporte l'echec au lieu de l'avaler, contrairement
    aux notifications de cycle : ici l'utilisateur attend une reponse.
    """
    prefs = get_store().load().notifications
    if not prefs.webhook_url:
        raise HTTPException(status_code=400, detail="Aucun webhook enregistre.")

    ok = await send(
        prefs.webhook_url,
        Notification(
            title="Sortilège est bien relié",
            body="Ce canal recevra le compte rendu des cycles automatiques.",
        ),
    )
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="Discord n'a pas accepte le message. Verifie que le webhook existe toujours.",
        )
    return {"sent": True}


@router.post("/media-server/test")
async def test_media_server() -> dict[str, object]:
    """Demande un rafraichissement d'essai. Rapporte l'echec au lieu de l'avaler."""
    prefs = get_store().load().media_server
    if not prefs.base_url or not prefs.api_key:
        raise HTTPException(status_code=400, detail="Adresse ou cle d'API manquante.")

    if not await refresh_library(prefs.base_url, prefs.api_key):
        raise HTTPException(
            status_code=502,
            detail=(
                "Le serveur n'a pas repondu. Verifie l'adresse, la cle d'API, "
                "et que le serveur est bien sur le reseau local."
            ),
        )
    return {"refreshed": True}


@router.get("/decisions")
def read_decisions() -> dict[str, object]:
    """Identifications retenues, les plus recentes d'abord."""
    return {
        "decisions": [
            {
                "kind": d.kind,
                "title_key": d.title_key,
                "title": d.title,
                "year": d.year,
                "provider": d.provider,
                "external_id": d.external_id,
                "poster_url": d.poster_url,
                "hits": d.hits,
                "created_at": d.created_at,
            }
            for d in get_memory().decisions()
        ]
    }


@router.delete("/decisions/{kind}/{title_key}")
def forget_decision(kind: str, title_key: str) -> dict[str, object]:
    """Oublie un choix : la question sera reposee au prochain scan."""
    removed = get_memory().forget(kind, title_key)
    if not removed:
        raise HTTPException(status_code=404, detail="Aucune decision pour ce titre.")
    return read_decisions()


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
