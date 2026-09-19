"""Reencodage differe : la nuit, un fichier a la fois, sans rien remplacer.

Trois decisions structurent ce module, et elles decoulent toutes de la meme
observation : reencoder est **lent, couteux et destructeur**. Une heure de
processeur pour un film, plusieurs jours pour une bibliotheque, et une perte de
qualite qu'aucune annulation ne rendra.

1. **Le fichier d'origine n'est jamais touche pendant l'encodage.** Le resultat
   est ecrit A COTE. Une coupure de courant, un disque plein, un ffmpeg tue par
   l'OOM killer : dans tous les cas on perd du temps de calcul, jamais un film.

2. **Le remplacement est un geste humain, separe et posterieur.** Le lendemain,
   on regarde le resultat, et on decide. Un encodeur qui remplacerait tout seul
   demanderait de lui faire confiance sur une operation qu'on ne peut pas
   defaire — et il suffit d'un filtre mal choisi pour degrader deux cents
   episodes pendant la nuit.

3. **Un seul encodage a la fois.** Deux ffmpeg en parallele sur un NAS ne vont
   pas deux fois plus vite : ils se disputent le processeur et les disques, et
   rendent la machine inutilisable pour ce a quoi elle sert — servir des films.

La fenetre nocturne n'est pas un confort. C'est ce qui rend l'operation
acceptable : le NAS travaille quand personne ne regarde, et se tait quand
quelqu'un regarde.

La pause en est le complement pour l'imprevu : quelqu'un regarde un film a
23 h 30 et le NAS rame. Elle GELE ffmpeg (SIGSTOP) au lieu de l'arreter : le
processeur est rendu immediatement, et la reprise (SIGCONT) repart de l'image
exacte ou l'encodage en etait. Arreter puis relancer jetterait les heures deja
calculees — ffmpeg ne sait pas reprendre un fichier a moitie ecrit.

Le format de sortie est le HEVC 10 bits, pour tout. Le cas qui l'a impose :
des films 4K HDR de 21 Go reencodes en libx264 CRF 21 sont sortis en H.264
**10 bits** — rien n'imposait le format de pixel, une source 10 bits le
restait — que ni la television (Jellyfin sur Android TV), ni l'iPad, ni
l'iPhone, ni Firefox ne decodent par le materiel. Jellyfin convertissait donc
tout a la volee, chargement interminable en wifi, et le HDR etait perdu. Ces
quatre appareils decodent en revanche le HEVC 10 bits par le materiel : c'est
le seul format qui leur arrive tel quel, HDR compris, et le plus compact des
deux. Voir ``planifier`` pour ce qui est fait de chaque source, et ce qui est
refuse.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

from . import lecture
from .journal import Journal, MoveRecord, _move
from .lecture import Analyse, PisteAudio, TypeHdr
from .probe import probe_media
from .proprietaire import cree_dossier_d_accueil

logger = logging.getLogger(__name__)

STAGING_DIRNAME = ".sortilege-reencodage"
"""Ou atterrissent les fichiers encodes, EN ATTENTE de verification.

Sous la racine de bibliotheque, et non dans le dossier de donnees : le
remplacement doit etre un renommage instantane. Depuis un autre volume, ce
serait une copie de plusieurs gigaoctets — precisement ce que le reencodage
cherchait a economiser.
"""

# Ecart de duree tolere entre l'original et le resultat. Un encodage interrompu
# produit un fichier PLUS COURT et plus petit : sans cette verification, il
# ressemblerait a une reussite particulierement efficace, et on remplacerait un
# film entier par ses vingt premieres minutes.
DUREE_TOLERANCE = 0.02

# Au-dela, le resultat n'a pas tenu sa promesse. Reencoder pour gagner cinq
# pour cent, c'est perdre de la qualite pour rien.
GAIN_MINIMAL = 0.10

horloge = time.monotonic
"""Horloge des durees de pause. Monotone, parce qu'un changement d'heure ou une
correction NTP pendant la pause donnerait sinon une duree negative, ou de
plusieurs heures. Remplacee en test pour simuler le temps qui passe."""


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    """Encode et en attente de verification humaine."""

    FAILED = "failed"
    REPLACED = "replaced"
    DISCARDED = "discarded"


@dataclass
class Job:
    """Un reencodage demande, dans l'etat ou il se trouve."""

    id: str
    source: Path
    relative_path: str
    title: str
    target_height: int
    source_bytes: int
    budget_bytes: int = 0
    """Poids maximal souhaite. 0 = pas de consigne, encodage a qualite
    constante."""

    state: State = State.QUEUED
    output: Path | None = None
    output_bytes: int = 0
    source_duration: float = 0.0
    output_duration: float = 0.0
    progress: float = 0.0
    """0 -> 1. Calcule sur la duree encodee, seule mesure fiable : le nombre
    d'images depend du debit, et la taille du fichier ne progresse pas
    lineairement."""

    error: str = ""
    definitif: bool = False
    """Refus qui vaudra encore demain pour ce MEME fichier (Dolby Vision
    profil 5, par exemple). La file s'en souvient pour ne plus le proposer :
    sans cela, « tout mettre en file » le remettrait chaque soir, pour un
    refus identique chaque nuit."""

    notes: list[str] = field(default_factory=list)
    """Ce que l'encodage a fait de la source et qu'il faut savoir avant de
    remplacer : metadonnees HDR10+ ou Dolby Vision non conservees, pistes
    audio converties. Rien de tout cela n'est une erreur, mais le decouvrir
    apres avoir jete l'original serait trop tard."""

    queued_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str = ""
    finished_at: str = ""

    paused_seconds: float = 0.0
    """Temps passe suspendu, pauses CLOSES seulement (voir ``seconds_paused``).

    Sans ce compte, une pause de deux heures ferait croire a un encodage deux
    fois plus lent : la duree affichee et le temps restant seraient faux
    d'autant."""

    suspended_at: float | None = None
    """Debut de la suspension en cours, sur ``horloge`` (monotone). None quand
    ffmpeg n'est pas gele : seul le travail suspendu en porte un."""

    def seconds_paused(self, now: float | None = None) -> float:
        """Temps total en pause, y compris la pause EN COURS.

        La pause en cours compte des maintenant : l'interface qui rafraichit
        pendant la pause doit voir le temps d'encodage s'arreter, pas grimper
        jusqu'a la reprise puis retomber d'un coup.
        """
        if self.suspended_at is None:
            return self.paused_seconds
        instant = horloge() if now is None else now
        return self.paused_seconds + max(0.0, instant - self.suspended_at)

    @property
    def suspended(self) -> bool:
        return self.suspended_at is not None

    @property
    def savings_bytes(self) -> int:
        if self.state not in (State.DONE, State.REPLACED) or not self.output_bytes:
            return 0
        return max(0, self.source_bytes - self.output_bytes)

    @property
    def ratio(self) -> float:
        """Part du poids d'origine conservee. 0.35 = le fichier a fondu aux
        deux tiers."""
        if not self.source_bytes or not self.output_bytes:
            return 0.0
        return self.output_bytes / self.source_bytes


def new_job(
    source: Path,
    relative_path: str,
    title: str,
    target: str,
    size_bytes: int,
    budget_bytes: int = 0,
) -> Job:
    return Job(
        id=uuid.uuid4().hex[:12],
        source=source,
        relative_path=relative_path,
        title=title,
        target_height=int(target.rstrip("pi") or 0),
        source_bytes=size_bytes,
        budget_bytes=budget_bytes,
    )


# --- La fenetre nocturne ----------------------------------------------------


def in_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    """Sommes-nous dans la plage autorisee ?

    La plage traverse minuit dans le cas normal — 23 h a 7 h — ce qui interdit
    la comparaison naive ``start <= h < end``. Une plage 23-7 lue ainsi ne
    serait JAMAIS vraie, et le reencodage n'aurait jamais lieu sans que rien ne
    l'explique.
    """
    if start_hour == end_hour:
        return True  # plage vide == aucune restriction
    heure = now.hour
    if start_hour < end_hour:
        return start_hour <= heure < end_hour
    return heure >= start_hour or heure < end_hour


# --- La commande ------------------------------------------------------------


AUDIO_KBPS = 192
"""Debit suppose des pistes audio, qui sont COPIEES et non reencodees. On le
soustrait du budget : l'oublier ferait viser un poids total superieur a la
consigne, et le budget ne serait jamais tenu."""

DEBIT_PLANCHER = 300
"""En dessous, l'image devient une bouillie de blocs. Mieux vaut depasser le
budget et le DIRE que rendre un fichier inregardable en silence."""


def bitrate_for(budget_bytes: int, duration_seconds: float, *, audio_kbps: int = AUDIO_KBPS) -> int:
    """Debit video, en kbit/s, pour tenir un budget de poids.

    C'est la traduction de « un episode doit peser 500 Mo au plus » en une
    consigne que ffmpeg comprend. La resolution n'y suffirait pas : deux
    fichiers en 1080p peuvent peser 1,2 Go et 6 Go selon leur debit.

    Zero quand il n'y a pas de budget ou pas de duree connue : on retombe alors
    sur le mode a qualite constante, qui est le bon defaut.
    """
    if budget_bytes <= 0 or duration_seconds <= 0:
        return 0
    total_kbps = (budget_bytes * 8) / duration_seconds / 1000
    return max(DEBIT_PLANCHER, int(total_kbps - audio_kbps))


