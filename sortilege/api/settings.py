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

import asyncio
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from ..config import get_settings
from ..core import apikey
from ..core.ai import PROVIDERS, resolver_status
from ..core.mediaserver import refresh_library
from ..core.nfo import ON_EXISTING_CHOICES, LocalMetadataSettings
from ..core.notify import Notification, send
from ..core.preferences import (
    KINDS,
    AISettings,
    AutomationSettings,
    MediaServerSettings,
    MetadataSettings,
    NotificationSettings,
    OversizeSettings,
    PreferenceError,
    Preferences,
    QualitySettings,
    ScanSettings,
    SubtitleSettings,
    TranscodeSettings,
    VpnSettings,
)
from ..core.probe import ffprobe_available
from ..core.quality import STRATEGIES as QUALITY_STRATEGIES
from ..core.vpn import POLICIES as VPN_POLICIES
from ..core.vpn import check as check_vpn
from ..core.vpn import egress_allowed
from ..core.vpn import reset_cache as reset_vpn_cache
from ..providers.base import forget_auth_errors, last_auth_error
from ..providers.tmdb import TMDBProvider
from .deps import get_memory, get_store, tmdb_key, tmdb_language

router = APIRouter(prefix="/api/settings", tags=["reglages"])


class AIIn(BaseModel):
    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    threshold: float | None = None

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


class MetadataIn(BaseModel):
    language: str | None = None

    tmdb_api_key: str | None = None
    """Ecriture seule, comme les autres cles. Absent ou vide = on conserve
    celle qui est enregistree — changer la langue ne doit pas obliger a
    ressaisir la cle. Une chaine « - » vide explicitement le champ, ce qui fait
    retomber l'application sur TMDB_API_KEY si le .env en porte une."""


class ScanIn(BaseModel):
    min_size_mb: int | None = None
    extra_skip_dirs: list[str] | None = None
    extra_skip_hints: list[str] | None = None


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


class LocalMetadataIn(BaseModel):
    nfo: bool | None = None
    artwork: bool | None = None
    opf: bool | None = None
    on_existing: str | None = None


class SubtitlesIn(BaseModel):
    enabled: bool | None = None
    languages: list[str] | None = None
    overwrite: bool | None = None
    audio_is_enough: bool | None = None

    opensubtitles_api_key: str | None = None
    """Ecriture seule, meme convention que les autres cles : vide = conserver,
    « - » = effacer."""

    opensubtitles_token: str | None = None


class VpnIn(BaseModel):
    policy: str | None = None
    reference_ip: str | None = None


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
    metadata: MetadataIn | None = None
    scan: ScanIn | None = None
    quality: QualityIn | None = None
    transcode: TranscodeIn | None = None
    local_metadata: LocalMetadataIn | None = None
    subtitles: SubtitlesIn | None = None
    vpn: VpnIn | None = None


