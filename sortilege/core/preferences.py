"""Preferences modifiables depuis l'interface.

Distinction importante avec ``config.py`` :

- ``config.py`` decrit le DEPLOIEMENT : points de montage, cles d'API, secrets.
  Il vient de l'environnement et n'est pas modifiable a chaud — ces valeurs
  doivent correspondre aux volumes du conteneur, les changer depuis l'UI
  produirait une configuration qui ne survit pas a un redemarrage.
- ce module decrit l'USAGE : quelles sources scanner, ou ranger chaque type,
  avec quel gabarit. Ce sont des choix qui appartiennent a l'utilisateur et qui
  changent souvent.

Garde-fou : une destination est toujours resolue SOUS la racine de
bibliotheque declaree dans l'environnement. Sans cela, l'interface deviendrait
un moyen d'ecrire n'importe ou sur le NAS.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, datetime
from ipaddress import ip_address
from pathlib import Path
from threading import Lock

from . import apikey
from .mediaserver import MediaServerError, validate_server_url
from .nfo import ON_EXISTING_CHOICES, LocalMetadataSettings
from .notify import WebhookError, validate_webhook
from .quality import BY_KEY as QUALITY_KEYS
from .quality import QualitySettings
from .safety import PathConfinementError, resolve_within
from .subtitles import is_language_code, normalise_language
from .template import PRESETS, TemplateError, validate
from .vpn import Policy as VpnPolicy

logger = logging.getLogger(__name__)

KINDS = ("movie", "episode", "anime", "book")

VIDEO_KINDS = ("movie", "episode", "anime")
"""Les types auxquels une notion de RESOLUTION s'applique.

Un livre n'a ni resolution, ni debit, ni strategie de qualite : lui en demander
une n'aurait aucun sens, et l'y forcer bloquerait l'enregistrement des
preferences."""

DEFAULT_DESTINATIONS: dict[str, str] = {
    "movie": "Films",
    "episode": "Series",
    "anime": "Animes",
    "book": "Livres",
}


LANGUAGE_TAG = re.compile(r"[a-z]{2}-[A-Z]{2}")
"""Forme attendue par TheMovieDB : « fr-FR », « en-US », « pt-BR ».

Verifiee ici plutot que laissee passer : une valeur mal formee ne provoque pas
d'erreur cote fournisseur, elle est ignoree — on recoit alors des titres
anglais sans qu'aucun message ne dise pourquoi."""


class PreferenceError(ValueError):
    """Preference refusee — message destine a l'utilisateur."""


@dataclass
class AISettings:
    """Choix du fournisseur IA, modifiable depuis l'interface.

    La cle vit ici et non dans l'environnement : changer de fournisseur ne doit
    pas imposer de modifier la stack et de redemarrer. Elle n'est JAMAIS
    renvoyee au navigateur — l'API n'expose qu'un booleen « configuree ».
    """

    enabled: bool = False
    provider: str = "anthropic"
    model: str = ""
    """Vide = le modele par defaut du fournisseur."""

    base_url: str = ""
    """Vide = l'URL connue du fournisseur. A renseigner pour « custom »."""

    api_key: str = ""
    threshold: float = 0.80
    """En dessous de ce score, le resolveur est sollicite. Au-dessus, le
    resultat deterministe est deja bon : payer un appel n'apporterait rien."""

    # Pas de taille de lot ici : voir AI_BATCH_SIZE dans pipeline.py. Le reglage
    # etait acceptable en ecriture, relu par le serveur, et n'apparaissait sur
    # aucun ecran — personne ne pouvait le decider, et il n'y a rien a arbitrer :
    # la valeur ne change ni le cout ni la qualite, seulement le decoupage.


@dataclass
class MetadataSettings:
    """Fournisseur de metadonnees : la cle TheMovieDB et la langue demandee.

    La cle vit ici et non dans l'environnement, pour la meme raison que celle
    du resolveur IA : obtenir une cle est le premier geste d'une installation,
    et le seul qui imposait jusqu'ici d'editer un fichier puis de redemarrer la
    pile. Elle n'est JAMAIS renvoyee au navigateur — l'API n'expose qu'un
    booleen « configuree ».

    L'environnement reste lu en repli (``TMDB_API_KEY``) : les installations
    existantes ne doivent pas s'arreter d'identifier le jour de la mise a jour.
    """

    tmdb_api_key: str = ""
    language: str = "fr-FR"
    """Langue des titres et resumes demandes au fournisseur.

    Determinante et pas seulement cosmetique : le titre renvoye est celui qui
    est compare au nom du fichier. Une bibliotheque nommee en anglais
    interrogee en francais fait s'effondrer la similarite de titre, donc le
    score, et remplit la file de revue d'identifications pourtant justes."""