# --- Le format de sortie ----------------------------------------------------


ENCODEUR = "libx265"

LIBELLE_FORMAT = "HEVC 10 bits"
"""Ce que l'interface affiche du format produit : « HEVC 10 bits · CRF 21 »."""

CRF_DEFAUT = 21
"""Qualite constante du HEVC jusqu'au 1080p. 21 est le reglage auquel on ne
voit pas de difference avec la source a distance normale d'un televiseur :
c'est « sans perte de qualite » au sens ou l'utilisateur l'entend."""

CRF_MIN = 16
CRF_MAX = 28
"""Bornes du reglage. En dessous de 16, le fichier approche le poids de la
source sans que l'oeil y gagne ; au-dela de 28, les aplats et les visages se
degradent a l'oeil nu."""

ECART_CRF_UHD = 1
"""Un cran de CRF en plus au-dela du 1080p (22 par defaut). La finesse du 4K
masque davantage ce que ce cran retire, et ce cran vaut plusieurs gigaoctets
sur un film de deux heures."""

PRESETS = ("veryfast", "fast", "medium", "slow")
"""Presets x265 proposes. « slow » gagne encore quelques pour cent de place,
mais double a peu pres le temps d'encodage — sur un Celeron a quatre coeurs,
cela se compte en nuits."""

PRESET_DEFAUT = "medium"

HAUTEUR_HD = 1080

PLAFOND_HD = (20_000, 40_000)
PLAFOND_UHD = (40_000, 80_000)
"""vbv-maxrate et vbv-bufsize (kbit/s), jusqu'au 1080p puis au-dela.

Le CRF seul laisse passer des pointes a 50 ou 60 Mbit/s sur une scene de pluie
ou d'explosions : c'est la, en wifi, que la lecture s'arrete pour charger.
Plafonner ces pointes coute un peu de qualite sur quelques secondes, jamais
sur le reste du film, dont le debit reste tres en dessous."""

EAC3_DEBIT = "640k"
EAC3_CANAUX_MAX = 6
"""L'encodeur E-AC-3 de ffmpeg s'arrete au 5.1 : un 7.1 est replie en 5.1."""

_STATISTIQUES_MKV = ("BPS", "BPS-eng", "NUMBER_OF_BYTES", "NUMBER_OF_BYTES-eng")
"""Etiquettes de statistiques qu'ecrit mkvmerge, et que ffmpeg RECOPIE telles
quelles sur la piste reencodee. Un film de 60 Mbit/s reencode a 8 Mbit/s
porterait encore « BPS=60000000 » : Jellyfin lit cette etiquette pour decider
si le debit passe en wifi, et convertirait pour rien le fichier qu'on vient
justement de rendre lisible."""

MOTIF_DOLBY_VISION_5 = (
    "Dolby Vision profil 5 : pas de couche HDR10, le réencoder donnerait des couleurs "
    "violettes et vertes"
)

MOTIF_SANS_LIBX265 = (
    "Le ffmpeg de cette installation ne propose pas l'encodeur libx265 (HEVC) : rien n'a "
    "été encodé. L'image Docker de Sortilège l'inclut ; un ffmpeg installé à part doit être "
    "compilé avec libx265."
)

MOTIF_ANALYSE_INCOMPLETE = (
    "Analyse incomplète de la source : le HDR ne peut pas être garanti, réencodage reporté."
)

DESENTRELACEMENT = "bwdif=mode=send_frame:deint=interlaced,setfield=prog"
"""Tete de la chaine de filtres, pour toute source.

x265 n'encode que du progressif : une source entrelacee (1080i d'un
enregistrement TNT, d'un Blu-ray de concert) gardait ses « peignes » sur chaque
mouvement, et la reduction de taille melangeait les deux trames. ``deint=
interlaced`` ne traite QUE les images marquees entrelacees : une source
progressive ressort identique (PSNR mesure 48,535 avec et sans le filtre).
``send_frame`` garde le nombre d'images (50i -> 25p) : doubler la cadence
doublerait le debit. ``setfield=prog`` etiquette la sortie : sans lui, le
conteneur pouvait encore annoncer des trames (« tb ») sur une image qui n'en a
plus."""

SOUS_TITRES_EN_SRT = frozenset({"mov_text"})
"""Sous-titres texte que le Matroska refuse, convertis en SRT. mov_text est le
format texte du MP4 : copie tel quel, il faisait echouer CHAQUE nuit le
reencodage de tout MP4 sous-titre."""

_TYPES_IMAGES = {
    "mjpeg": ("image/jpeg", ".jpg"),
    "png": ("image/png", ".png"),
    "bmp": ("image/bmp", ".bmp"),
    "gif": ("image/gif", ".gif"),
    "webp": ("image/webp", ".webp"),
}
"""Type MIME et extension d'une image de couverture, d'apres son codec, quand
la source ne les declare pas (couverture d'un MP4)."""


def crf_pour(hauteur: int, crf: int = CRF_DEFAUT) -> int:
    """CRF effectif pour une hauteur de sortie : le reglage, plus un cran en 4K."""
    return crf + (ECART_CRF_UHD if hauteur > HAUTEUR_HD else 0)


def plafond_pour(hauteur: int) -> tuple[int, int]:
    """(vbv-maxrate, vbv-bufsize) en kbit/s pour une hauteur de sortie."""
    return PLAFOND_UHD if hauteur > HAUTEUR_HD else PLAFOND_HD


@dataclass(frozen=True, slots=True)
class Couleurs:
    """Primaires, transfert et matrice que l'on DECLARE — jamais ce que l'on convertit.

    Vide = ne rien declarer. Chaque valeur est ecrite deux fois : dans le flux
    (VUI x265) et dans le conteneur, ou Jellyfin et les televiseurs la lisent.
    """

    primaires: str = ""
    transfert: str = ""
    matrice: str = ""

    @property
    def vide(self) -> bool:
        return not (self.primaires or self.transfert or self.matrice)


PQ = Couleurs("bt2020", "smpte2084", "bt2020nc")
"""HDR10, HDR10+ et base des Dolby Vision 7 et 8.1."""

HLG = Couleurs("bt2020", "arib-std-b67", "bt2020nc")
"""HLG, et base des Dolby Vision 8.4."""

# Valeurs SDR que ffprobe, ffmpeg (setparams, -color_*), swscale et x265
# nomment tous pareil. Une valeur hors de ces listes n'est pas recopiee : mal
# nommee, elle ferait echouer l'encodage ; inconnue de swscale, aussi.
_PRIMAIRES_SDR = frozenset(
    {"bt709", "bt470m", "bt470bg", "smpte170m", "smpte240m", "film", "bt2020"}
)
_TRANSFERTS_SDR = frozenset(
    {"bt709", "smpte170m", "smpte240m", "linear", "bt2020-10", "bt2020-12", "iec61966-2-1"}
)
_MATRICES_SDR = frozenset({"bt709", "fcc", "bt470bg", "smpte170m", "smpte240m", "bt2020nc"})


@dataclass(frozen=True, slots=True)
class ConversionAudio:
    """Une piste audio convertie en E-AC-3 plutot que copiee."""

    indice: int
    """Rang parmi les pistes audio. Toutes sont mappees dans l'ordre de la
    source (``-map 0:a?``) : c'est aussi leur rang en sortie (``a:N``)."""

    origine: str
    """« TrueHD 7.1 » : ce qu'elle etait, pour le dire dans les notes."""

    canaux: int | None = None

    @property
    def repliee(self) -> bool:
        """Plus de canaux que l'E-AC-3 n'en porte : repliee en 5.1."""
        return (self.canaux or 0) > EAC3_CANAUX_MAX


@dataclass(slots=True)
class PlanEncodage:
    """Ce que l'encodage fera d'UNE source, decide avant de lancer ffmpeg.

    Le plan vide (source non analysee) ne declare rien et copie tout l'audio :
    ffmpeg recopie alors les etiquettes de couleur du flux, sans metadonnees
    HDR ajoutees.
    """

    couleurs: Couleurs = field(default_factory=Couleurs)
    hdr: bool = False
    master_display: str | None = None
    """Au format x265, depuis ``lecture.Mastering.pour_x265`` — jamais invente."""

    max_cll: str | None = None
    """« MaxCLL,MaxFALL », depuis ``lecture.LumiereContenu.pour_x265``."""

    dolby_vision: bool = False
    """Source Dolby Vision : on encode sa couche de base, et on interdit a
    ffmpeg d'y recoller des metadonnees Dolby Vision (``-dolbyvision 0``)."""

    audio: list[ConversionAudio] = field(default_factory=list)
    sous_titres_srt: list[int] = field(default_factory=list)
    """Rangs (``s:N``) des pistes de sous-titres converties en SRT (voir
    ``SOUS_TITRES_EN_SRT``). Toutes sont mappees dans l'ordre de la source : le
    rang est le meme en sortie."""

    notes: list[str] = field(default_factory=list)
    refus: str = ""
    definitif: bool = False
    """Le refus vaudra pour ce fichier tant qu'il ne change pas (voir
    ``Job.definitif``)."""