def _ai_ready(prefs: Preferences) -> dict[str, object]:
    if not prefs.ai.enabled:
        return {"ok": False, "reason": "résolveur désactivé"}
    ok, motif = resolver_status(
        prefs.ai.provider, prefs.ai.api_key, prefs.ai.model, prefs.ai.base_url
    )
    return {"ok": ok, "reason": motif}


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
            # La cle ne sort jamais : cette reponse finit dans la console du
            # navigateur et dans son cache.
            "api_key_set": bool(prefs.ai.api_key),
        },
        "metadata": {
            "language": prefs.metadata.language,
            # La cle ne sort jamais, comme les autres. On dit en revanche D'OU
            # elle vient : sans cela, une installation qui la porte dans son
            # .env afficherait un champ vide a cote d'une application qui
            # identifie parfaitement, et le premier reflexe serait de la
            # ressaisir.
            "tmdb_key_set": bool(prefs.metadata.tmdb_api_key),
            "tmdb_key_from_env": bool(
                not prefs.metadata.tmdb_api_key.strip() and conf.tmdb_api_key.strip()
            ),
        },
        "scan": asdict(prefs.scan),
        "automation": asdict(prefs.automation),
        "quality": asdict(prefs.quality),
        "ai_models": {p.key: list(p.models) for p in PROVIDERS},
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
        # Pourquoi le resolveur ne fait rien, le cas echeant. Cocher « activer »
        # et ne rien voir se passer ne laisse aucun moyen de distinguer une cle
        # manquante d'un lot de fichiers trop clairs pour meriter un appel.
        "ai_ready": _ai_ready(prefs),
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
        "local_metadata": {
            **asdict(prefs.local_metadata),
            # Les conduites possibles voyagent avec la valeur : l'interface ne
            # doit pas avoir a redupliquer une liste qui vit dans core/nfo.
            "on_existing_choices": list(ON_EXISTING_CHOICES),
        },
        "subtitles": {
            "enabled": prefs.subtitles.enabled,
            "languages": prefs.subtitles.languages,
            "overwrite": prefs.subtitles.overwrite,
            "audio_is_enough": prefs.subtitles.audio_is_enough,
            # Les cles ne sortent jamais, comme partout ailleurs.
            "opensubtitles_key_set": bool(prefs.subtitles.opensubtitles_api_key),
            "opensubtitles_token_set": bool(prefs.subtitles.opensubtitles_token),
        },
        "vpn": {
            "policy": str(prefs.vpn.policy),
            "reference_ip": prefs.vpn.reference_ip,
            # Les politiques voyagent avec la valeur, avec la phrase qui dit ce
            # que chacune fait : « exiger » refuse d'emettre, et personne ne
            # doit avoir a le deviner d'apres le mot.
            "policies": [
                {"key": str(c.key), "label": c.label, "summary": c.summary} for c in VPN_POLICIES
            ],
        },
        "integration": {
            # Masquee ICI, en clair sur sa route dediee. Cette reponse-la est
            # demandee a chaque ouverture de l'ecran et finit dans le cache du
            # navigateur ; la cle ne doit pas y trainer alors que personne ne
            # la lit dans neuf ouvertures sur dix.
            "api_key_masked": apikey.masked(prefs.integration.api_key),
            "api_key_set": bool(prefs.integration.api_key),
            "header": apikey.HEADER,
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

    meta = current.metadata
    if body.metadata is not None:
        patch = body.metadata.model_dump(exclude_none=True)
        # Exactement le motif du serveur multimedia : une cle vide signifie
        # « ne change pas », parce que l'interface renvoie le formulaire entier
        # a chaque enregistrement sans connaitre la valeur actuelle. « - » est
        # le seul moyen de dire « efface » — sans lui, une cle saisie par
        # erreur ne pourrait plus jamais etre retiree depuis l'interface.
        key = patch.pop("tmdb_api_key", "").strip()
        if key == "-":
            patch["tmdb_api_key"] = ""
        elif key:
            patch["tmdb_api_key"] = key
        if "language" in patch:
            patch["language"] = patch["language"].strip()
        meta = MetadataSettings(**{**asdict(current.metadata), **patch})

    parcours = current.scan
    if body.scan is not None:
        patch = body.scan.model_dump(exclude_none=True)
        for champ in ("extra_skip_dirs", "extra_skip_hints"):
            if champ in patch:
                # Les lignes vides d'un champ multiligne sont un accident de
                # saisie, pas une exclusion sur « » — qui matcherait tout.
                patch[champ] = [v.strip() for v in patch[champ] if v.strip()]
        parcours = ScanSettings(**{**asdict(current.scan), **patch})

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

    fiches = current.local_metadata
    if body.local_metadata is not None:
        patch = body.local_metadata.model_dump(exclude_none=True)
        if "on_existing" in patch:
            patch["on_existing"] = patch["on_existing"].strip().lower()
        fiches = LocalMetadataSettings(**{**asdict(current.local_metadata), **patch})

    sous_titres = current.subtitles
    if body.subtitles is not None:
        patch = body.subtitles.model_dump(exclude_none=True)
        # Meme convention que les autres cles : vide = conserver, « - » =
        # effacer. Sans cela, changer de langue obligerait a ressaisir la cle.
        for champ in ("opensubtitles_api_key", "opensubtitles_token"):
            valeur = patch.pop(champ, "").strip()
            if valeur == "-":
                patch[champ] = ""
            elif valeur:
                patch[champ] = valeur
        if "languages" in patch:
            patch["languages"] = [v.strip() for v in patch["languages"] if v.strip()]
        sous_titres = SubtitleSettings(**{**asdict(current.subtitles), **patch})

    tunnel = current.vpn
    if body.vpn is not None:
        patch = body.vpn.model_dump(exclude_none=True)
        if "reference_ip" in patch:
            # « - » efface le temoin, comme « - » efface une cle. Sans lui, une
            # adresse relevee par erreur resterait pour toujours et fausserait
            # tous les verdicts.
            valeur = patch["reference_ip"].strip()
            patch["reference_ip"] = "" if valeur == "-" else valeur
        tunnel = VpnSettings(**{**asdict(current.vpn), **patch})

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
        metadata=meta,
        scan=parcours,
        quality=qualite,
        # Repris tel quel et jamais issu du formulaire : la cle d'API se
        # regenere par sa propre route. L'oublier ici la remettrait a vide au
        # premier enregistrement de reglages, et tous les scripts branches
        # dessus tomberaient sans que rien ne les ait touches.
        integration=current.integration,
        local_metadata=fiches,
        subtitles=sous_titres,
        vpn=tunnel,
    )

    try:
        store.save(merged)
    except PreferenceError as exc:
        # 400 et non 500 : c'est une saisie a corriger, pas une panne.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.vpn is not None:
        # La derniere mesure a ete jugee sous l'ancien reglage. Changer de
        # politique ou de temoin doit valoir tout de suite, pas une minute plus
        # tard quand le cache expire.
        reset_vpn_cache()

    return read_preferences()