@dataclass
class ScanSettings:
    """Ce que le parcours des sources ecarte avant meme de l'analyser.

    Ces trois valeurs etaient des constantes, donc des decisions prises a la
    place de l'utilisateur : un court-metrage ou un episode en 480p passait
    sous le plancher et disparaissait SANS TRACE, et aucun dossier personnel ne
    pouvait etre sorti du perimetre.
    """

    min_size_mb: int = 50
    """Plancher d'un fichier video. En dessous, c'est presque toujours un
    echantillon ou un telechargement avorte — mais « presque » justifiait de
    pouvoir descendre."""

    extra_skip_dirs: list[str] = field(default_factory=list)
    """Noms de dossiers a ignorer, EN PLUS de ceux qui le sont toujours."""

    extra_skip_hints: list[str] = field(default_factory=list)
    """Fragments de nom de fichier a ignorer, en plus de « sample », « trailer »
    et compagnie. Compares en minuscules, n'importe ou dans le nom."""

    def min_size_bytes(self) -> int:
        return max(0, self.min_size_mb) * 1024 * 1024


@dataclass
class OversizeSettings:
    """Destination separee pour les fichiers volumineux.

    Un remux 4K de 60 Go et un episode de 800 Mo n'ont pas les memes
    contraintes : on veut souvent les premiers sur un autre volume, ou
    simplement isoles pour les reperer. Le seuil se regle, et la destination
    reste soumise au meme confinement que les autres.
    """

    enabled: bool = False
    threshold_gb: float = 20.0
    destinations: dict[str, str] = field(
        default_factory=lambda: {
            "movie": "Films 4K",
            "episode": "Series 4K",
            "anime": "Animes 4K",
        }
    )

    def threshold_bytes(self) -> int:
        return int(self.threshold_gb * 1024**3)


@dataclass
class TranscodeSettings:
    """Reencodage differe des fichiers qui ne respectent pas la strategie.

    Desactive par defaut, et c'est delibere : c'est la seule fonction de
    Sortilege qui degrade volontairement de la qualite. Elle doit etre demandee,
    jamais subie.
    """

    enabled: bool = False

    start_hour: int = 23
    end_hour: int = 7
    """Plage horaire d'encodage. Traverse minuit dans le cas normal. Bornes
    egales = aucune restriction.

    Un encodage DEJA COMMENCE va a son terme meme si la plage se ferme :
    l'interrompre a six heures du matin jetterait une nuit de calcul."""

    codec: str = "libx264"
    """H.264 par defaut, pas HEVC : il gagne moins de place mais se lit
    partout, y compris sur les televiseurs et boitiers anciens. Une
    bibliotheque qu'on ne peut plus lire n'a pas gagne de place, elle a perdu
    des films."""

    crf: int = 21
    """Qualite constante. Plus bas = meilleure image et fichier plus gros. 21
    est le compromis courant pour un reencodage qu'on ne veut pas voir."""

    preset: str = "medium"
    """Compromis vitesse/compression de x264. « slow » gagne environ dix pour
    cent de place pour deux fois plus de temps — rarement rentable sur un NAS
    qui a des nuits, pas des semaines."""


@dataclass
class AutomationSettings:
    """Traitement automatique de bout en bout."""

    enabled: bool = False
    interval_minutes: int = 15
    quiet_seconds: int = 120
    """Un fichier doit n'avoir plus bouge depuis ce delai. Attendre trop coute
    un cycle ; traiter trop tot coute un fichier incomplet deplace."""

    apply_auto: bool = False
    """Deplacer reellement, ou seulement remplir la file. Desactive par
    defaut : ranger sans personne devant est un engagement plus lourd
    qu'identifier."""


@dataclass
class NotificationSettings:
    """Notification Discord de ce que le cycle automatique a fait.

    L'URL vit ici et non dans l'environnement : ajouter un canal ne doit pas
    imposer de modifier la stack et de redemarrer. Elle n'est JAMAIS renvoyee
    au navigateur — l'API n'expose qu'un booleen « configuree ».
    """

    enabled: bool = False
    webhook_url: str = ""
    on_failure: bool = True
    """Prevenir quand un cycle echoue. Active par defaut : c'est precisement ce
    qu'on ne verra pas autrement, puisque personne ne regarde l'interface tant
    que tout va bien."""


@dataclass
class MediaServerSettings:
    """Serveur multimedia a prevenir apres un rangement.

    Sans cela, un film range n'apparait dans Jellyfin qu'au prochain scan
    planifie — souvent plusieurs heures. La cle vit ici et non dans
    l'environnement, et n'est JAMAIS renvoyee au navigateur.
    """

    enabled: bool = False
    base_url: str = ""
    api_key: str = ""


@dataclass
class IntegrationSettings:
    """Cle d'API : ce qui permet de declencher Sortilege sans navigateur.

    Elle vit dans les preferences pour la meme raison que les autres cles —
    l'ajouter ne doit pas imposer d'editer un .env puis de redemarrer la pile.
    Elle en differe sur un point, et un seul : **elle se relit**. Les autres
    appartiennent a un service tiers et n'ont a sortir nulle part ; celle-ci
    n'existe que pour etre recopiee dans un script ou dans les reglages d'un
    client de telechargement. Voir ``core/apikey.py`` pour ce que ce choix
    coute et ce qui le borne.
    """

    api_key: str = ""