@dataclass(frozen=True, slots=True)
class ImageJointe:
    """Une image de couverture de la source (piste video ``attached_pic``).

    ffmpeg presente la couverture jointe d'un MKV (ou d'un MP4) comme une piste
    VIDEO, que ``-map 0:t?`` ne prend pas ; et la copier comme piste la
    reecrirait en piste video MJPEG d'une image, que les lecteurs verraient
    comme un second film. Elle est donc extraite puis rejointe par ``-attach``,
    ce qui la rend a l'identique, nom et type compris.
    """

    rang: int
    """Rang parmi TOUTES les pistes video de la source : ``0:v:<rang>``."""

    nom: str
    """Nom de piece jointe a declarer (« cover.jpg »)."""

    type_mime: str
    extension: str
    """Extension du fichier extrait, d'apres le codec (« .jpg »)."""


def audio_a_compresser(piste: PisteAudio) -> bool:
    """Cette piste merite-t-elle d'etre convertie en E-AC-3 640 kbit/s ?

    Les pistes sans perte (TrueHD, DTS-HD MA, PCM, FLAC multicanal) pesent
    3 a 6 Mbit/s, autant que la video reencodee : c'est la que se trouvent les
    3 a 4 Go restants d'un film. Le DTS « coeur » (768 ou 1 509 kbit/s) aussi,
    dans une moindre mesure.

    Restent copiees : ce qui est deja compact (AC-3, E-AC-3 — Atmos compris —,
    AAC, Opus…), le FLAC stereo (quelques centaines de kbit/s, rien a gagner),
    et le DTS Express, plus leger que l'E-AC-3 qu'il deviendrait.
    """
    codec = piste.codec
    if codec in ("truehd", "mlp") or codec.startswith("pcm_"):
        return True
    if codec == "dts":
        return piste.profil != "DTS Express"
    if codec == "flac":
        return (piste.canaux or 0) > 2
    return False


def _couleurs_sdr(analyse: Analyse) -> Couleurs:
    """Ce que la source SDR declare, complete comme un lecteur l'aurait suppose.

    Une valeur declaree est gardee telle quelle. Une valeur absente d'une
    source HD vaut BT.709 : c'est ce que tout lecteur supposait en la lisant,
    et on l'ecrit pour que la version reduite soit lue pareil — un 1080p non
    etiquete ramene en 480p serait sinon lu en BT.601, couleurs decalees. En
    definition standard, rien n'est ajoute : le lecteur fera la meme
    supposition qu'avant.
    """
    hd = (analyse.hauteur or 0) >= 720
    defaut = "bt709" if hd else ""
    return Couleurs(
        primaires=analyse.primaires if analyse.primaires in _PRIMAIRES_SDR else defaut,
        transfert=analyse.transfert if analyse.transfert in _TRANSFERTS_SDR else defaut,
        matrice=analyse.matrice if analyse.matrice in _MATRICES_SDR else defaut,
    )


_LIBELLES_BASE = {"hdr10": "HDR10", "hlg": "HLG", "sdr": "SDR"}

NOTE_HDR10PLUS = (
    "HDR10+ : les métadonnées dynamiques (réglées scène par scène) ne sont pas "
    "conservées, ffmpeg ne sait pas les recopier. La base HDR10 et ses métadonnées "
    "statiques le sont : l'image reste HDR partout, sans l'ajustement scène par scène."
)


def _signal(analyse: Analyse, plan: PlanEncodage) -> str:
    """Le signal a encoder : « hdr10 », « hlg » ou « sdr ». Vide = refus pose dans le plan.

    Le Dolby Vision decide en premier, parce que c'est lui qui peut detruire
    le film : un profil 5 n'a AUCUNE couche compatible, son signal est en
    IPTPQc2, et le reencoder comme du HDR10 donne une image violette et verte
    sans une ligne d'erreur dans les journaux.
    """
    genre = analyse.hdr
    if genre is TypeHdr.DOLBY_VISION and analyse.dolby_vision is not None:
        dv = analyse.dolby_vision
        plan.dolby_vision = True
        if dv.profil == 5:
            plan.refus, plan.definitif = MOTIF_DOLBY_VISION_5, True
            return ""
        base = dv.base
        if base is None:
            plan.refus = (
                f"{dv.libelle} : {dv.explication}. Sans savoir ce que voit un appareil "
                "sans Dolby Vision, le réencoder risquerait de fausser les couleurs : "
                "le fichier est laissé tel quel."
            )
            plan.definitif = True
            return ""
        note = (
            f"{dv.libelle} : seule la couche de base {_LIBELLES_BASE[base]} est réencodée, "
            "les métadonnées Dolby Vision ne sont pas conservées. Un appareil sans Dolby "
            "Vision voyait déjà exactement cette image."
        )
        if dv.profil == 7:
            note += " La couche d'amélioration du Blu-ray UHD est ignorée."
        plan.notes.append(note)
        if analyse.hdr10plus and base == "hdr10":
            # Les sorties « DV + HDR10+ » sont courantes : les deux pertes se disent.
            plan.notes.append(NOTE_HDR10PLUS)
        return base
    if genre is TypeHdr.HDR10PLUS:
        plan.notes.append(NOTE_HDR10PLUS)
        return "hdr10"
    if genre is TypeHdr.HDR10:
        return "hdr10"
    if genre is TypeHdr.HLG:
        return "hlg"
    return "sdr"


def planifier(
    analyse: Analyse | None,
    *,
    encodeurs: frozenset[str] | None = None,
    compresser_audio: bool = False,
) -> PlanEncodage:
    """Decide, AVANT d'encoder, ce qui sera fait de cette source — ou pourquoi pas.

    Pure : aucune commande n'est lancee ici, tout vient de l'analyse
    (``lecture.analyser``) et de la liste des encodeurs, ce qui rend chaque
    cas verifiable sans film ni ffmpeg.

    - **HDR10, HDR10+, HLG** : memes primaires, transfert et matrice, et les
      metadonnees statiques (mastering, MaxCLL) recopiees quand la source les
      porte. Sans elles, le televiseur ne sait pas doser la luminosite et
      l'image sort terne ou brulee.
    - **Dolby Vision** : la couche de base si elle est exploitable (7 et 8.1 :
      HDR10 ; 8.4 : HLG), en le disant. Profil 5 ou base inconnue : refus
      definitif.
    - **SDR** : HEVC 10 bits aussi — les 10 bits evitent les bandes dans les
      degrades (ciel, fondus au noir) — avec les couleurs de la source.

    ``encodeurs`` None = liste illisible : on ne refuse pas sur une
    supposition, ffmpeg dira lui-meme ce qui lui manque. Une analyse
    illisible, elle, fait refuser : sans savoir si la source est un Dolby
    Vision profil 5, l'encoder serait jouer ses couleurs a pile ou face. Une
    analyse PARTIELLE aussi, quand le conteneur laisse la place a du HDR (voir
    ``_hdr_non_exclu``) — mais pas pour de bon : la prochaine lecture de la
    premiere image peut reussir.
    """
    plan = PlanEncodage()
    if encodeurs is not None and ENCODEUR not in encodeurs:
        plan.refus = MOTIF_SANS_LIBX265
        return plan
    if analyse is None:
        return plan
    if not analyse.lue:
        plan.refus = (
            f"Analyse de la source impossible ({analyse.motif_illisible or 'ffprobe muet'}) : "
            "sans savoir si elle est HDR ou Dolby Vision, la réencoder risquerait d'en "
            "fausser les couleurs."
        )
        return plan
    if not analyse.a_video:
        plan.refus, plan.definitif = "Aucune piste vidéo dans ce fichier : rien à réencoder.", True
        return plan

    # Le Dolby Vision passe d'abord : son enregistrement vit dans le conteneur,
    # et un profil 5 reste un refus definitif meme sans premiere image.
    signal_ = _signal(analyse, plan)
    if plan.refus:
        return plan
    if not analyse.premiere_image_lue and _hdr_non_exclu(analyse):
        return PlanEncodage(refus=MOTIF_ANALYSE_INCOMPLETE)

    if signal_ == "sdr":
        plan.couleurs = _couleurs_sdr(analyse)
    else:
        plan.couleurs = PQ if signal_ == "hdr10" else HLG
        plan.hdr = True
        plan.master_display = analyse.mastering.pour_x265() if analyse.mastering else None
        plan.max_cll = analyse.lumiere.pour_x265() if analyse.lumiere else None
        absents = [
            nom
            for nom, valeur in (
                ("métadonnées de mastering", plan.master_display),
                ("MaxCLL/MaxFALL", plan.max_cll),
            )
            if valeur is None
        ]
        if absents:
            plan.notes.append(
                f"La source ne déclare pas de {' ni de '.join(absents)} : rien n'est inventé, "
                "le téléviseur appliquera ses valeurs par défaut."
            )

    if compresser_audio:
        _planifier_audio(analyse, plan, eac3=encodeurs is None or "eac3" in encodeurs)
    _planifier_sous_titres(analyse, plan)
    return plan