def _api_key_out(cle: str) -> dict[str, object]:
    return {
        "api_key": cle,
        "header": apikey.HEADER,
        "masked": apikey.masked(cle),
        "example": (
            f'curl -X POST "http://NAS:8117/api/integration/download-complete" '
            f'-H "{apikey.HEADER}: {cle}" '
            f'-H "Content-Type: application/json" '
            f'-d \'{{"path": "/downloads/Nom.Du.Transfert"}}\''
        ),
    }


def _sans_cache(response: Response) -> None:
    """Interdit toute mise en cache d'une reponse qui porte la cle en clair.

    La cle vaut le mot de passe. Sans ces en-tetes, un navigateur ou un proxy
    intermediaire peut la conserver sur disque, et la relire apres une
    regeneration qui la croyait revoquee. « Pragma » couvre les caches HTTP/1.0
    qui ignorent « Cache-Control ».
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _cle_ou_refus(obtenir) -> str:
    """Obtient la cle, en traduisant un refus du magasin en 400 lisible."""
    try:
        return obtenir()
    except PreferenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api-key")
def read_api_key(response: Response) -> dict[str, object]:
    """Rend la cle EN CLAIR, contrairement a toutes les autres.

    C'est une exception assumee, pas un oubli : les autres cles appartiennent a
    un service tiers et n'ont a sortir nulle part, celle-ci n'existe que pour
    etre recopiee dans un script ou dans un client de telechargement. Une cle
    qu'on ne peut pas lire est une fonction qui n'existe pas.

    L'exposition reste bornee : cette route est appelee sur un geste explicite,
    pas au chargement de l'ecran ; il faut deja une session pour la demander,
    donc deja tout pouvoir ; et la cle se revoque en un clic par la route
    voisine. La ligne `curl` complete est renvoyee avec elle — c'est ce qu'on
    va coller, et la reconstruire a la main est le seul endroit ou l'en-tete
    peut etre mal ecrit.
    """
    _sans_cache(response)
    return _api_key_out(_cle_ou_refus(get_store().ensure_api_key))


@router.post("/api-key/regenerate")
def regenerate_api_key(response: Response) -> dict[str, object]:
    """Remplace la cle. Tout ce qui utilisait l'ancienne cesse d'etre accepte.

    Reponse a une cle divulguee — un depot public, un historique de shell
    partage. Sans elle, revoquer un acces imposerait de changer le mot de passe
    de l'installation dans le .env et de redemarrer la pile.
    """
    _sans_cache(response)
    return _api_key_out(_cle_ou_refus(get_store().rotate_api_key))


async def _exige_la_sortie() -> None:
    """Refuse un bouton d'essai quand la politique VPN interdit d'emettre.

    Un essai est un appel sortant comme un autre : il revele l'adresse de la
    maison au service interroge. Sous « exiger », le laisser passer ferait du
    bouton la fuite exacte que la politique existe pour empecher — et la plus
    facile a declencher, puisqu'on le presse justement pour voir si ca marche.

    Place APRES les controles locaux (cle absente, resolveur coupe) : ceux-la
    n'emettent rien et disent quelque chose de plus utile. 409 et non 400, comme
    pour l'identification : la demande est valable, c'est l'etat du moment qui
    s'y oppose.
    """
    reglage = get_store().load().vpn
    decision = await egress_allowed(reglage.policy, reference_ip=reglage.reference_ip or None)
    if not decision.allowed:
        raise HTTPException(status_code=409, detail=decision.reason)


@router.post("/ai/test")
async def test_ai() -> dict[str, object]:
    """Soumet un cas fabrique au resolveur, et rapporte ce qu'il repond.

    Une cle peut etre valide de forme et refusee par le service, un modele peut
    ne plus exister, un serveur local peut ne pas repondre. Rien de tout cela ne
    se voit dans les reglages : le resolveur degradant vers la revue manuelle
    par construction, une configuration morte ressemble a une configuration qui
    n'a simplement rien eu a faire.

    Le cas soumis est un vrai cas difficile — un nom de release abime, sans
    annee — et la reponse est rendue telle quelle. Ce qui compte n'est pas
    qu'elle soit juste, c'est que le service reponde quelque chose.
    """
    from ..core.ai import build_resolver
    from ..core.ai.base import AmbiguousItem
    from ..core.parser import parse

    prefs = get_store().load().ai
    if not prefs.enabled:
        raise HTTPException(400, "Le résolveur est désactivé : active-le d'abord.")

    ok, motif = resolver_status(prefs.provider, prefs.api_key, prefs.model, prefs.base_url)
    if not ok:
        raise HTTPException(400, motif)

    await _exige_la_sortie()

    resolveur = build_resolver(prefs.provider, prefs.api_key, prefs.model, prefs.base_url)
    if resolveur is None:
        raise HTTPException(400, "Résolveur inutilisable avec ces réglages.")

    nom = "epz-the.vampire.diaries.109.le.cristal.de.la.discorde-Wawacity.avi"
    item = AmbiguousItem(
        index=0,
        filename=nom,
        parent_folder="JDownloader2",
        parsed=parse(Path(nom)),
        candidates=[],
    )

    try:
        propositions = await asyncio.to_thread(resolveur.resolve, [item])
    except Exception as exc:
        raise HTTPException(
            502,
            f"Le service n'a pas répondu : {type(exc).__name__} — {exc}",
        ) from exc
    finally:
        fermer = getattr(resolveur, "close", None)
        if callable(fermer):
            fermer()

    proposition = propositions.get(0)
    if proposition is None or not proposition.title:
        return {
            "ok": False,
            "detail": "Le service a répondu, mais sans identification exploitable.",
            "sent": nom,
        }
    return {
        "ok": True,
        "detail": (
            f"« {proposition.title} »"
            + (f" ({proposition.year})" if proposition.year else "")
            + (
                f" S{proposition.season:02}E{proposition.episode:02}"
                if proposition.season and proposition.episode
                else ""
            )
            + f" — confiance {proposition.confidence:.0%}"
        ),
        "sent": nom,
    }


@router.post("/notifications/test")
async def test_notification() -> dict[str, object]:
    """Envoie un message d'essai dans le canal configure.

    Un webhook peut etre valide de forme et revoque cote Discord ; seul un
    envoi reel le dit. On rapporte l'echec au lieu de l'avaler, contrairement
    aux notifications de cycle : ici l'utilisateur attend une reponse.
    """
    prefs = get_store().load().notifications
    if not prefs.webhook_url:
        raise HTTPException(status_code=400, detail="Aucun webhook enregistré.")

    await _exige_la_sortie()
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
            detail="Discord n'a pas accepté le message. Vérifie que le webhook existe toujours.",
        )
    return {"sent": True}


TEST_QUERY = "Dune"
"""Titre interroge par l'essai de cle. Court, sans accent, et present dans le
catalogue de TheMovieDB dans toutes les langues : une recherche vide ne
prouverait rien sur la cle, seulement sur le titre choisi."""


@router.post("/metadata/test")
async def test_metadata() -> dict[str, object]:
    """Interroge REELLEMENT TheMovieDB avec la cle enregistree.

    Une cle peut etre bien formee, enregistree, et refusee : la confusion entre
    la cle v3 et le jeton v4 se lit comme un 401 muet, et sans essai elle ne se
    manifeste que plus tard, sous la forme d'un scan qui n'identifie rien. Ce
    que le fournisseur sait dire du refus est repris tel quel — il nomme la
    confusion v3/v4, seule cause frequente ici.
    """
    cle = tmdb_key()
    if not cle:
        raise HTTPException(
            status_code=400,
            detail="Aucune clé TheMovieDB enregistrée. Saisis-la puis enregistre avant de tester.",
        )

    await _exige_la_sortie()

    # La memoire des refus est videe AVANT l'essai : sans cela, un 401 releve
    # lors d'un scan precedent serait resservi comme verdict de ce test, y
    # compris apres correction de la cle. Un essai doit repondre sur l'instant,
    # pas repeter ce qu'il savait deja.
    forget_auth_errors()
    provider = TMDBProvider(cle, tmdb_language())
    try:
        resultats = await provider.search_movie(TEST_QUERY, None)
    finally:
        await provider.aclose()

    if not resultats:
        raise HTTPException(
            status_code=502,
            detail=last_auth_error("tmdb")
            or "TheMovieDB n'a rien renvoyé. Vérifie la clé et l'accès réseau du conteneur.",
        )

    # Le titre trouve est renvoye : c'est ce qui prouve que la LANGUE est prise
    # en compte, et pas seulement que la cle passe.
    return {"ok": True, "language": tmdb_language(), "sample": resultats[0].title}


@router.post("/vpn/test")
async def test_vpn() -> dict[str, object]:
    """Mesure la sortie MAINTENANT et rend le verdict tel quel.

    ``force`` court-circuite le cache d'une minute : on presse ce bouton
    justement parce qu'on vient de changer quelque chose — brancher le tunnel,
    redemarrer gluetun — et resservir la mesure d'avant repondrait a la
    question precedente.

    Le verdict est rendu meme en mode « libre » : mesurer sur demande n'engage
    a rien, alors que refuser de repondre tant qu'une politique n'est pas
    choisie obligerait a l'activer pour savoir s'il faut l'activer.
    """
    # Pas de garde de sortie ici, a dessein : cette mesure EST la verification.
    # La soumettre a « exiger » l'empecherait de jamais confirmer la sortie, et
    # la politique ne se leverait plus.
    prefs = get_store().load().vpn
    etat = await check_vpn(reference_ip=prefs.reference_ip or None, force=True)

    return {
        # Le verdict brut ET sa phrase : l'interface colore d'apres le premier
        # et affiche la seconde, sans avoir a reecrire l'explication.
        "verdict": str(etat.verdict),
        "explanation": etat.explanation,
        "measured": etat.measured,
        "tunnel_present": etat.tunnel_present,
        "egress_confirmed": etat.egress_confirmed,
        "tunnel_interfaces": list(etat.tunnel_interfaces),
        "public_ip": etat.public_ip,
        "organization": etat.organization,
        "provider": etat.provider,
        "echo_service": etat.echo_service,
        "error": etat.error,
        "policy": str(prefs.policy),
        "reference_ip_set": bool(prefs.reference_ip),
        # Aucune proposition d'adopter l'adresse mesuree comme temoin. Un verdict
        # « inconnu » ne dit PAS que la sortie est en clair : un VPN actif chez un
        # fournisseur non reconnu donne le meme. Adopter alors l'adresse du tunnel
        # comme adresse de la maison inverserait tous les verdicts suivants — une
        # sortie en clair serait dite protegee. Le temoin se releve VPN coupe.
    }


@router.post("/subtitles/test")
async def test_subtitles() -> dict[str, object]:
    """Interroge REELLEMENT OpenSubtitles avec la cle enregistree.

    Une recherche par titre, pas par empreinte : l'empreinte demande un fichier
    sous la main, et ce bouton doit repondre avant qu'aucun rangement n'ait eu
    lieu. Ce que le fournisseur dit du refus est repris tel quel — il distingue
    une cle absente, une cle refusee et un quota epuise, et ces trois-la
    appellent trois gestes differents.
    """
    prefs = get_store().load().subtitles
    if not prefs.opensubtitles_api_key.strip():
        # La cle, pas « la cle ou le jeton » : le fournisseur n'emet rien sans
        # cle, le jeton VIP ne fait que relever un quota. Accepter le jeton seul
        # promettait un essai qui ne pouvait repondre qu'« indisponible ».
        jeton = (
            " Le jeton VIP seul ne suffit pas : il relève un quota, il ne remplace pas la clé."
            if prefs.opensubtitles_token.strip()
            else ""
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "Aucune clé d'API OpenSubtitles enregistrée. Saisis-la puis enregistre avant "
                f"de tester.{jeton}"
            ),
        )

    await _exige_la_sortie()

    from ..providers.opensubtitles import OpenSubtitlesProvider

    fournisseur = OpenSubtitlesProvider(
        prefs.opensubtitles_api_key, token=prefs.opensubtitles_token
    )
    try:
        if not fournisseur.available:
            raise HTTPException(status_code=400, detail=fournisseur.unavailable_reason)
        offres = await fournisseur.find(
            title=TEST_QUERY,
            languages=prefs.languages or ["en"],
        )
        motif = fournisseur.unavailable_reason
    finally:
        await fournisseur.aclose()

    if not offres:
        raise HTTPException(
            status_code=502,
            detail=(
                motif
                or "OpenSubtitles n'a rien renvoyé pour « "
                + TEST_QUERY
                + " ». Vérifie la clé et l'accès réseau du conteneur."
            ),
        )

    # Les langues REELLEMENT rendues, pas celles demandees : c'est ce qui
    # montre qu'une langue demandee n'existe pour aucun film, cas qu'on
    # confondrait sinon avec une cle qui ne marche pas.
    langues = sorted({o.language for o in offres if o.language})
    return {
        "ok": True,
        "sample": offres[0].release or TEST_QUERY,
        "languages": langues,
        "wanted": list(prefs.languages),
    }


@router.post("/media-server/test")
async def test_media_server() -> dict[str, object]:
    """Demande un rafraichissement d'essai. Rapporte l'echec au lieu de l'avaler.

    Pas de garde de sortie VPN, a dessein : le serveur multimedia vit sur le
    reseau local, l'appel ne montre l'adresse publique a personne. Le bloquer
    sous « exiger » interdirait de verifier un reglage sans risque de fuite.
    """
    prefs = get_store().load().media_server
    if not prefs.base_url or not prefs.api_key:
        raise HTTPException(status_code=400, detail="Adresse ou clé d'API manquante.")

    if not await refresh_library(prefs.base_url, prefs.api_key):
        raise HTTPException(
            status_code=502,
            detail=(
                "Le serveur n'a pas répondu. Vérifie l'adresse, la clé d'API, "
                "et que le serveur est bien sur le réseau local."
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
        raise HTTPException(status_code=404, detail="Aucune décision pour ce titre.")
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
                # La cle EFFECTIVE, preferences comprises : ce diagnostic dit
                # si l'identification peut fonctionner, pas si le .env est
                # rempli. Les deux ont diverge le jour ou la cle a pu se
                # saisir dans l'interface.
                "configured": bool(tmdb_key()),
                "required": True,
                "hint": "Réglages → Métadonnées, ou TMDB_API_KEY",
            },
            {
                "name": "AniList",
                "role": "Animes",
                "configured": True,
                "required": False,
                "hint": "Aucune clé requise",
            },
        ],
        # Pas de bloc « ai » ici : le resolveur se regle dans les preferences,
        # pas dans l'environnement. Ce qui le concerne sort par
        # ``GET /api/settings/preferences``, avec « ai_ready » qui dit pourquoi
        # il ne fait rien le cas echeant.
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

    cle = tmdb_key()
    checks.append(
        {
            "name": "TheMovieDB",
            "ok": bool(cle),
            # D'ou vient la cle, et pas seulement qu'elle existe : les deux
            # sources se valent a l'usage, mais on ne corrige pas au meme
            # endroit.
            "detail": (
                "Clé présente (réglages)"
                if get_store().load().metadata.tmdb_api_key.strip()
                else "Clé présente (TMDB_API_KEY)"
            )
            if cle
            else "Sans clé, aucun candidat n'est proposé et rien ne peut être identifié",
        }
    )

    return checks