@dataclass
class SubtitleSettings:
    """Recuperation des sous-titres manquants apres un rangement.

    Desactive par defaut : cela ajoute des appels reseau vers un service tiers
    et depose des fichiers dans la bibliotheque. Les deux doivent etre demandes.

    L'ordre de ``languages`` est un ordre de PREFERENCE, pas un ensemble : la
    premiere langue disponible pour une video est celle qu'on depose. Un ordre
    perdu ferait arriver l'anglais avant le francais une fois sur deux, sans
    que rien ne dise pourquoi.
    """

    enabled: bool = False

    opensubtitles_api_key: str = ""
    """Ecriture seule, comme les autres cles. JAMAIS renvoyee au navigateur."""

    opensubtitles_token: str = ""
    """Jeton d'un compte VIP. Facultatif : sans lui le quota gratuit
    s'applique, ce qui suffit a une bibliotheque qui ne bouge plus."""

    languages: list[str] = field(default_factory=lambda: ["fr", "en"])
    """Ordre de preference, pas ensemble. Voir la docstring de la classe."""

    overwrite: bool = False
    """Remplacer un sous-titre deja present. Non par defaut : un fichier
    corrige a la main ne doit pas etre efface par un telechargement."""

    audio_is_enough: bool = False
    """Ne rien chercher quand la video porte deja la langue en AUDIO. Pour qui
    ne lit les sous-titres que faute de piste comprehensible."""


@dataclass
class VpnSettings:
    """Exigence de sortie par un tunnel avant d'emettre du trafic.

    Sortilege interroge TheMovieDB, AniList, OpenSubtitles, Discord et le
    fournisseur d'IA choisi : chacun de ces appels revele l'adresse publique de
    la maison, et la liste de ce qu'on y identifie. C'est cela que ce reglage
    protege — pas un telechargement, qui n'existe pas encore ici.

    « Libre » est le defaut et ne mesure RIEN : mesurer pour n'exiger rien
    ajouterait une requete reseau a chaque cycle, pour un renseignement que
    personne n'a demande.
    """

    policy: str = VpnPolicy.FREE

    reference_ip: str = ""
    """Adresse publique constatee HORS VPN. Sans elle, une sortie protegee ne
    peut souvent pas etre distinguee d'une sortie en clair : c'est la seule
    donnee qui tranche quand l'organisation du fournisseur n'est pas reconnue."""


@dataclass
class Preferences:
    """Ce que l'utilisateur choisit, par opposition a ce que l'admin deploie."""

    custom_sources: list[str] = field(default_factory=list)
    """Sources ajoutees depuis l'interface, en plus des racines montees.

    Chacune doit se trouver SOUS une racine declaree dans l'environnement : le
    conteneur ne voit que ses volumes, et laisser saisir un chemin libre
    permettrait de parcourir tout ce qui est monte."""

    enabled_sources: list[str] = field(default_factory=list)
    """Sous-ensemble des sources a scanner. Vide = toutes.

    Utile quand une source est un montage reseau lent qu'on ne veut pas
    reparcourir a chaque fois."""

    destinations: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DESTINATIONS))
    """Sous-chemin de destination par type, RELATIF a la racine de
    bibliotheque. Permet de separer films et series sur des arborescences
    differentes."""

    templates: dict[str, str] = field(default_factory=dict)
    """Gabarit par type. Vide = celui du prereglage Jellyfin."""

    ai: AISettings = field(default_factory=AISettings)
    automation: AutomationSettings = field(default_factory=AutomationSettings)
    oversize: OversizeSettings = field(default_factory=OversizeSettings)
    notifications: NotificationSettings = field(default_factory=NotificationSettings)
    media_server: MediaServerSettings = field(default_factory=MediaServerSettings)
    metadata: MetadataSettings = field(default_factory=MetadataSettings)
    scan: ScanSettings = field(default_factory=ScanSettings)
    quality: QualitySettings = field(default_factory=QualitySettings)
    transcode: TranscodeSettings = field(default_factory=TranscodeSettings)
    integration: IntegrationSettings = field(default_factory=IntegrationSettings)
    local_metadata: LocalMetadataSettings = field(default_factory=LocalMetadataSettings)
    subtitles: SubtitleSettings = field(default_factory=SubtitleSettings)
    vpn: VpnSettings = field(default_factory=VpnSettings)

    def template_for(self, kind: str) -> str:
        return self.templates.get(kind) or PRESETS["jellyfin"].get(kind, "")

    def destination_for(self, kind: str) -> str:
        return self.destinations.get(kind) or DEFAULT_DESTINATIONS.get(kind, "")