def _hdr_non_exclu(analyse: Analyse) -> bool:
    """Le conteneur, lu SANS la premiere image, laisse-t-il une place au HDR ?

    Sans premiere image, l'analyse ne sait que ce que dit le conteneur. Or
    dans un MKV ecrit par ffmpeg, la courbe PQ ou HLG et les metadonnees HDR10
    ne vivent souvent QUE dans cette image, le conteneur ne portant au mieux
    que la matrice ``bt2020nc`` : un HDR y passe pour du SDR, et l'encoder
    comme tel l'aplatirait sans un mot. Seul un conteneur qui declare un
    transfert connu, des couleurs hors BT.2020 et 8 bits exclut le HDR — le
    HDR10 et le HLG sont en pratique du 10 bits BT.2020. Un 8 bits sans aucune
    etiquette est donc reporte lui aussi : rien ne prouve qu'il soit SDR, et
    reporter ne coute qu'une nuit.
    """
    return (
        analyse.primaires.startswith("bt2020")
        or analyse.matrice.startswith("bt2020")
        or not analyse.transfert
        or (analyse.profondeur or 0) > 8
    )


def _planifier_sous_titres(analyse: Analyse, plan: PlanEncodage) -> None:
    plan.sous_titres_srt = [
        rang for rang, piste in enumerate(analyse.sous_titres) if piste.codec in SOUS_TITRES_EN_SRT
    ]
    if plan.sous_titres_srt:
        plan.notes.append(
            "Sous-titres au format MP4 (mov_text) convertis en SRT : le MKV n'accepte pas "
            "ce format. Le texte est identique."
        )


def _origine(piste: PisteAudio) -> str:
    """« TrueHD 7.1 », « DTS-HD MA 5.1 », « PCM 7.1 » (et non « PCM_S24LE 7.1 »)."""
    if piste.codec.startswith("pcm_"):
        return piste.format.replace(piste.codec.upper(), "PCM", 1)
    return piste.format


def _planifier_audio(analyse: Analyse, plan: PlanEncodage, *, eac3: bool) -> None:
    pistes = [
        ConversionAudio(indice=i, origine=_origine(piste), canaux=piste.canaux)
        for i, piste in enumerate(analyse.audio)
        if audio_a_compresser(piste)
    ]
    if not pistes:
        return
    if not eac3:
        plan.notes.append(
            "Compression de l'audio demandée, mais ce ffmpeg n'a pas d'encodeur E-AC-3 : "
            "l'audio est copié tel quel."
        )
        return
    plan.audio = pistes
    converties = ", ".join(p.origine for p in pistes)
    note = (
        f"Audio compressé en E-AC-3 {EAC3_DEBIT.removesuffix('k')} kbit/s : {converties}. "
        "Un Atmos ou un DTS:X éventuel est perdu ; les autres pistes sont copiées."
    )
    if any(p.repliee for p in pistes):
        note += " Les pistes 7.1 sont repliées en 5.1."
    plan.notes.append(note)