def _meme_nature(defaut: object, valeur: object) -> tuple[bool, object]:
    """La valeur relue a-t-elle la nature du defaut ? Rend aussi la valeur retenue.

    ``bool`` est un ``int`` pour Python : sans le test explicite, « "enabled": 1 »
    passerait pour un booleen et « "interval_minutes": true » pour un entier. A
    l'inverse un entier est accepte la ou un decimal est attendu — « "threshold":
    1 » est ce qu'ecrit un humain, pas une erreur.

    Les listes et les tables des blocs sont toutes des listes de textes et des
    tables texte -> texte (langues, exclusions, destinations) : c'est ce qui est
    verifie, element par element.
    """
    if isinstance(defaut, bool):
        return isinstance(valeur, bool), valeur
    if isinstance(defaut, int):
        return isinstance(valeur, int) and not isinstance(valeur, bool), valeur
    if isinstance(defaut, float):
        ok = isinstance(valeur, int | float) and not isinstance(valeur, bool)
        return ok, (float(valeur) if ok else valeur)
    if isinstance(defaut, str):
        return isinstance(valeur, str), valeur
    if isinstance(defaut, list):
        return isinstance(valeur, list) and all(isinstance(v, str) for v in valeur), valeur
    if isinstance(defaut, dict):
        ok = isinstance(valeur, dict) and all(
            isinstance(k, str) and isinstance(v, str) for k, v in valeur.items()
        )
        return ok, valeur
    return True, valeur


def _bloc(gabarit, brut: object):
    """Relit un bloc de preferences en IGNORANT ce qu'il ne connait plus.

    Un reglage retire du code laisse une cle orpheline dans le fichier deja
    ecrit sur le disque. La passer au constructeur leve un TypeError qui n'est
    rattrape nulle part : l'application repartirait alors avec TOUTES les
    preferences aux defauts — destinations, gabarits, sources — pour un champ
    qui ne sert plus. Un reglage supprime doit s'oublier, pas tout emporter.

    Meme logique pour une valeur de la mauvaise NATURE (« "languages": "fr" »,
    « "enabled": "false" », un bloc entier remplace par une chaine) : acceptee
    telle quelle, elle passait la relecture puis faisait echouer TOUS les
    enregistrements suivants, ou levait une AttributeError au demarrage. Elle
    est ecartee au profit du defaut, et le journal le dit.
    """
    defauts = gabarit()
    if brut is None:
        return defauts
    if not isinstance(brut, dict):
        logger.warning(
            "preferences : bloc %s ignore (%s au lieu d'un objet), valeurs par defaut",
            gabarit.__name__,
            type(brut).__name__,
        )
        return defauts

    retenus: dict[str, object] = {}
    for champ in fields(gabarit):
        if champ.name not in brut:
            continue
        attendu = getattr(defauts, champ.name)
        ok, valeur = _meme_nature(attendu, brut[champ.name])
        if not ok:
            # Le type seulement, jamais la valeur : ce champ peut etre une cle.
            logger.warning(
                "preferences : %s.%s ignore (%s attendu, %s lu), valeur par defaut",
                gabarit.__name__,
                champ.name,
                type(attendu).__name__,
                type(brut[champ.name]).__name__,
            )
            continue
        retenus[champ.name] = valeur
    return replace(defauts, **retenus)


def _textes(brut: object, nom: str) -> list[str]:
    """Liste de textes de premier niveau, ou vide si le fichier dit autre chose.

    Sans ce controle, « "custom_sources": "/data" » devenait la liste de ses
    caracteres — puis un refus a chaque enregistrement."""
    if brut is None:
        return []
    if isinstance(brut, list) and all(isinstance(v, str) for v in brut):
        return list(brut)
    logger.warning("preferences : %s ignore (liste de textes attendue), valeur par defaut", nom)
    return []


def _table(brut: object, nom: str) -> dict[str, str]:
    """Table texte -> texte de premier niveau, ou vide si le fichier dit autre chose."""
    if brut is None:
        return {}
    if isinstance(brut, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in brut.items()
    ):
        return dict(brut)
    logger.warning("preferences : %s ignore (table de textes attendue), valeur par defaut", nom)
    return {}