def filtre_echelle(hauteur: int) -> str:
    """Ramene l'image DANS une boite 16:9 de cette hauteur, sans jamais l'agrandir.

    Le cas qui compte : un film en 2,39:1, soit 3840x1600 en 4K. ``scale=-2:1080``
    le rendait en 2592x1080 — pas du 1080p, un tiers de pixels en plus a
    encoder et a stocker. Borne aussi en largeur, il sort en 1920x800.

    ``min()`` empeche d'agrandir une source deja plus petite que la boite, et
    ``force_divisible_by=2`` garde des dimensions paires : HEVC refuse une
    largeur impaire des la premiere image.
    """
    largeur = round(hauteur * 16 / 9 / 2) * 2
    return (
        f"scale=w='min(iw,{largeur})':h='min(ih,{hauteur})'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def parametres_x265(plan: PlanEncodage, hauteur: int, bitrate_kbps: int = 0) -> str:
    """La chaine ``-x265-params`` : HDR, couleurs, plafond de debit.

    Tout ce qui touche au HDR passe ici plutot que par les options generiques
    de ffmpeg : c'est la seule voie qui atteint a coup sur les messages SEI
    que lisent les televiseurs, quelle que soit la version de ffmpeg.
    """
    elements: list[str] = []
    if plan.hdr:
        # hdr10=1 ecrit les SEI HDR10 meme sans valeur connue (0 = « inconnu »,
        # prevu par la norme) ; repeat-headers les repete a chaque image-cle,
        # pour un lecteur qui prend le flux en cours de route.
        elements += ["hdr10=1", "repeat-headers=1"]
    couleurs = plan.couleurs
    if couleurs.primaires:
        elements.append(f"colorprim={couleurs.primaires}")
    if couleurs.transfert:
        elements.append(f"transfer={couleurs.transfert}")
    if couleurs.matrice:
        elements.append(f"colormatrix={couleurs.matrice}")
    if plan.hdr and plan.master_display:
        elements.append(f"master-display={plan.master_display}")
    if plan.hdr and plan.max_cll:
        elements.append(f"max-cll={plan.max_cll}")
    if bitrate_kbps > 0:
        # Mode budget : debit cible, pointes bornees a 1,5 fois, tampon de
        # deux fois — sans quoi une scene chargee ferait depasser le poids
        # que la moyenne respectait.
        elements += [
            f"bitrate={bitrate_kbps}",
            f"vbv-maxrate={int(bitrate_kbps * 1.5)}",
            f"vbv-bufsize={bitrate_kbps * 2}",
        ]
    else:
        maximum, tampon = plafond_pour(hauteur)
        elements += [f"vbv-maxrate={maximum}", f"vbv-bufsize={tampon}"]
    # x265 ecrit sinon une vingtaine de lignes d'information sur stderr, et
    # la derniere — celle qu'on affiche en cas d'echec — n'en serait pas une.
    elements.append("log-level=error")
    return ":".join(elements)


def build_command(
    source: Path,
    output: Path,
    height: int,
    *,
    crf: int = CRF_DEFAUT,
    preset: str = PRESET_DEFAUT,
    bitrate_kbps: int = 0,
    plan: PlanEncodage | None = None,
    images: Sequence[tuple[ImageJointe, Path]] = (),
    pieces_jointes: int = 0,
) -> list[str]:
    """Argv complet, sans shell. ``crf`` est le reglage 1080p : le 4K prend un cran de plus.

    Toujours du HEVC 10 bits (``yuv420p10le``, profil Main 10) : c'est ce qui
    rend le fichier lisible tel quel par la television, l'iPad, l'iPhone et
    Firefox. ``-tag:v hvc1`` est l'etiquette qu'exigent les appareils Apple,
    mais le MKV ne la stocke pas : un remuxage en MP4 par ``-c copy`` ecrit
    ``hev1``, que ces appareils refusent. Il faut la redemander a ce moment-la
    (``-c copy -tag:v hvc1``).

    Seule la video principale est encodee (``0:V:0`` : la premiere piste video
    qui n'est pas une couverture). Ce qui est copie plutot que reencode compte
    autant que le reste :

    - **l'audio**, par defaut : aucune perte, et il pese peu face a une video
      4K. Seule l'option « compresser l'audio » convertit les pistes sans perte
      (voir ``audio_a_compresser``) ;
    - **les sous-titres et les pieces jointes**, sans quoi les polices des
      sous-titres ASS disparaitraient et l'affichage deviendrait illisible.
      Seul le mov_text du MP4, que le Matroska refuse, est converti en SRT ;
    - **les couvertures** (``images``, extraites par ``run``), rejointes par
      ``-attach`` avec leur nom et leur type : ``pieces_jointes`` est le nombre
      de pieces jointes deja copiees, que les nouvelles suivent (``t:N``).

    Les couleurs declarees (voir ``Couleurs``) le sont aussi EN ENTREE, par
    ``setparams`` : depuis ffmpeg 7, une etiquette de sortie differente de
    celle de l'entree fait CONVERTIR l'image. Une source non etiquetee,
    supposee BT.601 par ffmpeg, aurait ete repeinte en BT.709 — couleurs
    decalees sur tout le film.
    """
    if Path(output).resolve() == Path(source).resolve():
        # ``-y`` ecraserait la source. Rien ne doit pouvoir y mener.
        raise ValueError("la sortie d'un réencodage ne peut pas être sa source")
    plan = plan or PlanEncodage()
    couleurs = plan.couleurs

    declarees = [
        (option_setparams, option_ffmpeg, valeur)
        for option_setparams, option_ffmpeg, valeur in (
            ("color_primaries", "-color_primaries", couleurs.primaires),
            ("color_trc", "-color_trc", couleurs.transfert),
            ("colorspace", "-colorspace", couleurs.matrice),
        )
        if valeur
    ]
    filtres = [DESENTRELACEMENT]
    if declarees:
        filtres.append("setparams=" + ":".join(f"{nom}={val}" for nom, _opt, val in declarees))
    if height > 0:
        filtres.append(filtre_echelle(height))

    commande = [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        # Les pistes optionnelles sont marquees « ? » : un fichier sans
        # sous-titres ne doit pas faire echouer l'encodage.
        "-map",
        "0:V:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map",
        "0:t?",
    ]
    for _image, chemin in images:
        commande += ["-attach", str(chemin)]
    commande += [
        "-c:v",
        ENCODEUR,
        "-preset",
        preset,
        "-profile:v",
        "main10",
        "-pix_fmt",
        "yuv420p10le",
        "-tag:v",
        "hvc1",
        # Deux modes exclusifs. A qualite constante (CRF) par defaut, pointes
        # plafonnees pour le wifi (voir ``parametres_x265``). A debit cible
        # quand un budget est fixe — « 500 Mo au plus » est une consigne de
        # POIDS, et le CRF ne sait pas la tenir.
        *([] if bitrate_kbps > 0 else ["-crf", str(crf_pour(height, crf))]),
        *(["-dolbyvision", "0"] if plan.dolby_vision else []),
    ]
    for _nom, option, valeur in declarees:
        commande += [option, valeur]
    commande += ["-x265-params", parametres_x265(plan, height, bitrate_kbps)]
    commande += ["-vf", ",".join(filtres)]
    commande += _sans_statistiques("v:0")

    commande += ["-c:a", "copy"]
    for piste in plan.audio:
        flux = f"a:{piste.indice}"
        commande += [f"-c:{flux}", "eac3", f"-b:{flux}", EAC3_DEBIT]
        if piste.repliee:
            commande += [f"-ac:{flux}", str(EAC3_CANAUX_MAX)]
        commande += _sans_statistiques(flux)

    commande += ["-c:s", "copy"]
    for rang in plan.sous_titres_srt:
        commande += [f"-c:s:{rang}", "srt"]
    for decalage, (image, _chemin) in enumerate(images):
        flux = f"t:{pieces_jointes + decalage}"
        commande += [
            f"-metadata:s:{flux}",
            f"mimetype={image.type_mime}",
            f"-metadata:s:{flux}",
            f"filename={image.nom}",
        ]

    return [
        *commande,
        "-progress",
        "pipe:1",
        "-loglevel",
        "error",
        str(output),
    ]


def _sans_statistiques(flux: str) -> list[str]:
    """Efface les statistiques mkvmerge recopiees de la source (voir ``_STATISTIQUES_MKV``).

    Cle par cle, et non ``-map_metadata:s`` : ce dernier coupe la recopie
    automatique des etiquettes de TOUTES les pistes, et les pistes audio
    perdraient leur langue — Jellyfin ne saurait plus laquelle est la VF.
    """
    arguments: list[str] = []
    for cle in _STATISTIQUES_MKV:
        arguments += [f"-metadata:s:{flux}", f"{cle}="]
    return arguments


def staging_root(library_root: Path) -> Path:
    return library_root / STAGING_DIRNAME


def output_path(job: Job, library_root: Path) -> Path:
    """Ou ecrire le resultat.

    Toujours en Matroska : c'est le seul conteneur courant qui accepte
    l'ensemble des pistes qu'on recopie — plusieurs audios, sous-titres,
    polices. Ecrire un AVI reencode dans un AVI perdrait tout le reste.

    Le dossier de reencodage est cree comme un dossier d'accueil (voir
    ``core/proprietaire``) : il vit DANS la bibliotheque, et un reencodage
    interrompu y laisse un fichier a demi ecrit que l'utilisateur doit pouvoir
    retirer lui-meme. Cree par root, il l'en empecherait.
    """
    dossier = staging_root(library_root)
    cree_dossier_d_accueil(dossier)
    return dossier / f"{job.id}-{job.target_height}p.mkv"


# --- L'execution ------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


ENCODEURS_DELAI = 20.0

_LIGNE_ENCODEUR = re.compile(r"^\s*[VAS][.A-Z]{5}\s+(\S+)")
_ENCODEURS: dict[tuple[str, int], frozenset[str]] = {}


def lire_encodeurs(texte: str) -> frozenset[str] | None:
    """Les noms listes par ``ffmpeg -encoders``. None si ce n'est pas une telle liste.

    None et non un ensemble vide : une sortie qu'on ne reconnait pas (ffmpeg
    tronque, binaire remplace) ne prouve pas l'absence de libx265, et refuser
    sur cette base bloquerait toute la file pour une supposition.
    """
    _entete, separateur, corps = texte.partition("------")
    if not separateur:
        return None
    noms = {m.group(1) for ligne in corps.splitlines() if (m := _LIGNE_ENCODEUR.match(ligne))}
    return frozenset(noms) or None


def encodeurs_ffmpeg(binaire: str) -> frozenset[str] | None:
    """Ce que ce ffmpeg sait encoder, lu une fois par binaire. Ne leve jamais.

    Memorise sur (chemin, date de modification) : la liste ne change pas tant
    que le binaire ne change pas, et la relire avant chaque film serait un
    processus de plus pour rien. Un echec n'est PAS memorise — un NAS charge
    qui depasse le delai une fois doit pouvoir repondre la fois suivante.
    """
    try:
        cle = (binaire, os.stat(binaire).st_mtime_ns)
    except OSError:
        return None
    if cle in _ENCODEURS:
        return _ENCODEURS[cle]
    try:
        sortie = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            [binaire, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=ENCODEURS_DELAI,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("liste des encodeurs illisible (%s)", type(exc).__name__)
        return None
    encodeurs = lire_encodeurs(sortie.stdout)
    if encodeurs is not None:
        _ENCODEURS[cle] = encodeurs
    return encodeurs


@dataclass(frozen=True, slots=True)
class Preparation:
    """Ce qu'on sait AVANT de lancer ffmpeg : ce qu'est la source, ce que sait l'encodeur."""

    analyse: Analyse
    encodeurs: frozenset[str] | None = None
    images: tuple[ImageJointe, ...] = ()
    """Couvertures a rejoindre au resultat (voir ``ImageJointe``)."""

    pieces_jointes: int = 0
    """Pieces jointes (polices…) de la source, copiees par ``0:t?`` : les
    couvertures rejointes se rangent apres elles."""


def preparer(source: Path) -> Preparation:
    """Analyse la source et lit les encodeurs disponibles. Ne leve jamais.

    Separe de ``run`` pour que la decision (``planifier``) reste verifiable
    sans film : la boucle de nuit prepare, puis lance. L'analyse est celle du
    lecteur de verification (``lecture.analyser``) — une seule lecture d'un
    meme fichier, sinon les deux descriptions finiraient par diverger. Les
    couvertures, que cette analyse ne retient pas, sont lues a part.
    """
    binaire = shutil.which("ffmpeg")
    images, pieces_jointes = _sonder_jointes(source)
    return Preparation(
        analyse=lecture.analyser(source),
        encodeurs=encodeurs_ffmpeg(binaire) if binaire else None,
        images=images,
        pieces_jointes=pieces_jointes,
    )


def lire_jointes(sortie: dict) -> tuple[tuple[ImageJointe, ...], int]:
    """(couvertures, nombre de pieces jointes) d'apres ``ffprobe -show_streams``. Pure.

    Une couverture dont on ne connait pas le type MIME est laissee de cote :
    le Matroska refuse une piece jointe sans type, et l'encodage entier
    echouerait pour une image.
    """
    images: list[ImageJointe] = []
    pieces_jointes = 0
    rang_video = -1
    for flux in sortie.get("streams") or []:
        genre = flux.get("codec_type")
        if genre == "attachment":
            pieces_jointes += 1
        if genre != "video":
            continue
        rang_video += 1
        if not (flux.get("disposition") or {}).get("attached_pic"):
            continue
        type_defaut, extension = _TYPES_IMAGES.get(str(flux.get("codec_name") or ""), ("", ""))
        tags = {str(cle).lower(): str(valeur) for cle, valeur in (flux.get("tags") or {}).items()}
        type_mime = tags.get("mimetype") or type_defaut
        if not type_mime:
            logger.warning("couverture de type inconnu (%s) ignoree", flux.get("codec_name"))
            continue
        nom = tags.get("filename") or f"cover{extension}"
        extension = extension or Path(nom).suffix or ".img"
        images.append(ImageJointe(rang_video, nom, type_mime, extension))
    return tuple(images), pieces_jointes


def _sonder_jointes(source: Path) -> tuple[tuple[ImageJointe, ...], int]:
    """Couvertures et pieces jointes de la source. Ne leve jamais : rien = aucune.

    Un echec coute la couverture, jamais l'encodage : c'etait le sort de toutes
    les couvertures avant cette lecture.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return (), 0
    try:
        sortie = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "stream=index,codec_type,codec_name"
                ":stream_disposition=attached_pic:stream_tags=filename,mimetype",
                "-of",
                "json",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=lecture.FFPROBE_DELAI,
            check=False,
        )
        return lire_jointes(json.loads(sortie.stdout or "{}"))
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError) as exc:
        logger.warning("couvertures illisibles (%s) : %s", type(exc).__name__, source.name)
        return (), 0


def run(
    job: Job,
    library_root: Path,
    *,
    crf: int = CRF_DEFAUT,
    preset: str = PRESET_DEFAUT,
    compresser_audio: bool = False,
    preparation: Preparation | None = None,
    codec: str | None = None,
) -> Job:
    """Encode un fichier. Bloquant : appele depuis un fil dedie.

    Ne leve jamais. Un encodage rate est un etat du travail, pas une exception
    a remonter : la file doit continuer avec le suivant.

    ``preparation`` (voir ``preparer``) est ce qui permet de refuser AVANT de
    commencer — Dolby Vision profil 5, libx265 absent, source illisible — et
    de conserver le HDR. Absente, elle est faite ici : un encodage sans elle
    partait sans verification, et un Dolby Vision profil 5 sortait violet et
    vert. La boucle de nuit la fait elle-meme, pour la lire avant de lancer.

    ``codec`` n'est plus lu : tout reencodage est en HEVC 10 bits. Il reste
    accepte pour les appelants ecrits avant ce choix.
    """
    del codec
    binaire = shutil.which("ffmpeg")
    if binaire is None:
        return _echec(job, "ffmpeg absent de l'image")

    if not job.source.is_file():
        return _echec(job, "fichier source introuvable")

    preparation = preparation or preparer(job.source)
    plan = planifier(
        preparation.analyse,
        encodeurs=preparation.encodeurs,
        compresser_audio=compresser_audio,
    )
    if plan.refus:
        job.definitif = plan.definitif
        return _echec(job, plan.refus)
    job.notes = list(plan.notes)
    for note in job.notes:
        logger.info("reencodage %s : %s", job.relative_path, note)

    job.state = State.RUNNING
    job.started_at = datetime.now(UTC).isoformat()
    job.source_duration = probe_media(job.source).duration_seconds or 0.0
    sortie = output_path(job, library_root)
    job.output = sortie

    # Tout ce qui s'ecrit a cote du resultat pendant l'encodage, efface apres.
    # Un redemarrage en plein travail les laisse : ``purge_staging`` les
    # ramasse avec le fichier partiel.
    journal = sortie.with_suffix(".ffmpeg.log")
    images = _extraire_images(binaire, job, preparation.images, sortie)
    try:
        debit = bitrate_for(job.budget_bytes, job.source_duration)
        crf = min(CRF_MAX, max(CRF_MIN, crf))
        commande = build_command(
            job.source,
            sortie,
            job.target_height,
            crf=crf,
            preset=preset if preset in PRESETS else PRESET_DEFAUT,
            bitrate_kbps=debit,
            plan=plan,
            images=images,
            pieces_jointes=preparation.pieces_jointes,
        )
        commande[0] = binaire
        logger.info(
            "reencodage demarre : %s -> %sp HEVC 10 bits%s",
            job.relative_path,
            job.target_height,
            f", debit vise {debit} kbit/s"
            if debit
            else f", qualite constante (CRF {crf_pour(job.target_height, crf)}, "
            f"pointes plafonnees a {plafond_pour(job.target_height)[0]} kbit/s)",
        )
        code, erreur = _executer(job, commande, journal)
    finally:
        journal.unlink(missing_ok=True)
        for _image, chemin in images:
            chemin.unlink(missing_ok=True)

    if code != 0:
        # Le fichier partiel est retire : le laisser ferait croire a un
        # resultat disponible, et il occuperait la place qu'on cherchait a
        # gagner.
        sortie.unlink(missing_ok=True)
        return _echec(job, erreur or f"ffmpeg a echoue (code {code})")

    return _conclure(job, sortie)


def _executer(job: Job, commande: list[str], journal: Path) -> tuple[int, str]:
    """Lance ffmpeg, suit sa progression, et rend (code de retour, ligne d'erreur).

    La sortie d'erreur va dans un FICHIER, et non dans un tube : personne ne
    lit le tube avant la fin, et au-dela de 64 Ko de messages — une source
    abimee en ecrit des centaines de kilo-octets — ffmpeg s'y bloquait pour
    toujours. Le travail restait « en cours » et la file entiere attendait un
    redemarrage. Le fichier est relu une fois ffmpeg termine, par sa fin.
    """
    try:
        erreurs = journal.open("w+b")
    except OSError as exc:
        return -1, f"journal de ffmpeg impossible à créer : {exc}"
    with erreurs:
        try:
            processus = subprocess.Popen(  # noqa: S603 - argv fixe, pas de shell
                commande,
                stdout=subprocess.PIPE,
                stderr=erreurs,
                text=True,
            )
        except OSError as exc:
            return -1, f"lancement impossible : {exc}"

        _prendre_en_charge(job, processus)
        try:
            _suivre(job, processus)
            code = processus.wait()
        finally:
            _lacher(job)
        return code, (ligne_d_erreur(_fin_du_journal(erreurs)) if code != 0 else "")


FIN_DU_JOURNAL = 64 * 1024
"""Ce qu'on relit du journal de ffmpeg. La cause d'un echec est dans les
dernieres lignes ; une source abimee peut en ecrire des megaoctets avant."""


def _fin_du_journal(fichier: BinaryIO, taille: int = FIN_DU_JOURNAL) -> str:
    fin = fichier.seek(0, os.SEEK_END)
    fichier.seek(max(0, fin - taille))
    texte = fichier.read().decode("utf-8", errors="replace")
    if fin > taille:
        # La premiere ligne lue est coupee en son milieu.
        texte = texte.partition("\n")[2]
    return texte


_CONSEQUENCES = (
    "Nothing was written into output file",
    "Conversion failed",
    "Task finished with error code",
    "Terminating thread with return code",
    "Error sending frames to consumers",
    "Could not write header",
    "Error opening output file",
    "Error initializing output stream",
    "Error while opening encoder",
    "Could not open encoder before EOF",
)
"""Lignes que ffmpeg ecrit APRES la cause d'un echec, en le propageant : elles
disent qu'il a echoue, jamais pourquoi."""

_ADRESSE = re.compile(r" @ 0x[0-9a-fA-F]+\]")


def ligne_d_erreur(texte: str) -> str:
    """La ligne qui dit POURQUOI ffmpeg a echoue, sans son adresse memoire. Vide si rien.

    Pas la derniere : ffmpeg termine par une cascade de consequences (voir
    ``_CONSEQUENCES``). Un MP4 sous-titre echouait sur « Nothing was written
    into output file… », qui cachait la vraie raison cinq lignes plus haut :
    « Subtitle codec mov_text is not supported ». Si toutes les lignes sont des
    consequences, la derniere vaut mieux que rien.
    """
    lignes = [ligne.strip() for ligne in texte.splitlines() if ligne.strip()]
    if not lignes:
        return ""
    cause = next(
        (ligne for ligne in reversed(lignes) if not any(c in ligne for c in _CONSEQUENCES)),
        lignes[-1],
    )
    return _ADRESSE.sub("]", cause)


EXTRACTION_DELAI = 60.0
"""Extraire une couverture ne lit qu'un paquet : au-dela, quelque chose ne va
pas, et l'encodage part sans elle plutot que d'attendre."""


def _extraire_images(
    binaire: str, job: Job, images: Sequence[ImageJointe], sortie: Path
) -> list[tuple[ImageJointe, Path]]:
    """Extrait chaque couverture, a l'octet pres, pour la rejoindre au resultat.

    Une extraction ratee coute cette couverture, pas l'encodage, et une note
    le dit avant le remplacement.
    """
    extraites: list[tuple[ImageJointe, Path]] = []
    for numero, image in enumerate(images):
        chemin = sortie.with_name(f"{sortie.stem}.couverture-{numero}{image.extension}")
        try:
            resultat = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
                [
                    binaire,
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(job.source),
                    "-map",
                    f"0:v:{image.rang}",
                    "-c",
                    "copy",
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    "-f",
                    "image2",
                    str(chemin),
                ],
                capture_output=True,
                text=True,
                timeout=EXTRACTION_DELAI,
                check=False,
            )
            reussi = resultat.returncode == 0 and chemin.is_file()
            motif = "" if reussi else ligne_d_erreur(resultat.stderr)
        except (OSError, subprocess.SubprocessError) as exc:
            reussi, motif = False, type(exc).__name__
        if reussi:
            extraites.append((image, chemin))
            continue
        chemin.unlink(missing_ok=True)
        job.notes.append(
            f"Couverture « {image.nom} » non conservée : son extraction a échoué"
            + (f" ({motif})." if motif else ".")
        )
        logger.warning("couverture %s non extraite de %s : %s", image.nom, job.relative_path, motif)
    return extraites


def _suivre(job: Job, processus: subprocess.Popen) -> None:
    """Lit la progression de ffmpeg au fil de l'eau.

    ``-progress pipe:1`` ecrit des lignes ``cle=valeur``. On ne retient que le
    temps encode : c'est la seule mesure qui avance de facon reguliere.
    """
    if processus.stdout is None:
        return
    for ligne in processus.stdout:
        if not ligne.startswith("out_time_us=") or job.source_duration <= 0:
            continue
        try:
            secondes = int(ligne.split("=", 1)[1]) / 1_000_000
        except ValueError:
            continue
        job.progress = min(1.0, secondes / job.source_duration)


def _conclure(job: Job, sortie: Path) -> Job:
    job.output_bytes = sortie.stat().st_size if sortie.is_file() else 0
    job.output_duration = probe_media(sortie).duration_seconds or 0.0
    job.progress = 1.0
    job.finished_at = datetime.now(UTC).isoformat()
    job.state = State.DONE
    logger.info(
        "reencodage termine : %s — %s -> %s octets (%.0f %% du poids d'origine)",
        job.relative_path,
        job.source_bytes,
        job.output_bytes,
        job.ratio * 100,
    )
    return job


def _echec(job: Job, message: str) -> Job:
    job.state = State.FAILED
    job.error = message
    job.finished_at = datetime.now(UTC).isoformat()
    logger.warning("reencodage echoue : %s — %s", job.relative_path, message)
    return job


# --- La pause ---------------------------------------------------------------


@dataclass
class _Controle:
    """Ce que la pause doit savoir de l'encodage en cours, sous un seul verrou.

    La pause arrive par une requete HTTP, donc depuis un autre fil que celui
    qui lance ffmpeg. Sans verrou commun, une pause demandee pendant le
    lancement (sonde de duree, creation du dossier) trouverait encore aucun
    processus a geler, et l'encodage demarrerait en pleine pause.
    """

    verrou: threading.Lock = field(default_factory=threading.Lock)
    job: Job | None = None
    processus: subprocess.Popen | None = None
    en_pause: bool = False
    depuis: str = ""


_controle = _Controle()


def suspendre() -> Job | None:
    """Met la file en pause et gele l'encodage en cours, s'il y en a un.

    SIGSTOP et non un arret : le processeur est rendu sur-le-champ, et SIGCONT
    reprendra a l'image pres. Le processus garde sa memoire tant qu'il est
    gele — c'est le prix de la reprise exacte.

    Sans encodage en cours, la pause reste valable : elle empeche le suivant
    de partir (voir ``_prendre_en_charge``). Idempotent : une seconde pause ne
    renvoie pas de signal et ne remet pas le compteur a zero.

    Retourne le travail gele, None si rien ne tournait.
    """
    with _controle.verrou:
        if not _controle.en_pause:
            _controle.en_pause = True
            _controle.depuis = datetime.now(UTC).isoformat()
        return _geler()


def relancer() -> Job | None:
    """Leve la pause et degele l'encodage suspendu, qui repart ou il en etait.

    Retourne le travail degele, None s'il n'y en avait pas.
    """
    with _controle.verrou:
        _controle.en_pause = False
        _controle.depuis = ""
        return _degeler()


def degeler_pour_arret() -> None:
    """Degele ffmpeg SANS lever la pause, a l'extinction.

    Un processus gele ne traite aucun signal hormis SIGKILL : le SIGTERM ou le
    SIGINT de l'arret resterait en attente, et un ffmpeg orphelin et fige
    survivrait a Sortilege, memoire comprise. La pause, elle, reste voulue :
    c'est l'etat que le prochain demarrage doit retrouver.
    """
    with _controle.verrou:
        _degeler()


def restaurer_pause(depuis: str) -> None:
    """Remet la file en pause au demarrage, depuis l'etat enregistre.

    Aucun processus a geler a ce stade : la file ne survit pas au redemarrage.
    Seul compte que le prochain encodage ne parte pas.
    """
    with _controle.verrou:
        _controle.en_pause = True
        _controle.depuis = depuis or datetime.now(UTC).isoformat()


def en_pause() -> bool:
    with _controle.verrou:
        return _controle.en_pause


def pause_depuis() -> str:
    """Debut de la pause (ISO, UTC), vide hors pause."""
    with _controle.verrou:
        return _controle.depuis


def travail_suspendu() -> Job | None:
    with _controle.verrou:
        job = _controle.job
        return job if job is not None and job.suspended else None


def _prendre_en_charge(job: Job, processus: subprocess.Popen) -> None:
    """Enregistre le processus lance, et le gele aussitot si la pause est la.

    C'est ce qui ferme la course entre « la boucle a verifie qu'il n'y avait
    pas de pause » et « ffmpeg a demarre » : une pause arrivee entre les deux
    trouve ici le processus, sous le meme verrou qu'elle.
    """
    with _controle.verrou:
        _controle.job = job
        _controle.processus = processus
        if _controle.en_pause:
            _geler()


def _lacher(job: Job) -> None:
    """Oublie le processus termine, et clot le compte de pause s'il le faut."""
    with _controle.verrou:
        if job.suspended:
            _clore_pause(job)
        if _controle.job is job:
            _controle.job = None
            _controle.processus = None


def _geler() -> Job | None:
    """SIGSTOP au processus en cours. Verrou tenu."""
    job, processus = _controle.job, _controle.processus
    if job is None or processus is None:
        return None
    if job.suspended:
        return job
    if not _signaler(processus, signal.SIGSTOP):
        return None
    job.suspended_at = horloge()
    logger.info("reencodage suspendu : %s (%.0f %%)", job.relative_path, job.progress * 100)
    return job


def _degeler() -> Job | None:
    """SIGCONT au processus suspendu. Verrou tenu."""
    job, processus = _controle.job, _controle.processus
    if job is None or processus is None or not job.suspended:
        return None
    # Le compte est clos meme si le signal echoue : un processus disparu
    # entre-temps n'est plus en pause, et le laisser compter ferait deriver la
    # duree affichee.
    _signaler(processus, signal.SIGCONT)
    _clore_pause(job)
    logger.info("reencodage repris : %s", job.relative_path)
    return job


def _clore_pause(job: Job) -> None:
    job.paused_seconds = job.seconds_paused()
    job.suspended_at = None


def _signaler(processus: subprocess.Popen, signal_: int) -> bool:
    """Envoie un signal ; False si le processus n'est plus la pour le recevoir.

    Ne leve jamais : un ffmpeg qui se termine a l'instant ou l'on appuie sur
    pause est un cas normal, pas une erreur a remonter a l'interface.
    """
    try:
        processus.send_signal(signal_)
    except ProcessLookupError:
        return False
    except OSError as exc:
        logger.warning("signal %s refuse par ffmpeg (pid %s) : %s", signal_, processus.pid, exc)
        return False
    # ``send_signal`` interroge le processus avant d'envoyer : un encodage
    # termine entre-temps a desormais un code de retour, et n'a rien recu.
    return processus.returncode is None


FICHIER_PAUSE = "reencodage-pause.json"
"""Nom du fichier d'etat, dans le dossier de donnees. La pause doit survivre a
un redemarrage du conteneur : relancee au milieu d'un film, elle ferait
exactement ce qu'on lui demandait d'empecher."""


@dataclass
class PauseEnregistree:
    depuis: str
    """Debut de la pause (ISO, UTC)."""

    travail: str = ""
    """Titre de ce qui encodait au moment de la pause. Il sera PERDU si le
    conteneur redemarre (la file ne survit pas), et il faut pouvoir le dire au
    retour plutot que de le laisser disparaitre en silence."""


def lire_pause(chemin: Path) -> PauseEnregistree | None:
    """L'etat enregistre, None s'il n'y a pas de pause.

    Un fichier illisible vaut « pas de pause » : il ne peut provenir que d'une
    main exterieure (l'ecriture est atomique), et bloquer la file pour
    toujours sur un fichier qu'on ne comprend pas serait pire.
    """
    try:
        brut = json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("etat de pause illisible (%s), ignore : %s", chemin, exc)
        return None
    if not isinstance(brut, dict) or not brut.get("paused"):
        return None
    return PauseEnregistree(
        depuis=str(brut.get("paused_since") or ""),
        travail=str(brut.get("job_title") or ""),
    )


def ecrire_pause(chemin: Path, pause: PauseEnregistree | None) -> None:
    """Enregistre la pause, ou l'efface avec None. Leve OSError.

    Temporaire puis ``os.replace`` : une coupure au milieu laisse l'ancien etat
    intact, jamais un JSON tronque que la relecture prendrait pour « pas de
    pause » — et le NAS repartirait a pleine charge au redemarrage.
    """
    if pause is None:
        chemin.unlink(missing_ok=True)
        return
    _ecrire_json(chemin, {"paused": True, "paused_since": pause.depuis, "job_title": pause.travail})


def _ecrire_json(chemin: Path, donnees: object) -> None:
    """Ecriture atomique d'un petit etat JSON (voir ``ecrire_pause``). Leve OSError."""
    chemin.parent.mkdir(parents=True, exist_ok=True)
    contenu = json.dumps(donnees, indent=2, ensure_ascii=False)
    provisoire = chemin.with_name(f".{chemin.name}.{os.getpid()}.tmp")
    try:
        with provisoire.open("w", encoding="utf-8") as sortie:
            sortie.write(contenu)
            sortie.flush()
            os.fsync(sortie.fileno())
        os.replace(provisoire, chemin)
    except OSError:
        with contextlib.suppress(OSError):
            provisoire.unlink(missing_ok=True)
        raise


# --- Les refus definitifs ---------------------------------------------------


FICHIER_REFUS = "reencodage-refus.json"
"""Fichiers refuses pour de bon, dans le dossier de donnees. Sans cette memoire,
un Dolby Vision profil 5 reviendrait parmi les candidats a chaque ouverture de
la page, et « tout mettre en file » le relancerait chaque soir — pour le meme
refus chaque nuit, noye au milieu des vrais resultats."""


@dataclass(frozen=True, slots=True)
class RefusEnregistre:
    motif: str
    taille: int
    modifie_ns: int
    """Taille et date du fichier refuse. Le refus ne vaut que pour CE fichier :
    remplace par une autre version (un vrai HDR10, un 1080p), il redevient un
    candidat comme les autres."""

    date: str = ""

    def vaut_pour(self, source: Path) -> bool:
        try:
            info = source.stat()
        except OSError:
            return False
        return info.st_size == self.taille and info.st_mtime_ns == self.modifie_ns


def refus_de(job: Job) -> RefusEnregistre | None:
    """Le refus a retenir pour ce travail, None s'il n'est pas definitif."""
    if job.state is not State.FAILED or not job.definitif:
        return None
    try:
        info = job.source.stat()
    except OSError:
        return None
    return RefusEnregistre(
        motif=job.error,
        taille=info.st_size,
        modifie_ns=info.st_mtime_ns,
        date=datetime.now(UTC).isoformat(),
    )


def lire_refus(chemin: Path) -> dict[str, RefusEnregistre]:
    """Refus enregistres, par chemin relatif. Illisible = aucun.

    Un fichier corrompu ne doit rien bloquer : au pire, un fichier refuse
    redevient candidat, et sera refuse de nouveau avant d'etre encode.
    """
    try:
        brut = json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("refus de reencodage illisibles (%s), ignores : %s", chemin, exc)
        return {}
    if not isinstance(brut, dict):
        return {}
    refus: dict[str, RefusEnregistre] = {}
    for chemin_relatif, valeur in brut.items():
        if not isinstance(valeur, dict):
            continue
        taille, modifie = valeur.get("size"), valeur.get("mtime_ns")
        if not isinstance(taille, int) or not isinstance(modifie, int):
            continue
        refus[str(chemin_relatif)] = RefusEnregistre(
            motif=str(valeur.get("reason") or ""),
            taille=taille,
            modifie_ns=modifie,
            date=str(valeur.get("date") or ""),
        )
    return refus


def ecrire_refus(chemin: Path, refus: dict[str, RefusEnregistre]) -> None:
    """Enregistre les refus (ecriture atomique). Leve OSError."""
    _ecrire_json(
        chemin,
        {
            chemin_relatif: {
                "reason": r.motif,
                "size": r.taille,
                "mtime_ns": r.modifie_ns,
                "date": r.date,
            }
            for chemin_relatif, r in sorted(refus.items())
        },
    )


# --- La verification, puis le remplacement ----------------------------------


def verify(job: Job) -> tuple[bool, str]:
    """Le resultat est-il remplacable ? Refuser vaut mieux que se tromper.

    La verification de DUREE est la plus importante, et la moins evidente : un
    encodage interrompu produit un fichier plus court ET plus petit. Sans elle,
    il ressemblerait a une reussite particulierement efficace — et on
    remplacerait un film entier par ses vingt premieres minutes.
    """
    if job.state is not State.DONE:
        return False, "ce reencodage n'est pas termine"
    if job.output is None or not job.output.is_file():
        return False, "le fichier reencode a disparu"
    if not job.source.is_file():
        return False, "le fichier d'origine a disparu — rien a remplacer"
    if job.output_bytes <= 0:
        return False, "le fichier reencode est vide"

    if job.source_duration > 0 and job.output_duration > 0:
        ecart = abs(job.output_duration - job.source_duration) / job.source_duration
        if ecart > DUREE_TOLERANCE:
            return False, (
                f"durees incoherentes : {job.source_duration:.0f} s a l'origine, "
                f"{job.output_duration:.0f} s apres — encodage probablement interrompu"
            )

    if job.budget_bytes and job.output_bytes > job.budget_bytes * 1.15:
        # Le debit vise n'a pas suffi : cela arrive sur une source tres
        # chargee. On ne bloque pas — le fichier est peut-etre tres bien — mais
        # on ne laisse pas croire que le budget a ete tenu.
        logger.info(
            "budget depasse pour %s : %s octets vises, %s obtenus",
            job.relative_path,
            job.budget_bytes,
            job.output_bytes,
        )

    if job.ratio > 1 - GAIN_MINIMAL:
        return False, (
            f"le gain est negligeable ({100 - job.ratio * 100:.0f} %) — "
            "remplacer perdrait de la qualite sans rendre de place"
        )
    return True, ""


def replace(job: Job, journal: Journal, trash_root: Path | None) -> tuple[bool, str]:
    """Met le fichier reencode a la place de l'original, qui part en corbeille.

    Meme ordre que partout ailleurs dans l'application : l'original part
    D'ABORD, ce qui libere la place, et il est REMIS si la suite echoue. Laisser
    la bibliotheque sans le fichier serait pire que de n'avoir rien tente.

    Jamais de suppression : un reencodage peut etre visuellement decevant sans
    que le controle automatique l'ait vu, et cela ne se decouvre parfois qu'en
    regardant le film.
    """
    ok, motif = verify(job)
    if not ok:
        return False, motif
    if trash_root is None:
        return False, "aucune corbeille configuree"

    if job.output is None:  # deja garanti par verify, garde pour le typage
        return False, "aucun fichier reencode"
    lot = datetime.now(UTC).strftime("%Y-%m-%d")
    ecarte = trash_root / lot / job.source.name
    # Le lot du jour comme ailleurs : cree a l'identite de la corbeille, faute
    # de quoi l'utilisateur ne pourrait plus la vider (voir core/proprietaire).
    cree_dossier_d_accueil(ecarte.parent)
    if ecarte.exists():
        ecarte = ecarte.with_name(f"{job.id}-{job.source.name}")

    try:
        methode = _move(job.source, ecarte)
    except OSError as exc:
        return False, f"impossible d'ecarter l'original : {exc}"
    _inscrire(journal, job, job.source, ecarte, methode, "trash")

    # Le remplacant prend le nom de l'original, avec l'extension du conteneur
    # reellement produit : un AVI reencode devient un MKV, et pretendre le
    # contraire donnerait un fichier que certains lecteurs refuseraient.
    destination = job.source.with_suffix(job.output.suffix)
    try:
        methode = _move(job.output, destination)
    except OSError as exc:
        try:
            _move(ecarte, job.source)
        except OSError:
            logger.exception("restauration impossible apres echec : %s", job.source)
        return False, f"impossible d'installer le fichier reencode : {exc}"

    _inscrire(journal, job, job.output, destination, methode, "video")
    job.state = State.REPLACED
    logger.info("fichier remplace : %s (%s octets rendus)", destination, job.savings_bytes)
    return True, ""


def discard(job: Job) -> None:
    """Jette le resultat sans rien remplacer. L'original n'a jamais bouge."""
    if job.output is not None:
        job.output.unlink(missing_ok=True)
    job.state = State.DISCARDED


def _inscrire(
    journal: Journal, job: Job, source: Path, destination: Path, methode: str, genre: str
) -> None:
    journal.append(
        MoveRecord(
            timestamp=datetime.now(UTC).isoformat(),
            plan_id=f"reencodage:{job.id}",
            source=str(source),
            destination=str(destination),
            method=methode,
            kind=genre,
            title=job.title,
        )
    )


def purge_staging(library_root: Path, jobs: list[Job]) -> int:
    """Efface les fichiers en attente qui ne correspondent plus a aucun travail.

    Un redemarrage pendant un encodage laisse un fichier partiel que plus rien
    ne reclame. Sans ce menage, il occuperait indefiniment la place qu'on
    cherchait a liberer.
    """
    dossier = staging_root(library_root)
    if not dossier.is_dir():
        return 0
    connus = {job.output for job in jobs if job.output is not None}
    efface = 0
    for fichier in dossier.iterdir():
        if fichier.is_file() and fichier not in connus:
            fichier.unlink(missing_ok=True)
            efface += 1
    if efface:
        logger.info("menage du dossier de reencodage : %s fichier(s) orphelin(s)", efface)
    return efface


def next_queued(jobs: list[Job]) -> Job | None:
    """Le prochain a encoder. Un seul a la fois, dans l'ordre d'arrivee."""
    if any(job.state is State.RUNNING for job in jobs):
        return None
    return next((job for job in jobs if job.state is State.QUEUED), None)