class PreferenceStore:
    """Persistance JSON.

    Un fichier plutot qu'une table : ces preferences forment un seul document,
    elles se lisent a chaque requete et s'ecrivent rarement. Une table
    n'apporterait rien et il faudrait la migrer.
    """

    def __init__(
        self,
        path: Path,
        library_root: Path,
        source_roots: list[Path],
        extra_roots: list[Path] | None = None,
    ) -> None:
        self._path = path
        self._library_root = library_root
        self._source_roots = source_roots
        self._extra_roots = extra_roots or []
        self._lock = Lock()
        self._cache: Preferences | None = None
        self._illisible = False
        """Vrai quand un fichier EXISTE mais n'a pas pu etre relu.

        Les defauts servis a sa place ne sont qu'un repli en memoire. Sans ce
        drapeau, la premiere ecriture — la cle d'API au demarrage suffisait —
        remplacait sources, destinations, gabarits et cles par ces defauts,
        pour une virgule en trop."""

    @property
    def unreadable(self) -> bool:
        """Le fichier sur disque est-il illisible (defauts servis en memoire) ?"""
        return self._illisible

    def load(self) -> Preferences:
        with self._lock:
            if self._cache is not None:
                return self._cache

            self._illisible = False
            if not self._path.is_file():
                self._cache = Preferences()
                return self._cache

            motif = ""
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raw, motif = None, str(exc)
            if not isinstance(raw, dict):
                # Un fichier corrompu ne doit pas empecher l'application de
                # demarrer : on sert les defauts EN MEMOIRE, et le fichier reste
                # intact. Il n'est remplace qu'a un enregistrement, et mis de
                # cote juste avant (voir ``_ecrit``).
                logger.warning(
                    "preferences illisibles (%s) : defauts en memoire, fichier laisse intact",
                    motif or f"{type(raw).__name__} au lieu d'un objet",
                )
                self._illisible = True
                self._cache = Preferences()
                return self._cache

            self._cache = Preferences(
                custom_sources=_textes(raw.get("custom_sources"), "custom_sources"),
                enabled_sources=_textes(raw.get("enabled_sources"), "enabled_sources"),
                destinations={
                    **DEFAULT_DESTINATIONS,
                    **_table(raw.get("destinations"), "destinations"),
                },
                templates=_table(raw.get("templates"), "templates"),
                ai=_bloc(AISettings, raw.get("ai")),
                automation=_bloc(AutomationSettings, raw.get("automation")),
                oversize=_bloc(OversizeSettings, raw.get("oversize")),
                notifications=_bloc(NotificationSettings, raw.get("notifications")),
                media_server=_bloc(MediaServerSettings, raw.get("media_server")),
                metadata=_bloc(MetadataSettings, raw.get("metadata")),
                scan=_bloc(ScanSettings, raw.get("scan")),
                quality=_bloc(QualitySettings, raw.get("quality")),
                transcode=_bloc(TranscodeSettings, raw.get("transcode")),
                integration=_bloc(IntegrationSettings, raw.get("integration")),
                local_metadata=_bloc(LocalMetadataSettings, raw.get("local_metadata")),
                subtitles=_bloc(SubtitleSettings, raw.get("subtitles")),
                vpn=_bloc(VpnSettings, raw.get("vpn")),
            )
            return self._cache

    def save(self, prefs: Preferences) -> Preferences:
        self.validate(prefs)
        with self._lock:
            self._ecrit(prefs)
        return prefs

    def _met_de_cote(self) -> Path:
        """Copie le fichier illisible sous ``preferences.json.illisible-<date>``.

        Copie et non renommage : si l'ecriture qui suit echoue, l'original doit
        rester a sa place. Et si la copie elle-meme echoue, on refuse d'ecrire —
        remplacer sans filet un fichier qui porte peut-etre toutes les sources
        et toutes les cles, a une virgule pres, est exactement ce qu'on evite.
        """
        horodatage = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        copie = self._path.with_name(f"{self._path.name}.illisible-{horodatage}")
        rang = 1
        while copie.exists():
            copie = self._path.with_name(f"{self._path.name}.illisible-{horodatage}-{rang}")
            rang += 1
        try:
            shutil.copy2(self._path, copie)
        except OSError as exc:
            raise PreferenceError(
                "Le fichier de réglages est illisible et sa copie de secours n'a pas pu être "
                f"écrite ({exc}). Enregistrement refusé : il aurait remplacé ce fichier sans "
                "filet."
            ) from exc
        logger.warning("preferences illisibles conservees sous %s avant reecriture", copie.name)
        return copie

    def _ecrit(self, prefs: Preferences) -> None:
        """Ecriture atomique. A appeler verrou tenu.

        Fichier temporaire puis ``os.replace`` : une coupure au milieu laisse
        l'ancien fichier intact, jamais un JSON tronque que la relecture suivante
        prendrait pour un fichier illisible — et remplacerait par les defauts.
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._illisible and self._path.exists():
            self._met_de_cote()

        provisoire = self._path.with_name(f"{self._path.name}.tmp")
        try:
            provisoire.write_text(
                json.dumps(asdict(prefs), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(provisoire, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                provisoire.unlink(missing_ok=True)
            raise
        self._illisible = False
        self._cache = prefs

    def _enregistre_cle(self, cle: str) -> None:
        """Ecrit la cle d'API SANS la validation complete des preferences.

        Seule la longueur est controlee. Passer par ``save`` faisait dependre la
        cle de TOUT le reste : une source demontee depuis, une destination
        devenue invalide, et le demarrage levait une PreferenceError — conteneur
        en boucle, pour un reglage sans rapport avec la cle.
        """
        if len(cle.strip()) < apikey.MIN_LENGTH:
            raise PreferenceError(
                f"la clé d'API doit faire au moins {apikey.MIN_LENGTH} caractères."
            )
        prefs = self.load()
        with self._lock:
            self._ecrit(replace(prefs, integration=IntegrationSettings(api_key=cle)))

    def ensure_api_key(self) -> str:
        """Cle d'API garantie presente. Appele au demarrage.

        Generee ici plutot que demandee a l'utilisateur : une cle qu'il faut
        penser a creer avant de pouvoir integrer quoi que ce soit est une cle
        qui n'existe pas le jour ou on en a besoin. Une installation qui n'a
        jamais ouvert l'ecran d'integration en a donc une, prete a copier.

        Une cle deja en place n'est jamais remplacee : le faire au demarrage
        casserait tous les scripts a chaque mise a jour d'image.

        Sur un fichier ILLISIBLE, rien n'est ecrit : la cle neuve vit en memoire
        le temps du processus. Ecrire ici remplacait les reglages de
        l'utilisateur par les defauts au premier demarrage suivant une faute de
        frappe — le geste le plus destructeur du produit, fait sans personne
        devant l'ecran.
        """
        prefs = self.load()
        courante = prefs.integration.api_key.strip()
        if len(courante) >= apikey.MIN_LENGTH:
            return courante

        neuve = apikey.generate()
        with self._lock:
            if self._illisible:
                self._cache = replace(prefs, integration=IntegrationSettings(api_key=neuve))
                logger.warning(
                    "preferences illisibles : cle d'API gardee en memoire seulement, fichier "
                    "non reecrit. Elle changera au prochain demarrage tant que %s n'est pas "
                    "repare ou reenregistre depuis l'interface.",
                    self._path.name,
                )
                return neuve
        self._enregistre_cle(neuve)
        return neuve

    def rotate_api_key(self) -> str:
        """Nouvelle cle. L'ancienne cesse de valoir immediatement.

        C'est la reponse a une cle divulguee, et c'est pour cela qu'elle doit
        rester a un clic : sans regeneration, la seule facon de revoquer un
        acces serait de changer le mot de passe de l'installation.

        Geste explicite, donc ecrit — y compris sur un fichier illisible, qui
        est alors mis de cote avant d'etre remplace. Une cle regeneree qui ne
        survivrait pas au redemarrage casserait les scripts qu'on vient de
        mettre a jour.
        """
        neuve = apikey.generate()
        self._enregistre_cle(neuve)
        return neuve

    def all_sources(self, prefs: Preferences | None = None) -> list[Path]:
        """Racines montees + sources ajoutees, dedoublonnees et ordonnees."""
        prefs = prefs or self.load()
        seen: dict[str, Path] = {str(p): p for p in self._source_roots}
        for raw in prefs.custom_sources:
            path = Path(raw)
            seen.setdefault(str(path), path)
        return list(seen.values())

    @property
    def allowed_areas(self) -> list[Path]:
        """Zones que l'interface peut parcourir et proposer comme source.

        Les racines sources ET la racine de bibliotheque. Cette derniere n'est
        pas un oubli : scanner une bibliotheque deja rangee est un usage a part
        entiere — la normaliser, corriger d'anciens noms, rattraper ce qui a ete
        classe a la main. C'est le cas « rattrapage » pour lequel on utilise
        FileBot d'habitude.
        """
        areas: dict[str, Path] = {str(p): p for p in self._source_roots}
        areas.setdefault(str(self._library_root), self._library_root)
        for extra in self._extra_roots:
            areas.setdefault(str(extra), extra)
        return list(areas.values())

    def _assert_under_a_root(self, raw: str) -> Path:
        """Une source doit vivre sous une zone autorisee.

        Le conteneur ne voit que ses volumes ; accepter un chemin libre
        reviendrait a offrir un parcours de tout ce qui est monte, y compris
        ce qui n'a rien a voir avec des medias.
        """
        if not raw.strip():
            raise PreferenceError("chemin de source vide")
        if any(part in ("..", ".") for part in raw.replace("\\", "/").split("/")):
            raise PreferenceError(f"« {raw} » : « .. » et « . » sont interdits.")

        candidate = Path(raw)
        if not candidate.is_absolute():
            raise PreferenceError(f"« {raw} » doit être un chemin absolu.")

        resolved = candidate.resolve(strict=False)
        for root in self.allowed_areas:
            root = root.resolve()
            if resolved == root or root in resolved.parents:
                return resolved

        allowed = ", ".join(str(p) for p in self.allowed_areas) or "(aucune)"
        raise PreferenceError(
            f"« {raw} » est hors des zones montées. Zones autorisées : {allowed}. "
            "Pour en ouvrir une autre, déclare-la dans SORTILEGE_ALLOWED_ROOTS "
            "(le volume doit déjà être monté dans le conteneur)."
        )

    def validate(self, prefs: Preferences) -> None:
        """Refuse une preference dangereuse ou inapplicable."""
        for raw in prefs.custom_sources:
            self._assert_under_a_root(raw)

        known = {str(p) for p in self.all_sources(prefs)}
        for source in prefs.enabled_sources:
            if source not in known:
                raise PreferenceError(
                    f"« {source} » n'est ni une racine montée ni une source ajoutée."
                )

        if prefs.oversize.threshold_gb <= 0:
            raise PreferenceError("le seuil de taille doit être strictement positif")

        # Les deux tables sont parcourues SEPAREMENT : les fusionner par cle
        # ferait ecraser une destination par son homologue « volumineux », et
        # la premiere echapperait silencieusement a la validation.
        pairs = [*prefs.destinations.items(), *prefs.oversize.destinations.items()]
        for kind, sub in pairs:
            if kind not in KINDS:
                raise PreferenceError(f"type de média inconnu : {kind}")
            if not sub.strip():
                raise PreferenceError(f"destination vide pour « {kind} »")

            # ``resolve_within`` NEUTRALISE une remontee au lieu de la refuser,
            # ce qui est le bon comportement pour un titre venu d'une API mais
            # pas pour une saisie humaine : l'utilisateur qui tape « ../../etc »
            # doit lire un refus, pas decouvrir plus tard que ses fichiers sont
            # partis dans « media/etc ». On refuse donc explicitement en amont.
            if sub.startswith("/"):
                raise PreferenceError(
                    f"« {kind} » : la destination doit être relative à la racine de "
                    "bibliothèque, sans « / » initial."
                )
            if any(part in ("..", ".") for part in sub.replace("\\", "/").split("/")):
                raise PreferenceError(
                    f"« {kind} » : « .. » et « . » sont interdits dans une destination."
                )

            try:
                resolve_within(self._library_root, sub)
            except PathConfinementError as exc:
                raise PreferenceError(f"destination refusée pour « {kind} » : {exc}") from exc

        from .ai import BY_KEY

        if prefs.ai.provider not in BY_KEY:
            raise PreferenceError(f"fournisseur IA inconnu : {prefs.ai.provider}")
        if prefs.automation.interval_minutes < 1:
            raise PreferenceError("l'intervalle doit valoir au moins une minute")
        if prefs.notifications.enabled:
            try:
                validate_webhook(prefs.notifications.webhook_url)
            except WebhookError as exc:
                raise PreferenceError(str(exc)) from exc
            if not prefs.notifications.webhook_url.strip():
                raise PreferenceError(
                    "Renseigne l'URL du webhook Discord avant d'activer les notifications."
                )

        if prefs.media_server.enabled:
            try:
                validate_server_url(prefs.media_server.base_url)
            except MediaServerError as exc:
                raise PreferenceError(str(exc)) from exc
            if not prefs.media_server.base_url.strip():
                raise PreferenceError(
                    "Renseigne l'adresse du serveur avant d'activer le rafraîchissement."
                )

        if not LANGUAGE_TAG.fullmatch(prefs.metadata.language.strip()):
            raise PreferenceError(
                f"« {prefs.metadata.language} » n'est pas une langue valide. Attendu « fr-FR », "
                "« en-US », « ja-JP » — deux lettres de langue, un tiret, deux lettres de pays."
            )

        cle = prefs.integration.api_key.strip()
        if cle and len(cle) < apikey.MIN_LENGTH:
            raise PreferenceError(
                f"la clé d'API doit faire au moins {apikey.MIN_LENGTH} caractères. "
                "Régénère-la depuis Réglages → Système → Intégration plutôt que de la "
                "saisir à la main : une clé courte se devine."
            )

        if prefs.local_metadata.on_existing not in ON_EXISTING_CHOICES:
            # Refuse plutot que corrige : cette valeur decide du sort de
            # fichiers que l'utilisateur n'a pas ecrits. Une faute de frappe
            # silencieusement ramenee a « ecraser » serait irrattrapable.
            raise PreferenceError(
                f"« {prefs.local_metadata.on_existing} » n'est pas une conduite connue face à un "
                f".nfo existant. Attendu : {', '.join(ON_EXISTING_CHOICES)}."
            )

        if prefs.scan.min_size_mb < 0:
            raise PreferenceError("la taille minimale ne peut pas être négative")
        if prefs.scan.min_size_mb > 100_000:
            # Cent gigaoctets ecarteraient absolument tout : ce n'est pas un
            # reglage prudent, c'est un scan qui ne trouvera plus jamais rien.
            raise PreferenceError("la taille minimale doit rester en dessous de 100 000 Mo")
        for nom in prefs.scan.extra_skip_dirs:
            if "/" in nom or "\\" in nom:
                raise PreferenceError(
                    f"« {nom} » : indique un NOM de dossier, pas un chemin. "
                    "L'exclusion s'applique à ce nom où qu'il se trouve sous les sources."
                )

        for kind in VIDEO_KINDS:
            choix = getattr(prefs.quality, kind, None)
            if choix not in QUALITY_KEYS:
                raise PreferenceError(f"stratégie de qualité inconnue pour « {kind} » : {choix}")

        if prefs.automation.quiet_seconds < 0:
            raise PreferenceError("le délai de stabilité ne peut pas être négatif")

        if not 0.0 <= prefs.ai.threshold <= 1.0:
            raise PreferenceError("le seuil IA doit être compris entre 0 et 1")
        if prefs.ai.base_url and not prefs.ai.base_url.startswith(("http://", "https://")):
            raise PreferenceError("l'URL du service IA doit commencer par http:// ou https://")
        if prefs.ai.provider == "custom" and prefs.ai.enabled and not prefs.ai.base_url:
            raise PreferenceError(
                "un fournisseur « Autre » demande une URL de base : sans elle, "
                "on ne sait pas qui appeler."
            )

        if prefs.subtitles.enabled:
            if not prefs.subtitles.languages:
                raise PreferenceError(
                    "Choisis au moins une langue de sous-titres avant d'activer la "
                    "recherche : sans langue demandée, il n'y a rien à chercher."
                )
            # La cle, pas « la cle ou le jeton » : le fournisseur n'emet rien
            # sans cle, le jeton VIP ne fait que relever un quota. Accepter le
            # jeton seul enregistrait une recherche active qui ne pouvait rien
            # trouver.
            if not prefs.subtitles.opensubtitles_api_key.strip():
                raise PreferenceError(
                    "Renseigne la clé d'API OpenSubtitles avant d'activer la recherche. "
                    "Le jeton VIP seul ne suffit pas : il relève un quota, il ne remplace "
                    "pas la clé. Sans elle le service refuse toutes les requêtes, et la "
                    "recherche échouerait en silence à chaque rangement."
                )

        # Normalisee ICI plutot qu'a l'usage : « FR », « fre » et « fr-FR »
        # designent la meme langue, et deux ecritures de la meme langue dans la
        # liste feraient chercher deux fois ce qui a deja ete depose.
        vues: set[str] = set()
        normalisees: list[str] = []
        for brut in prefs.subtitles.languages:
            code = normalise_language(brut)
            if not is_language_code(code):
                raise PreferenceError(
                    f"« {brut} » n'est pas un code de langue reconnu. Attendu « fr », "
                    "« en », « pt-BR » — deux lettres, éventuellement suivies d'une région."
                )
            if code not in vues:
                vues.add(code)
                normalisees.append(code)
        prefs.subtitles.languages = normalisees

        try:
            VpnPolicy(prefs.vpn.policy)
        except ValueError as exc:
            raise PreferenceError(
                f"« {prefs.vpn.policy} » n'est pas une politique connue. Attendu : "
                f"{', '.join(m.value for m in VpnPolicy)}."
            ) from exc

        reference = prefs.vpn.reference_ip.strip()
        if reference:
            try:
                ip_address(reference)
            except ValueError as exc:
                raise PreferenceError(
                    f"« {reference} » n'est pas une adresse IP. Relève ton adresse publique "
                    "VPN COUPÉ — c'est celle-là qui sert de témoin."
                ) from exc
            prefs.vpn.reference_ip = reference

        for kind, tpl in prefs.templates.items():
            if kind not in KINDS:
                raise PreferenceError(f"type de média inconnu : {kind}")
            if not tpl.strip():
                continue
            try:
                validate(tpl)
            except TemplateError as exc:
                raise PreferenceError(f"gabarit invalide pour « {kind} » : {exc}") from exc

    def resolved_sources(self) -> list[Path]:
        """Sources effectivement a scanner."""
        prefs = self.load()
        available = self.all_sources(prefs)
        if not prefs.enabled_sources:
            return available
        selected = set(prefs.enabled_sources)
        return [p for p in available if str(p) in selected]

    def browse(self, raw: str | None) -> dict[str, object]:
        """Sous-dossiers d'un chemin, pour l'explorateur de l'interface.

        Sans argument, renvoie les racines montees. Toujours confine : on ne
        peut descendre que sous une racine declaree, et jamais remonter
        au-dessus.
        """
        if not raw:
            return {
                "path": None,
                "parent": None,
                "entries": [
                    {
                        "path": str(p),
                        "name": p.name or str(p),
                        "exists": p.is_dir(),
                        "is_root": True,
                        # Signale une zone qui est aussi une destination : y
                        # scanner sert a normaliser l'existant, pas a importer.
                        "is_library": p == self._library_root,
                    }
                    for p in self.allowed_areas
                ],
            }

        current = self._assert_under_a_root(raw)
        if not current.is_dir():
            raise PreferenceError(f"« {raw} » n'est pas un dossier accessible.")

        entries = []
        try:
            for child in sorted(current.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                entries.append(
                    {
                        "path": str(child),
                        "name": child.name,
                        "exists": True,
                        "is_root": False,
                        "is_library": False,
                    }
                )
        except OSError as exc:
            raise PreferenceError(f"lecture impossible : {exc}") from exc

        # Le parent n'est propose que s'il reste dans le perimetre autorise.
        parent: str | None = None
        try:
            parent = str(self._assert_under_a_root(str(current.parent)))
        except PreferenceError:
            parent = None

        return {"path": str(current), "parent": parent, "entries": entries}

    def destination_root(self, kind: str, size_bytes: int | None = None) -> Path:
        """Chemin absolu ou ranger ce type de media.

        ``size_bytes`` bascule vers la destination des fichiers volumineux
        quand elle est active et que le seuil est franchi.
        """
        prefs = self.load()
        over = prefs.oversize
        if (
            over.enabled
            and size_bytes is not None
            and size_bytes >= over.threshold_bytes()
            and over.destinations.get(kind)
        ):
            return resolve_within(self._library_root, over.destinations[kind])
        return resolve_within(self._library_root, prefs.destination_for(kind))
