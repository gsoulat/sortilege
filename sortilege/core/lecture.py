"""Ce qu'est un fichier video, et comment le montrer a un navigateur sans mentir.

Le cas qui a fait naitre ce module : « Kandahar », 21,5 Go reencodes en 5,6 Go.
Au moment de decider s'il remplace l'original, l'utilisateur clique
« Verifier », et Firefox repond « le fichier est corrompu ». Le fichier ne
l'etait pas : c'etait un H.264 **10 bits**, qu'aucun navigateur ne decode, et
que l'ancien lecteur recopiait tel quel parce que le codec s'appelait « h264 ».
Pire, le flux etait servi sans prise en charge des plages d'octets, ce que
Safari exige d'un serveur video et que les autres supportent mal.

Sur cet ecran precis, les deux erreurs coutent cher : un faux « corrompu » fait
jeter un bon fichier, un vrai defaut non vu fait remplacer un bon original par
un mauvais. D'ou les trois parties de ce module :

1. **Savoir ce qu'est le fichier** (``sonder``) : codec, profil, profondeur,
   HDR et ses metadonnees, pistes. C'est aussi la description qu'utilise le
   reencodage pour decider comment traiter une source HDR — une seule lecture
   d'un meme fichier, sinon deux descriptions finissent par diverger.
2. **Choisir comment le montrer** (``decider``) : lecture directe, reemballage
   sans toucher a l'image, ou conversion POUR L'APERCU SEULEMENT, servie en HLS
   par des sessions bornees (``Sessions``) — un NAS ne doit jamais convertir
   deux films en meme temps pour quelqu'un qui a ferme l'onglet.
3. **Distinguer « le navigateur ne sait pas » de « le fichier est abime »**
   (``controler``) : on decode reellement le fichier cote serveur, a cinq
   endroits. Le navigateur n'est pas un instrument de mesure.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import shutil
import signal
import struct
import subprocess
import threading
import time
import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FFPROBE_DELAI = 20.0
"""Un fichier pathologique ne doit pas retenir un worker indefiniment."""


# --- Libelles -----------------------------------------------------------------

_CODECS_VIDEO = {
    "h264": "H.264",
    "hevc": "HEVC",
    "av1": "AV1",
    "vp9": "VP9",
    "vp8": "VP8",
    "mpeg2video": "MPEG-2",
    "mpeg1video": "MPEG-1",
    "mpeg4": "MPEG-4 Part 2",
    "vc1": "VC-1",
    "wmv3": "WMV 9",
    "msmpeg4v3": "DivX 3",
    "prores": "ProRes",
}

_CODECS_AUDIO = {
    "aac": "AAC",
    "ac3": "AC-3",
    "eac3": "E-AC-3",
    "dts": "DTS",
    "truehd": "TrueHD",
    "mp3": "MP3",
    "mp2": "MP2",
    "flac": "FLAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "alac": "ALAC",
}

_CODECS_SOUS_TITRES = {
    "subrip": "SRT",
    "ass": "ASS",
    "ssa": "SSA",
    "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "VobSub",
    "dvb_subtitle": "DVB",
    "mov_text": "mov_text",
    "webvtt": "WebVTT",
}

_CONTENEURS = {
    ".mkv": "MKV",
    ".mp4": "MP4",
    ".m4v": "MP4",
    ".mov": "MOV",
    ".avi": "AVI",
    ".ts": "MPEG-TS",
    ".m2ts": "MPEG-TS",
    ".mts": "MPEG-TS",
    ".webm": "WebM",
    ".wmv": "WMV",
    ".mpg": "MPEG-PS",
    ".mpeg": "MPEG-PS",
    ".vob": "VOB",
}


def duree_lisible(secondes: float | None) -> str:
    """« 2 h 05 min », « 42 min 10 s », « 8 s ». Une duree se lit, elle ne se calcule pas."""
    if secondes is None:
        return "inconnue"
    total = round(abs(secondes))
    heures, reste = divmod(total, 3600)
    minutes, sec = divmod(reste, 60)
    if heures:
        return f"{heures} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {sec:02d} s"
    return f"{sec} s"


# --- Ce que le fichier dit de lui-meme ---------------------------------------


@dataclass(frozen=True, slots=True)
class PisteAudio:
    codec: str
    canaux: int | None = None
    disposition: str = ""
    """Agencement des canaux tel que ffprobe le nomme (« 5.1(side) »)."""

    langue: str = ""
    profil: str = ""
    defaut: bool = False
    titre: str = ""

    @property
    def format(self) -> str:
        """« E-AC-3 5.1 ». Le profil DTS compte : un DTS-HD MA n'est pas un DTS."""
        codec = _CODECS_AUDIO.get(self.codec, self.codec.upper() or "?")
        if self.codec == "dts" and self.profil and self.profil != "DTS":
            codec = self.profil
        return " ".join(m for m in (codec, _canaux_lisibles(self.canaux, self.disposition)) if m)

    @property
    def libelle(self) -> str:
        """« fre E-AC-3 5.1 » : la langue d'abord, c'est elle qu'on cherche des yeux."""
        return f"{self.langue or 'und'} {self.format}"


@dataclass(frozen=True, slots=True)
class PisteSousTitre:
    codec: str
    langue: str = ""
    forcee: bool = False
    titre: str = ""

    @property
    def libelle(self) -> str:
        codec = _CODECS_SOUS_TITRES.get(self.codec, self.codec or "?")
        texte = f"{self.langue or 'und'} {codec}"
        return f"{texte} (forcés)" if self.forcee else texte


def _canaux_lisibles(canaux: int | None, disposition: str) -> str:
    if disposition:
        # « 5.1(side) » : la precision entre parentheses n'aide personne a
        # comparer deux fichiers.
        base = disposition.split("(", 1)[0]
        return {"mono": "mono", "stereo": "stéréo"}.get(base, base)
    if canaux is None:
        return ""
    return {1: "mono", 2: "stéréo", 6: "5.1", 8: "7.1"}.get(canaux, f"{canaux} canaux")


@dataclass(frozen=True, slots=True)
class Mastering:
    """Mastering display metadata (SMPTE ST 2086), telles que lues dans le fichier.

    Chromaticites en coordonnees CIE 1931 (0,68 pour le rouge BT.2020), luminances
    en cd/m². ``pour_x265`` les rend dans les unites de ``x265 --master-display``
    — c'est ce que le reencodage HDR recopie pour que le televiseur sache doser
    la luminosite. Une valeur absente reste ``None`` : inventer une luminance de
    mastering serait pire que de ne pas en donner.
    """

    rouge: tuple[float, float] | None = None
    vert: tuple[float, float] | None = None
    bleu: tuple[float, float] | None = None
    point_blanc: tuple[float, float] | None = None
    luminance_min: float | None = None
    luminance_max: float | None = None

    def pour_x265(self) -> str | None:
        """« G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50) ».

        Unites x265 : 0,00002 pour les chromaticites, 0,0001 cd/m² pour les
        luminances. None des qu'une valeur manque : une chaine a moitie
        remplie serait refusee par x265, ou pire, acceptee avec des zeros.
        """
        if (
            self.vert is None
            or self.bleu is None
            or self.rouge is None
            or self.point_blanc is None
            or self.luminance_min is None
            or self.luminance_max is None
        ):
            return None

        def xy(point: tuple[float, float]) -> str:
            return f"{round(point[0] * 50000)},{round(point[1] * 50000)}"

        lmax = round(self.luminance_max * 10000)
        lmin = round(self.luminance_min * 10000)
        return (
            f"G({xy(self.vert)})B({xy(self.bleu)})R({xy(self.rouge)})"
            f"WP({xy(self.point_blanc)})L({lmax},{lmin})"
        )

    def en_dict(self) -> dict[str, Any]:
        return {
            "rouge": self.rouge,
            "vert": self.vert,
            "bleu": self.bleu,
            "point_blanc": self.point_blanc,
            "luminance_min": self.luminance_min,
            "luminance_max": self.luminance_max,
            "x265": self.pour_x265(),
        }


@dataclass(frozen=True, slots=True)
class LumiereContenu:
    """Content light level metadata : MaxCLL et MaxFALL, en cd/m².

    Zero est une valeur LEGITIME et distincte de l'absence : la norme HDR10 dit
    « 0 = inconnu, declare comme tel ». On la garde telle quelle.
    """

    max_cll: int | None = None
    max_fall: int | None = None

    def pour_x265(self) -> str | None:
        """« 1000,400 », pour ``x265 --max-cll``."""
        if self.max_cll is None or self.max_fall is None:
            return None
        return f"{self.max_cll},{self.max_fall}"

    def en_dict(self) -> dict[str, Any]:
        return {"max_cll": self.max_cll, "max_fall": self.max_fall, "x265": self.pour_x265()}


# Signification de dv_bl_signal_compatibility_id : ce que la couche de base
# donne a un appareil qui ignore le Dolby Vision.
_BASE_DV = {0: None, 1: "hdr10", 2: "sdr", 4: "hlg", 6: "hdr10"}

# A defaut d'identifiant de compatibilite, ce que le profil implique.
_BASE_DV_PAR_PROFIL = {4: "sdr", 5: None, 7: "hdr10", 9: "sdr"}


@dataclass(frozen=True, slots=True)
class DolbyVision:
    """Ce que dit le « DOVI configuration record ».

    Le PROFIL est vital, pas decoratif. Un profil 5 n'a AUCUNE couche de base
    compatible : son signal est en IPTPQc2, et le decoder comme du HDR10 donne
    une image violette et verte. Un profil 8.1 porte une base HDR10 exploitable,
    un profil 7 (double couche des Blu-ray UHD) aussi, par sa couche de base.
    Reencoder un profil 5 « comme du HDR10 » detruit le film sans erreur visible
    dans les journaux.
    """

    profil: int | None = None
    niveau: int | None = None
    compatibilite: int | None = None
    """dv_bl_signal_compatibility_id : 1 = HDR10, 2 = SDR, 4 = HLG, 6 = Blu-ray
    UHD (HDR10), 0 = aucune."""

    couche_base: bool | None = None
    couche_amelioration: bool | None = None
    rpu: bool | None = None

    @property
    def base(self) -> str | None:
        """Ce que voit un appareil sans Dolby Vision : hdr10, sdr, hlg, ou None."""
        if self.profil == 5:
            return None
        if self.compatibilite is not None and self.compatibilite in _BASE_DV:
            return _BASE_DV[self.compatibilite]
        return _BASE_DV_PAR_PROFIL.get(self.profil) if self.profil is not None else None

    @property
    def base_exploitable(self) -> bool:
        return self.base is not None

    @property
    def libelle(self) -> str:
        """« Dolby Vision profil 8.1 » : le sous-profil n'a de sens que pour 8 et 10."""
        if self.profil is None:
            return "Dolby Vision (profil inconnu)"
        if self.profil in (8, 10) and self.compatibilite is not None:
            return f"Dolby Vision profil {self.profil}.{self.compatibilite}"
        return f"Dolby Vision profil {self.profil}"

    @property
    def explication(self) -> str:
        if self.profil == 5:
            return (
                "aucune couche de base compatible : lu sans Dolby Vision, "
                "l'image vire au violet et au vert"
            )
        base = self.base
        if base == "hdr10":
            return "couche de base HDR10 exploitable"
        if base == "hlg":
            return "couche de base HLG exploitable"
        if base == "sdr":
            return "couche de base SDR"
        return "compatibilité de la couche de base inconnue"

    def en_dict(self) -> dict[str, Any]:
        return {
            "profil": self.profil,
            "niveau": self.niveau,
            "compatibilite": self.compatibilite,
            "couche_base": self.couche_base,
            "couche_amelioration": self.couche_amelioration,
            "rpu": self.rpu,
            "base": self.base,
            "base_exploitable": self.base_exploitable,
            "libelle": self.libelle,
            "explication": self.explication,
        }


class TypeHdr(StrEnum):
    SDR = "sdr"
    HDR10 = "hdr10"
    HDR10PLUS = "hdr10plus"
    HLG = "hlg"
    DOLBY_VISION = "dolby_vision"


_LIBELLES_HDR = {
    TypeHdr.SDR: "SDR",
    TypeHdr.HDR10: "HDR10",
    TypeHdr.HDR10PLUS: "HDR10+",
    TypeHdr.HLG: "HLG",
}


@dataclass(slots=True)
class Analyse:
    """Tout ce que ffprobe dit d'un fichier, dans une forme qu'on peut comparer.

    ``lue`` distingue « ffprobe n'a rien pu lire » de « le fichier n'a pas de
    video » : le premier se dit « illisible », le second « pas de video », et
    les deux ne menent pas au meme conseil.
    """

    lue: bool = False
    motif_illisible: str = ""
    nom: str = ""
    extension: str = ""
    conteneur: str = ""
    duree: float | None = None

    video_codec: str = ""
    video_profil: str = ""
    video_tag: str = ""
    pix_fmt: str = ""
    profondeur: int | None = None
    largeur: int | None = None
    hauteur: int | None = None
    images_par_seconde: float | None = None

    transfert: str = ""
    """color_transfer : smpte2084 = PQ (HDR10), arib-std-b67 = HLG."""

    primaires: str = ""
    matrice: str = ""
    mastering: Mastering | None = None
    lumiere: LumiereContenu | None = None
    dolby_vision: DolbyVision | None = None
    hdr10plus: bool = False

    premiere_image_lue: bool = False
    """Vrai SEULEMENT si ffprobe a lu la premiere image (seconde passe de
    ``sonder``). Faux — echec ou delai depasse —, l'analyse est incomplete sans
    le dire : dans un MKV ecrit par ffmpeg, la courbe PQ ou HLG et les
    metadonnees HDR10 ne vivent souvent QUE dans cette image, le conteneur ne
    portant parfois que la matrice ``bt2020nc``. Un HDR y passe alors pour du
    SDR. Le reencodage s'en sert pour refuser d'encoder sur une description
    qu'il sait incomplete ; ce module se contente de le dire."""

    audio: list[PisteAudio] = field(default_factory=list)
    sous_titres: list[PisteSousTitre] = field(default_factory=list)

    @property
    def a_video(self) -> bool:
        return bool(self.video_codec)

    @property
    def hdr(self) -> TypeHdr:
        """Le type de HDR, resume en un mot.

        Le Dolby Vision l'emporte : c'est lui qui decide de ce qu'on peut faire
        du fichier (voir ``DolbyVision``). Le HDR10+ ensuite, qui est un HDR10
        avec des metadonnees par scene. Une luminance de mastering sans fonction
        de transfert declaree vaut HDR10 : ces metadonnees n'existent pas en SDR.
        """
        if self.dolby_vision is not None:
            return TypeHdr.DOLBY_VISION
        if self.hdr10plus:
            return TypeHdr.HDR10PLUS
        if self.transfert == "smpte2084":
            return TypeHdr.HDR10
        if self.transfert == "arib-std-b67":
            return TypeHdr.HLG
        if self.mastering is not None and not self.transfert:
            return TypeHdr.HDR10
        return TypeHdr.SDR

    @property
    def est_hdr(self) -> bool:
        return self.hdr is not TypeHdr.SDR

    @property
    def libelle_hdr(self) -> str:
        if self.dolby_vision is not None:
            return self.dolby_vision.libelle
        return _LIBELLES_HDR[self.hdr]

    @property
    def libelle_codec(self) -> str:
        return _CODECS_VIDEO.get(self.video_codec, self.video_codec.upper() or "inconnu")

    @property
    def libelle_video(self) -> str:
        """« H.264 10 bits », « HEVC » : ce qui decide de la lecture, en trois mots."""
        if self.profondeur and self.profondeur > 8:
            return f"{self.libelle_codec} {self.profondeur} bits"
        return self.libelle_codec

    @property
    def libelle_profil(self) -> str:
        """« H.264 High 10 », « HEVC Main 10 »."""
        return f"{self.libelle_codec} {self.video_profil}".strip()

    @property
    def definition(self) -> str:
        """Palier de definition, d'apres la hauteur ET la largeur.

        Un film en 2,39:1 encode en 1920x800 est un 1080p : la hauteur seule le
        classerait en 720p, et la fiche comparee annoncerait une perte qui n'a
        pas eu lieu.
        """
        if not self.hauteur:
            return "inconnue"
        largeur = self.largeur or 0
        if self.hauteur >= 2000 or largeur >= 3800:
            return "2160p"
        if self.hauteur >= 1000 or largeur >= 1900:
            return "1080p"
        if self.hauteur >= 700 or largeur >= 1260:
            return "720p"
        return f"{self.hauteur}p"

    @property
    def conteneur_lisible(self) -> str:
        return _CONTENEURS.get(self.extension, self.extension.lstrip(".").upper() or "inconnu")

    @property
    def piste_audio_principale(self) -> int | None:
        """Indice (parmi les pistes audio) de celle qu'un lecteur jouerait."""
        if not self.audio:
            return None
        return next((i for i, p in enumerate(self.audio) if p.defaut), 0)

    def en_dict(self) -> dict[str, Any]:
        return {
            "lue": self.lue,
            "motif_illisible": self.motif_illisible,
            "nom": self.nom,
            "conteneur": self.conteneur_lisible,
            "duree": self.duree,
            "duree_lisible": duree_lisible(self.duree),
            "video": {
                "codec": self.video_codec,
                "profil": self.video_profil,
                "libelle": self.libelle_video,
                "libelle_profil": self.libelle_profil,
                "pix_fmt": self.pix_fmt,
                "profondeur": self.profondeur,
                "largeur": self.largeur,
                "hauteur": self.hauteur,
                "definition": self.definition,
                "images_par_seconde": self.images_par_seconde,
            },
            "hdr": {
                "type": str(self.hdr),
                "libelle": self.libelle_hdr,
                "transfert": self.transfert,
                "primaires": self.primaires,
                "matrice": self.matrice,
                "mastering": self.mastering.en_dict() if self.mastering else None,
                "lumiere": self.lumiere.en_dict() if self.lumiere else None,
                "dolby_vision": self.dolby_vision.en_dict() if self.dolby_vision else None,
                "hdr10plus": self.hdr10plus,
            },
            "audio": [
                {
                    "codec": p.codec,
                    "canaux": p.canaux,
                    "langue": p.langue,
                    "defaut": p.defaut,
                    "libelle": p.libelle,
                }
                for p in self.audio
            ],
            "sous_titres": [
                {"codec": p.codec, "langue": p.langue, "forcee": p.forcee, "libelle": p.libelle}
                for p in self.sous_titres
            ],
        }


# --- Lecture de la sortie de ffprobe ------------------------------------------


def _rationnel(valeur: object) -> float | None:
    """« 34000/50000 » -> 0.68. ffprobe ecrit ses metadonnees HDR en fractions."""
    if valeur is None:
        return None
    texte = str(valeur).strip()
    try:
        if "/" in texte:
            num, den = texte.split("/", 1)
            den_f = float(den)
            return float(num) / den_f if den_f else None
        return float(texte)
    except ValueError:
        return None


def _entier(valeur: object) -> int | None:
    if valeur is None:
        return None
    try:
        return int(str(valeur).strip())
    except ValueError:
        return None


def _booleen(valeur: object) -> bool | None:
    entier = _entier(valeur)
    return None if entier is None else bool(entier)


def _profondeur(pix_fmt: str, profil: str, bits_bruts: object) -> int | None:
    """8, 10 ou 12 bits, d'apres le format de pixel d'abord.

    « yuv420p10le » dit 10 ; « p010le » aussi ; « yuv420p » dit 8. Le profil
    (« High 10 », « Main 10 ») sert quand le format manque — c'est lui qui
    trahit un H.264 10 bits meme quand ffprobe n'a pas decode d'image.
    """
    if pix_fmt:
        if m := re.search(r"p(\d{2})(?:le|be)$", pix_fmt):
            return int(m.group(1))
        if m := re.match(r"p0(\d{2})", pix_fmt):
            return int(m.group(1))
        return 8
    if (bits := _entier(bits_bruts)) and bits > 0:
        return bits
    if m := re.search(r"\b(10|12)\b", profil):
        return int(m.group(1))
    return None


def _point(donnees: dict[str, Any], x: str, y: str) -> tuple[float, float] | None:
    vx, vy = _rationnel(donnees.get(x)), _rationnel(donnees.get(y))
    return None if vx is None or vy is None else (vx, vy)


def _mastering(donnees: dict[str, Any]) -> Mastering:
    return Mastering(
        rouge=_point(donnees, "red_x", "red_y"),
        vert=_point(donnees, "green_x", "green_y"),
        bleu=_point(donnees, "blue_x", "blue_y"),
        point_blanc=_point(donnees, "white_point_x", "white_point_y"),
        luminance_min=_rationnel(donnees.get("min_luminance")),
        luminance_max=_rationnel(donnees.get("max_luminance")),
    )


def _dolby_vision(donnees: dict[str, Any]) -> DolbyVision:
    return DolbyVision(
        profil=_entier(donnees.get("dv_profile")),
        niveau=_entier(donnees.get("dv_level")),
        compatibilite=_entier(donnees.get("dv_bl_signal_compatibility_id")),
        couche_base=_booleen(donnees.get("bl_present_flag")),
        couche_amelioration=_booleen(donnees.get("el_present_flag")),
        rpu=_booleen(donnees.get("rpu_present_flag")),
    )


def _type_donnee(donnees: dict[str, Any]) -> str:
    return str(donnees.get("side_data_type") or "")


def lire_sonde(
    flux: dict[str, Any], image: dict[str, Any] | None = None, *, nom: str = ""
) -> Analyse:
    """Transforme les sorties JSON de ffprobe en ``Analyse``. Pure : testable sans ffprobe.

    ``flux`` est la sortie de ``-show_streams -show_format`` ; ``image`` celle de
    ``-show_frames`` sur la PREMIERE image. Les deux sont necessaires : dans un
    MKV, la fonction de transfert et les metadonnees HDR10 statiques ne sont
    souvent QUE dans le flux video (messages SEI de la premiere image), pas dans
    l'en-tete du conteneur. Lire l'en-tete seul classerait en SDR un film HDR10
    bien forme — c'est ce qui arrive avec un MKV ecrit par ffmpeg.

    Les donnees de l'image l'emportent sur celles du conteneur : c'est ce que
    le decodeur verra vraiment.
    """
    analyse = Analyse(lue=True, nom=nom, extension=Path(nom).suffix.lower())
    # ``image`` vaut None quand la seconde passe a echoue ou depasse son delai.
    analyse.premiere_image_lue = bool(((image or {}).get("frames") or [None])[0])
    format_ = flux.get("format") or {}
    analyse.conteneur = str(format_.get("format_name") or "")
    analyse.duree = _rationnel(format_.get("duration"))

    video: dict[str, Any] | None = None
    for stream in flux.get("streams") or []:
        genre = stream.get("codec_type")
        disposition = stream.get("disposition") or {}
        tags = {str(k).lower(): v for k, v in (stream.get("tags") or {}).items()}
        if genre == "video" and video is None and not disposition.get("attached_pic"):
            video = stream
        elif genre == "audio":
            analyse.audio.append(
                PisteAudio(
                    codec=str(stream.get("codec_name") or ""),
                    canaux=_entier(stream.get("channels")),
                    disposition=str(stream.get("channel_layout") or ""),
                    langue=str(tags.get("language") or ""),
                    profil=str(stream.get("profile") or ""),
                    defaut=bool(disposition.get("default")),
                    titre=str(tags.get("title") or ""),
                )
            )
        elif genre == "subtitle":
            analyse.sous_titres.append(
                PisteSousTitre(
                    codec=str(stream.get("codec_name") or ""),
                    langue=str(tags.get("language") or ""),
                    forcee=bool(disposition.get("forced")),
                    titre=str(tags.get("title") or ""),
                )
            )

    if video is None:
        return analyse

    premiere = ((image or {}).get("frames") or [{}])[0] or {}
    analyse.video_codec = str(video.get("codec_name") or "")
    analyse.video_profil = str(video.get("profile") or "")
    analyse.video_tag = str(video.get("codec_tag_string") or "")
    analyse.pix_fmt = str(premiere.get("pix_fmt") or video.get("pix_fmt") or "")
    analyse.profondeur = _profondeur(
        analyse.pix_fmt, analyse.video_profil, video.get("bits_per_raw_sample")
    )
    analyse.largeur = _entier(video.get("width"))
    analyse.hauteur = _entier(video.get("height"))
    ips = _rationnel(video.get("avg_frame_rate")) or _rationnel(video.get("r_frame_rate"))
    analyse.images_par_seconde = ips if ips and ips > 0 else None
    if analyse.duree is None:
        analyse.duree = _rationnel(video.get("duration"))

    def _couleur(cle: str) -> str:
        valeur = str(premiere.get(cle) or video.get(cle) or "")
        return "" if valeur in ("unknown", "reserved", "unspecified") else valeur

    analyse.transfert = _couleur("color_transfer")
    analyse.primaires = _couleur("color_primaries")
    analyse.matrice = _couleur("color_space")

    # Donnees annexes : celles de l'image d'abord, puis celles du flux (que
    # certains conteneurs portent aussi, et ou vit toujours l'enregistrement
    # Dolby Vision).
    annexes = [*(premiere.get("side_data_list") or []), *(video.get("side_data_list") or [])]
    for donnees in annexes:
        genre = _type_donnee(donnees)
        if genre == "Mastering display metadata" and analyse.mastering is None:
            analyse.mastering = _mastering(donnees)
        elif genre == "Content light level metadata" and analyse.lumiere is None:
            analyse.lumiere = LumiereContenu(
                max_cll=_entier(donnees.get("max_content")),
                max_fall=_entier(donnees.get("max_average")),
            )
        elif genre == "DOVI configuration record" and analyse.dolby_vision is None:
            analyse.dolby_vision = _dolby_vision(donnees)
        elif "SMPTE2094-40" in genre or "HDR10+" in genre:
            analyse.hdr10plus = True
        elif genre.startswith("Dolby Vision") and analyse.dolby_vision is None:
            # Donnees RPU sans enregistrement de configuration : c'est bien du
            # Dolby Vision, mais on n'en connait pas le profil. Ne pas
            # l'inventer — « profil inconnu » dit exactement cela.
            analyse.dolby_vision = DolbyVision()

    # Un MP4 Dolby Vision s'annonce aussi par son etiquette de codec, meme
    # quand le demultiplexeur n'a pas remonte l'enregistrement.
    if analyse.dolby_vision is None and analyse.video_tag in ("dvh1", "dvhe", "dva1", "dvav"):
        analyse.dolby_vision = DolbyVision()
    return analyse


def _ffprobe_json(ffprobe: str, argv: list[str]) -> dict[str, Any] | None:
    try:
        sortie = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            [ffprobe, "-v", "error", *argv],
            capture_output=True,
            text=True,
            timeout=FFPROBE_DELAI,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("ffprobe a echoue (%s)", type(exc).__name__)
        return None
    if sortie.returncode != 0 or not sortie.stdout:
        return None
    try:
        return json.loads(sortie.stdout)
    except ValueError:
        return None


def sonder(chemin: Path) -> Analyse:
    """Lance ffprobe (deux passes bornees) et rend l'analyse. Ne leve jamais.

    Deux passes parce que la seconde lit la premiere IMAGE : c'est la que vivent
    les metadonnees HDR10 statiques et le HDR10+, et la lire demande de decoder
    une image — ``-read_intervals %+#1`` borne cette lecture a un paquet.
    """
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return Analyse(
            nom=chemin.name, extension=chemin.suffix.lower(), motif_illisible="ffprobe absent"
        )
    flux = _ffprobe_json(ffprobe, ["-show_streams", "-show_format", "-of", "json", str(chemin)])
    if flux is None:
        return Analyse(
            nom=chemin.name,
            extension=chemin.suffix.lower(),
            motif_illisible="ffprobe ne parvient pas à lire ce fichier",
        )
    image = _ffprobe_json(
        ffprobe,
        [
            "-read_intervals",
            "%+#1",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=pix_fmt,color_transfer,color_primaries,color_space,side_data_list",
            "-of",
            "json",
            str(chemin),
        ],
    )
    return lire_sonde(flux, image, nom=chemin.name)


def empreinte(chemin: Path) -> str:
    """Identite d'un fichier A UN INSTANT : chemin, taille, date de modification.

    Un fichier remplace sous le meme nom change d'empreinte, et rien de ce qui
    a ete appris sur l'ancien (analyse, controle) ne lui est attribue.
    """
    try:
        info = chemin.stat()
        return f"{chemin}|{info.st_size}|{info.st_mtime_ns}"
    except OSError:
        return str(chemin)


_ANALYSES: OrderedDict[str, Analyse] = OrderedDict()
_ANALYSES_MAX = 64
_ANALYSES_VERROU = threading.Lock()


def analyser(chemin: Path) -> Analyse:
    """``sonder`` avec memoire : l'ecran de verification demande le meme fichier
    trois fois (plan de lecture, session, fiche comparee), et deux passes de
    ffprobe sur un NAS ne sont pas gratuites.

    Seule une analyse COMPLETE est retenue. Une premiere image illisible (un
    ffprobe qui depasse son delai sur un NAS occupe, un disque qui tarde) est
    souvent passagere : la garder, c'etait ressortir la meme description
    incomplete a chaque demande — et le reencodage, qui refuse d'encoder sur
    une telle description, l'aurait refusee a nouveau la nuit suivante sans
    jamais relire le fichier.
    """
    cle = empreinte(chemin)
    with _ANALYSES_VERROU:
        if cle in _ANALYSES:
            _ANALYSES.move_to_end(cle)
            return _ANALYSES[cle]
    analyse = sonder(chemin)
    if analyse.lue and (analyse.premiere_image_lue or not analyse.a_video):
        with _ANALYSES_VERROU:
            _ANALYSES[cle] = analyse
            while len(_ANALYSES) > _ANALYSES_MAX:
                _ANALYSES.popitem(last=False)
    return analyse


# --- La decision de lecture ---------------------------------------------------


class Mode(StrEnum):
    DIRECT = "direct"
    """Le fichier lui-meme, servi avec les plages d'octets."""

    REMUX = "remux"
    """Reemballe en HLS, image COPIEE : rien n'est reencode."""

    TRANSCODE = "transcode"
    """Converti pour l'apercu seulement : 720p, 8 bits, SDR."""

    IMPOSSIBLE = "impossible"


CONTENEURS_DIRECTS = {".mp4", ".m4v", ".mov"}
AUDIO_NAVIGATEUR = {"aac", "mp3"}
"""Ce que tout navigateur lit dans un MP4."""

AUDIO_COPIABLE_HLS = {"aac"}
"""Ce qu'on recopie tel quel dans des segments fMP4. L'AAC seul : c'est le seul
audio que Safari (HLS natif) et hls.js acceptent tous deux dans ce format.
Reencoder un MP3 en AAC coute quelques pourcents d'un coeur, un apercu muet
couterait la verification."""

PIX_FMT_NAVIGATEUR = {"yuv420p", "yuvj420p"}
PROFILS_H264_8_BITS = {"Baseline", "Constrained Baseline", "Main", "Extended", "High"}
HAUTEUR_APERCU = 720


@dataclass(frozen=True, slots=True)
class Decision:
    mode: Mode
    motif: str
    """Pourquoi, en toutes lettres, pour l'ecran."""

    resume: str
    """Une ligne : « lecture directe », « réemballage, image intacte »…"""

    audio_copie: bool = False
    piste_audio: int | None = None
    hauteur: int = 0
    tonemap: bool = False
    avertissements: tuple[str, ...] = ()

    def en_dict(self) -> dict[str, Any]:
        return {
            "mode": str(self.mode),
            "motif": self.motif,
            "resume": self.resume,
            "audio_copie": self.audio_copie,
            "hauteur": self.hauteur,
            "tonemap": self.tonemap,
            "avertissements": list(self.avertissements),
        }


def h264_8_bits(analyse: Analyse) -> bool:
    """Le seul codec video que TOUS les navigateurs decodent.

    Le nom du codec ne suffit pas, et c'est l'erreur qui a fait croire Kandahar
    corrompu : un H.264 High 10 s'appelle « h264 » comme les autres, et aucun
    navigateur ne le decode. Il faut le format de pixel 4:2:0 8 bits — ou, a
    defaut, un profil qui l'implique.
    """
    if analyse.video_codec != "h264" or analyse.est_hdr:
        return False
    if analyse.pix_fmt:
        return analyse.pix_fmt in PIX_FMT_NAVIGATEUR
    return analyse.video_profil in PROFILS_H264_8_BITS


PIX_FMT_HEVC = {"yuv420p": "main", "yuv420p10le": "main10"}
"""Les deux profils HEVC qu'un navigateur peut decoder, par format de pixel."""

PROFILS_HEVC = {"Main": "main", "Main 10": "main10"}
DECODAGES_HEVC = ("main", "main10")
"""Ce que le lecteur peut declarer : « main10 » couvre aussi le Main 8 bits."""


def profil_hevc(analyse: Analyse) -> str:
    """« main », « main10 », ou "" pour un HEVC hors de portee d'un navigateur (4:2:2…)."""
    if analyse.video_codec != "hevc":
        return ""
    if analyse.pix_fmt:
        return PIX_FMT_HEVC.get(analyse.pix_fmt, "")
    return PROFILS_HEVC.get(analyse.video_profil, "")


def hevc_decodable(analyse: Analyse, hevc: str) -> bool:
    """Ce navigateur decode-t-il CE HEVC, et peut-on le lui donner tel quel ?

    ``hevc`` est ce que le lecteur a mesure chez lui (``MediaSource.
    isTypeSupported``) : Firefox 136 et suivants decodent le HEVC par le materiel
    sur macOS, Safari depuis longtemps, Chrome selon la machine. Le serveur ne
    peut pas le deviner ; le client le sait.

    Le HDR reste exclu meme quand le codec passe : un navigateur qui decode un
    HEVC 10 bits n'en restitue pas pour autant le HDR de facon fiable, et un
    apercu delave ferait croire a un mauvais encodage — sur l'ecran precis ou
    l'on juge l'encodage.
    """
    if analyse.est_hdr or hevc not in DECODAGES_HEVC:
        return False
    profil = profil_hevc(analyse)
    return profil == "main" or (profil == "main10" and hevc == "main10")


CODECS_LIBRES = ("vp9", "av1")
DECODAGES_LIBRES = ("8", "10")
"""Ce que le lecteur peut declarer pour le VP9 et l'AV1 : « 10 » couvre aussi le 8 bits."""

PIX_FMT_LIBRES = {"yuv420p": "8", "yuv420p10le": "10"}
"""Le 4:2:0 en 8 ou 10 bits : ce que les navigateurs decodent en VP9 (profils 0 et
2) et en AV1 (profil Main). Le 4:4:4 et le 12 bits restent hors de portee."""

CONTENEURS_WEBM = {".webm"}
AUDIO_WEBM = {"opus", "vorbis"}


def profondeur_libre(analyse: Analyse) -> str:
    """« 8 », « 10 », ou "" pour un VP9/AV1 hors de portee d'un navigateur."""
    if analyse.video_codec not in CODECS_LIBRES:
        return ""
    if analyse.pix_fmt:
        return PIX_FMT_LIBRES.get(analyse.pix_fmt, "")
    return str(analyse.profondeur) if analyse.profondeur in (8, 10) else ""


def libre_decodable(analyse: Analyse, *, vp9: str = "", av1: str = "") -> bool:
    """Ce navigateur decode-t-il CE VP9 ou CET AV1 ?

    Comme pour le HEVC, c'est le lecteur qui le mesure (``MediaSource.
    isTypeSupported``) : Firefox les decode tous deux, Safari selon la machine.
    Sans cette declaration, un WebM VP9 que Firefox lit tel quel etait converti
    par le NAS — et un AV1 4K converti en logiciel sur un Celeron, c'est la
    machine entiere occupee pour un apercu moins fidele que le fichier. Le HDR
    reste exclu pour la meme raison que le HEVC : un apercu delave accuserait
    l'encodage.
    """
    declare = {"vp9": vp9, "av1": av1}.get(analyse.video_codec, "")
    if analyse.est_hdr or declare not in DECODAGES_LIBRES:
        return False
    profondeur = profondeur_libre(analyse)
    return profondeur == "8" or (profondeur == "10" and declare == "10")


def _pourquoi_convertir(analyse: Analyse, hevc: str = "", vp9: str = "", av1: str = "") -> str:
    """La raison en clair : c'est elle qui empeche de soupconner le fichier."""
    codec = analyse.libelle_codec
    hdr = f" {analyse.libelle_hdr}" if analyse.est_hdr else ""
    if analyse.video_codec == "h264":
        if analyse.profondeur and analyse.profondeur > 8:
            return f"vidéo H.264 {analyse.profondeur} bits{hdr} : aucun navigateur ne la décode"
        if "422" in analyse.pix_fmt or "4:2:2" in analyse.video_profil:
            return f"vidéo H.264 4:2:2{hdr} : aucun navigateur ne la décode"
        if "444" in analyse.pix_fmt or "4:4:4" in analyse.video_profil:
            return f"vidéo H.264 4:4:4{hdr} : aucun navigateur ne la décode"
        if analyse.est_hdr:
            return f"vidéo H.264{hdr} : un navigateur l'afficherait délavée"
        return f"vidéo H.264 {analyse.video_profil or 'de profil inconnu'} : lecture incertaine"
    video = f"vidéo {analyse.libelle_video}{hdr}"
    if analyse.video_codec == "hevc":
        if analyse.est_hdr and hevc in DECODAGES_HEVC:
            return f"{video} : ce navigateur ne rend pas le HDR fidèlement"
        if profil_hevc(analyse) == "main10" and hevc == "main":
            return f"{video} : ce navigateur décode le HEVC 8 bits, pas le 10 bits"
        if hevc in DECODAGES_HEVC and not profil_hevc(analyse):
            echantillon = analyse.pix_fmt or analyse.video_profil
            return f"vidéo HEVC {echantillon}{hdr} : hors de portée d'un navigateur"
        return f"{video} : ce navigateur ne la décode pas"
    if analyse.video_codec in CODECS_LIBRES:
        declare = vp9 if analyse.video_codec == "vp9" else av1
        if analyse.est_hdr and declare in DECODAGES_LIBRES:
            return f"{video} : ce navigateur ne rend pas le HDR fidèlement"
        if profondeur_libre(analyse) == "10" and declare == "8":
            return f"{video} : ce navigateur ne décode ce codec qu'en 8 bits"
        if declare in DECODAGES_LIBRES and not profondeur_libre(analyse):
            echantillon = analyse.pix_fmt or analyse.video_profil
            return f"vidéo {codec} {echantillon}{hdr} : hors de portée d'un navigateur"
        return f"{video} : ce navigateur ne la décode pas"
    if analyse.video_codec in _CODECS_VIDEO:
        return f"{video} : aucun navigateur ne la lit"
    return f"vidéo {codec}{hdr} : les navigateurs ne la lisent pas"


def decider(
    analyse: Analyse,
    *,
    ffmpeg: bool = True,
    filtres: Iterable[str] = (),
    conversion: bool = False,
    hevc: str = "",
    vp9: str = "",
    av1: str = "",
) -> Decision:
    """Comment montrer ce fichier a un navigateur, et pourquoi.

    - ``direct`` : MP4/MOV en H.264 8 bits (ou en VP9/AV1 que CE navigateur
      declare decoder), audio AAC ou MP3 ; WebM en VP9/AV1 declare, audio Opus
      ou Vorbis ; rien a faire.
    - ``remux`` : les memes images dans un autre conteneur (ou avec un audio que
      les navigateurs ignorent), ou HEVC SDR que CE navigateur declare decoder
      (``hevc`` : « main10 », « main » ou vide ; ``vp9`` et ``av1`` : « 10 »,
      « 8 » ou vide) ; on change d'emballage, l'image n'est pas touchee.
    - ``transcode`` : tout le reste ; l'APERCU est converti en 720p 8 bits, et
      ramene en SDR quand ``zscale`` et ``tonemap`` le permettent.

    La mediatheque et l'ecran de reencodage passent TOUS DEUX par ici, avec les
    capacites que le navigateur a declarees : sans cela, l'une reemballerait ce
    que l'autre convertit, et le meme fichier aurait deux diagnostics.

    ``conversion`` force le dernier mode : c'est le recours quand le navigateur
    a echoue sur une lecture directe ou reemballee ALORS QUE le controle de
    decodage dit le fichier sain — il reste alors a lui donner ce qu'il sait
    lire a coup sur.

    ``filtres`` est la liste des filtres de ce ffmpeg (``filtres_ffmpeg``) :
    passee en argument pour que la decision reste une fonction pure.
    """
    if not analyse.lue:
        if analyse.motif_illisible == "ffprobe absent" and analyse.extension in CONTENEURS_DIRECTS:
            return Decision(
                Mode.DIRECT,
                "ffprobe absent : impossible de savoir ce que contient le fichier, "
                "lecture directe tentée.",
                "lecture directe",
            )
        return Decision(
            Mode.IMPOSSIBLE,
            f"{analyse.motif_illisible or 'fichier illisible'} : il est peut-être abîmé.",
            "lecture impossible",
        )
    if not analyse.a_video:
        return Decision(Mode.IMPOSSIBLE, "aucune piste vidéo dans ce fichier.", "pas de vidéo")

    piste = analyse.piste_audio_principale
    audio = analyse.audio[piste].codec if piste is not None else ""
    libre = libre_decodable(analyse, vp9=vp9, av1=av1)
    lisible = h264_8_bits(analyse) or hevc_decodable(analyse, hevc) or libre

    directe = (h264_8_bits(analyse) or libre) and (
        analyse.extension in CONTENEURS_DIRECTS and (piste is None or audio in AUDIO_NAVIGATEUR)
    )
    directe = directe or (
        libre and analyse.extension in CONTENEURS_WEBM and (piste is None or audio in AUDIO_WEBM)
    )
    if not conversion and directe:
        video = "H.264 8 bits" if analyse.video_codec == "h264" else analyse.libelle_video
        return Decision(
            Mode.DIRECT,
            f"{analyse.conteneur_lisible} en {video}"
            + (f", audio {_CODECS_AUDIO.get(audio, audio)}" if audio else "")
            + " : le navigateur lit le fichier tel quel.",
            "lecture directe",
            audio_copie=True,
            piste_audio=piste,
        )

    if not ffmpeg:
        return Decision(
            Mode.IMPOSSIBLE,
            f"ffmpeg absent : impossible de préparer une vidéo {analyse.libelle_video} "
            f"en {analyse.conteneur_lisible} pour le navigateur.",
            "lecture impossible",
        )

    if lisible and not conversion:
        copie = audio in AUDIO_COPIABLE_HLS
        precision = (
            f" ; audio {analyse.audio[piste].format} converti en AAC stéréo"
            if piste is not None and not copie
            else ""
        )
        video = (
            "H.264 8 bits"
            if analyse.video_codec == "h264"
            else f"{analyse.libelle_video}, que ce navigateur décode,"
        )
        return Decision(
            Mode.REMUX,
            f"{video} en {analyse.conteneur_lisible} : réemballage, l'image n'est pas "
            f"touchée{precision}.",
            "réemballage, image intacte",
            audio_copie=copie,
            piste_audio=piste,
        )

    cause = (
        "le navigateur n'a pas su lire la version non convertie"
        if lisible
        else _pourquoi_convertir(analyse, hevc, vp9, av1)
    )
    return _decision_conversion(analyse, set(filtres), piste, cause)


def _decision_conversion(
    analyse: Analyse, filtres: set[str], piste: int | None, cause: str
) -> Decision:
    hauteur = min(HAUTEUR_APERCU, analyse.hauteur or HAUTEUR_APERCU)
    hauteur -= hauteur % 2
    tonemap = analyse.est_hdr and {"zscale", "tonemap"} <= filtres
    avertissements: list[str] = []
    if analyse.est_hdr and not tonemap:
        avertissements.append(
            "Couleurs délavées dans l'aperçu : ce ffmpeg n'a pas de quoi convertir le HDR "
            "(filtres zscale et tonemap absents). Le fichier n'est pas en cause."
        )
    dv = analyse.dolby_vision
    if dv is not None and not dv.base_exploitable:
        avertissements.append(
            f"{dv.libelle} : {dv.explication}. L'aperçu aura des couleurs fausses ; "
            "c'est l'aperçu, pas le fichier."
        )
    return Decision(
        Mode.TRANSCODE,
        # L'audio est toujours ramene en AAC stereo ici : l'image est deja
        # convertie, le dire en plus n'apprendrait rien a personne.
        f"{cause}, conversion de l'aperçu en {hauteur}p" + (" SDR" if tonemap else "") + ".",
        f"aperçu converti en {hauteur}p — ce n'est pas la qualité du fichier",
        audio_copie=False,
        piste_audio=piste,
        hauteur=hauteur,
        tonemap=tonemap,
        avertissements=tuple(avertissements),
    )


@lru_cache(maxsize=1)
def filtres_ffmpeg() -> frozenset[str]:
    """Les filtres que CE ffmpeg propose. Lu une fois : il ne change pas en cours de route.

    ``zscale`` depend de la bibliotheque zimg, absente de certaines
    compilations (celle de Homebrew, par exemple) : le supposer present ferait
    echouer toute conversion HDR au lieu d'en livrer un apercu delave.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return frozenset()
    try:
        sortie = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            [ffmpeg, "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=FFPROBE_DELAI,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    noms = set()
    for ligne in sortie.stdout.splitlines():
        champs = ligne.split()
        if len(champs) >= 2 and re.fullmatch(r"[.A-Z|]{2,4}", champs[0]):
            noms.add(champs[1])
    return frozenset(noms)


# --- Les commandes ------------------------------------------------------------

SEGMENT_SECONDES = 6


def _filtre_video(analyse: Analyse, decision: Decision) -> str:
    """Mise a l'echelle PUIS conversion des couleurs.

    Dans cet ordre, et pas l'inverse : ``zscale`` travaille en flottants 32
    bits, et lui donner 720 lignes plutot que 2160 divise son cout par neuf.
    Les proprietes d'entree (PQ ou HLG, primaires BT.2020) sont ecrites en dur :
    un MKV les porte souvent dans le flux seulement, et zscale refuse de
    convertir ce qu'on ne lui a pas decrit.
    """
    etapes = []
    if analyse.hauteur and analyse.hauteur > decision.hauteur:
        etapes.append(f"scale=-2:{decision.hauteur}")
    if decision.tonemap:
        entree = "arib-std-b67" if analyse.hdr is TypeHdr.HLG else "smpte2084"
        if analyse.dolby_vision is not None and analyse.dolby_vision.base == "hlg":
            entree = "arib-std-b67"
        etapes += [
            f"zscale=tin={entree}:pin=bt2020:min=bt2020nc:rin=tv:t=linear:npl=100",
            "format=gbrpf32le",
            "zscale=p=bt709",
            "tonemap=tonemap=hable:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
        ]
    etapes.append("format=yuv420p")
    return ",".join(etapes)


def commande_hls(
    ffmpeg: str, source: Path, dossier: Path, analyse: Analyse, decision: Decision, at: float
) -> list[str]:
    """Argv complet, sans shell, qui ecrit une liste de lecture HLS dans ``dossier``.

    Segments fMP4 de six secondes : le format que Safari lit nativement et que
    hls.js sait passer aux Media Source Extensions sans rien retransformer.
    ``temp_file`` fait ecrire chaque segment sous un nom provisoire : un segment
    a moitie ecrit n'est jamais servi.

    ``-ss`` AVANT ``-i`` : le positionnement saute directement a l'image cle
    la plus proche au lieu de decoder tout ce qui precede — sur un 4K, c'est la
    difference entre une seconde et une minute d'attente.

    **Les horodatages restent ceux du film.** ``-output_ts_offset`` rend a
    chaque image sa position reelle dans le fichier, et ``frag_discont`` l'ecrit
    telle quelle dans chaque segment (``tfdt``) au lieu de tout ramener a zero.
    Sans cela, la position affichee ne pouvait etre que « point demande + temps
    ecoule » : sur un fichier tronque a 1:12 qui annonce 2:00, « 90 % »
    affichait 1:48 sur des images de 1:10 — le geste « verifier la fin »
    mentait. C'est aussi ce qui garde le son cale sur l'image quand les deux
    pistes ne commencent pas au meme instant : les decalages ecrits en « edit
    list », les Media Source Extensions les ignorent.

    La conversion part, comme la copie, de l'image cle qui precede
    (``-noaccurate_seek``, ``-fps_mode passthrough``) : c'est ainsi qu'un
    saut au-dela de la fin reelle montre les dernieres images qui existent, au
    lieu de ne rien produire du tout.

    Pas de ``-readrate`` pour freiner la production, bien qu'il ait ete
    essaye : sur un MKV reel a plusieurs pistes, ffmpeg 9 lisait vingt
    secondes de film en douze a vingt-trois secondes — plus lentement que la
    lecture. Le frein est la regulation des sessions (``Sessions``), qui
    suspend ffmpeg.
    """
    argv = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
    if at > 0:
        if decision.mode is Mode.TRANSCODE:
            argv += ["-noaccurate_seek"]
        argv += ["-ss", f"{at:.3f}"]
    argv += ["-i", str(source), "-map", "0:v:0"]
    if decision.piste_audio is not None:
        argv += ["-map", f"0:a:{decision.piste_audio}"]
    argv += ["-sn", "-dn"]

    if decision.mode is Mode.REMUX:
        argv += ["-c:v", "copy"]
        if analyse.video_codec == "hevc":
            # Un MKV porte souvent son HEVC en « hev1 » (parametres dans le
            # flux) : Safari et Firefox ne lisent dans un MP4 que « hvc1 »
            # (parametres dans l'en-tete). Sans cette etiquette, un HEVC
            # parfaitement decodable resterait un ecran noir.
            argv += ["-tag:v", "hvc1"]
    else:
        argv += [
            "-vf",
            _filtre_video(analyse, decision),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            # Plafond de debit : un apercu n'a pas a saturer le reseau local.
            "-maxrate",
            "5M",
            "-bufsize",
            "10M",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            # Pas d'images B dans un apercu. Leur reordonnancement, combine aux
            # horodatages d'origine gardes tels quels, donnait sous le ffmpeg
            # 7.1 de l'image Docker des temps de decodage EN DOUBLE (« 219 >=
            # 219 », une image sur deux) : de quoi faire saccader ou refuser
            # un segment. Sans images B, l'ordre de decodage est celui de
            # l'affichage. Un apercu n'y perd rien qui se voie.
            "-bf",
            "0",
            # Les images gardent leurs horodatages : en cadence fixe, ffmpeg
            # jetterait celles d'avant le point demande — toutes, quand le
            # fichier s'arrete avant.
            "-fps_mode",
            "passthrough",
            # Une image cle toutes les six secondes, comptees depuis la
            # premiere : sans elle, les segments suivraient les images cles de
            # l'encodeur et leur duree varierait. Comptees depuis la premiere
            # et non depuis zero : les horodatages sont ceux du film.
            "-force_key_frames",
            f"expr:if(isnan(prev_forced_t),1,gte(t,prev_forced_t+{SEGMENT_SECONDES}))",
        ]
        if decision.tonemap:
            argv += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]

    if decision.piste_audio is not None:
        if decision.audio_copie:
            # L'AAC d'un MPEG-TS est en ADTS ; un MP4 veut de l'AAC « brut ».
            # Sans ce filtre, ffmpeg refuse le premier paquet et la session
            # meurt sans image. Sans effet sur l'AAC d'un MKV ou d'un MP4.
            argv += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
        else:
            argv += ["-c:a", "aac", "-ac", "2", "-b:a", "160k"]

    if at > 0:
        argv += ["-output_ts_offset", f"{at:.3f}"]
    argv += [
        "-max_muxing_queue_size",
        "2048",
        "-f",
        "hls",
        "-hls_segment_options",
        # Une base de temps fine (1/90 000 s, celle de la video numerique) :
        # la base par defaut suivait la cadence, trop grossiere pour ecrire
        # sans collision des horodatages d'origine decales.
        "movflags=+frag_discont:video_track_timescale=90000",
        "-hls_time",
        str(SEGMENT_SECONDES),
        "-hls_list_size",
        "0",
        "-hls_playlist_type",
        "event",
        "-hls_segment_type",
        "fmp4",
        "-hls_fmp4_init_filename",
        "init.mp4",
        "-hls_segment_filename",
        str(dossier / "seg_%05d.m4s"),
        "-hls_flags",
        "independent_segments+temp_file",
        "-start_number",
        "0",
        str(dossier / "index.m3u8"),
    ]
    return argv


# --- Les sessions HLS ---------------------------------------------------------

NOM_SESSION = re.compile(r"[0-9a-f]{32}")
NOMS_SERVIS = re.compile(r"index\.m3u8|init\.mp4|seg_\d{5,9}\.m4s")
"""Les SEULS noms qu'une session sert. Tout le reste — « .. », un chemin, le
journal de ffmpeg, un segment provisoire en « .tmp » — est refuse avant meme de
chercher la session.

Ces motifs s'emploient TOUJOURS avec ``fullmatch`` : un « $ » laisse passer un
saut de ligne final, et « init.mp4\\n » n'est pas un nom que la session produit."""

_SEGMENT = re.compile(r"seg_(\d{5,9})\.m4s")

INACTIVITE = 60.0
"""Une session sans lecture depuis une minute est arretee et son dossier efface.
Le lecteur envoie un signe de vie toutes les 20 s tant qu'il lit : une minute
laisse passer deux signes perdus sans couper une lecture reelle."""

AVANCE_MAX = 8
"""Segments d'avance (≈ 48 s) au-dela desquels ffmpeg est SUSPENDU. Sans ce
frein, un reemballage produirait tout le film en quelques minutes — 20 Go de
cache pour quelqu'un qui voulait voir trois scenes."""

AVANCE_REPRISE = 4
CACHE_MAX_OCTETS = 2 * 1024**3
"""Plafond du cache de lecture. Il partage le volume de donnees avec le journal
d'annulation, la seule chose vraiment precieuse ici."""

REPRISE_CACHE = 0.8
"""Production suspendue pour cache plein : elle reprend sous 80 % du plafond,
pas au premier octet libere — sinon ffmpeg serait arrete et relance a chaque
segment."""

ECART_SAUT = 15.0
"""Au-dela, la premiere image montree est « loin » du point demande, et on le
dit. Une copie part de l'image cle qui precede : dix secondes d'ecart sont
normales avec un x264 regle par defaut, pas trente-huit."""

RETENUE = 20
"""Segments deja lus gardes derriere la position (≈ 2 min), quand le cache
deborde : assez pour revenir un peu en arriere, pas pour tout garder."""

SESSIONS_MAX = 3
TOMBES_MAX = 64
PAS_ENTRETIEN = 0.5
PAS_RAPIDE = 0.05
"""Le pas de l'entretien quand ffmpeg produit. Un reemballage copie a la
vitesse du disque — un fichier deja en memoire, a plusieurs centaines de Mo/s :
entre deux passages, il ecrit ce que le disque debite pendant ce temps, et c'est
ce qui borne le depassement du plafond du cache. Une conversion lente, elle, ne
sort un segment que toutes les quelques secondes : l'entretien revient alors a
la demi-seconde, et ne coute rien au NAS."""


class RaisonFin(StrEnum):
    INACTIVE = "inactive"
    REMPLACEE = "remplacee"
    RELANCEE = "relancee"
    FERMEE = "fermee"
    CACHE = "cache"
    TROP = "trop"


MOTIFS_FIN = {
    RaisonFin.INACTIVE: "Aperçu mis en veille après une minute sans lecture, pour libérer le NAS.",
    RaisonFin.REMPLACEE: (
        "Aperçu interrompu : une autre conversion a démarré, et le NAS n'en fait qu'une à la fois."
    ),
    RaisonFin.RELANCEE: "Aperçu relancé à une autre position.",
    RaisonFin.FERMEE: "Aperçu fermé.",
    RaisonFin.CACHE: "Aperçu arrêté : le cache de lecture était plein.",
    RaisonFin.TROP: "Aperçu arrêté : trop d'aperçus ouverts en même temps.",
}


class NomRefuse(ValueError):
    """Nom de fichier hors de ceux qu'une session produit."""


class SessionInconnue(LookupError):
    """Session absente. ``raison`` est rempli quand on sait pourquoi elle a fini."""

    def __init__(self, raison: RaisonFin | None = None) -> None:
        self.raison = raison
        super().__init__(self.motif)

    @property
    def motif(self) -> str:
        return MOTIFS_FIN[self.raison] if self.raison else "Session de lecture inconnue."


class FichierAbsent(LookupError):
    """Le nom est valide, la session existe, mais le fichier n'y est pas (ou plus)."""


class ConversionEchouee(RuntimeError):
    """ffmpeg s'est arrete sans produire de quoi lire."""


@dataclass
class Session:
    id: str
    cle: str
    """Ce que la session montre (« plan:ab12 », « transcode:cd34 ») : deux
    sessions sur la meme cle sont deux positions dans le meme film, et la
    seconde remplace la premiere."""

    source: Path
    decision: Decision
    at: float
    dossier: Path
    processus: Any
    cree: float
    dernier_acces: float
    dernier_segment: int = -1
    suspendue: bool = False
    code_retour: int | None = None
    erreur: str = ""
    vus: int = 0
    """Segments termines au passage precedent de l'entretien : s'il en est
    apparu depuis, ffmpeg produit, et l'entretien presse le pas."""

    duree: float | None = None
    """Ce que le fichier ANNONCE : c'est a elle qu'on compare la fin reelle."""

    debut: float | None = None
    """Position reelle, dans le film, de ce que le lecteur appelle « 0 ». Lue
    dans le premier segment : c'est l'origine que hls.js donne a la video."""

    premiere_image: float | None = None
    """Position reelle de la premiere image montree. Loin du point demande,
    elle trahit un fichier qui s'arrete avant ce qu'il annonce."""

    @property
    def mode(self) -> Mode:
        return self.decision.mode

    def produits(self) -> int:
        """Nombre de segments TERMINES (les provisoires en .tmp ne comptent pas)."""
        plus_haut = -1
        with contextlib.suppress(OSError):
            for entree in self.dossier.iterdir():
                if m := _SEGMENT.fullmatch(entree.name):
                    plus_haut = max(plus_haut, int(m.group(1)))
        return plus_haut + 1

    def liste_texte(self) -> str | None:
        try:
            return (self.dossier / "index.m3u8").read_text(encoding="utf-8")
        except OSError:
            return None

    def lire_origines(self) -> None:
        """Lit une fois, dans le premier segment, ou le film commence vraiment."""
        if self.debut is not None:
            return
        try:
            init = (self.dossier / "init.mp4").read_bytes()
            with (self.dossier / "seg_00000.m4s").open("rb") as segment:
                # Le « moof » est en tete : inutile de lire des megaoctets d'image.
                debut = segment.read(1024 * 1024)
        except OSError:
            return
        self.debut, self.premiere_image = origines_hls(init, debut)

    def en_dict(self) -> dict[str, Any]:
        liste = self.liste_texte() or ""
        vivant = self.code_retour is None and self.processus.poll() is None
        pret = "#EXTINF" in liste
        termine = "#EXT-X-ENDLIST" in liste
        if pret:
            self.lire_origines()
        fin = None
        if termine:
            depart = self.premiere_image if self.premiere_image is not None else self.debut
            if depart is not None:
                fin = depart + duree_liste(liste)
        return {
            "session": self.id,
            "mode": str(self.mode),
            "at": self.at,
            "pret": pret,
            "termine": termine,
            "produits": self.produits(),
            "suspendue": self.suspendue,
            "vivant": vivant,
            "erreur": self.erreur,
            "debut": self.debut,
            "premiere_image": self.premiere_image,
            "fin": fin,
            "alerte": alerte_position(self.at, self.premiere_image, fin=fin, duree=self.duree),
            "url": f"/api/media/lecture/{self.id}/index.m3u8",
        }


def temps_lisible(secondes: float) -> str:
    """« 1:12 », « 1:02:05 » : l'ecriture du lecteur, pour que les deux se lisent pareil."""
    total = max(0, round(secondes))
    heures, reste = divmod(total, 3600)
    minutes, sec = divmod(reste, 60)
    if heures:
        return f"{heures}:{minutes:02d}:{sec:02d}"
    return f"{minutes}:{sec:02d}"


def duree_liste(liste: str) -> float:
    """Somme des ``#EXTINF`` : la duree de ce que la session a produit."""
    total = 0.0
    for valeur in re.findall(r"^#EXTINF:([0-9.]+)", liste, flags=re.MULTILINE):
        with contextlib.suppress(ValueError):
            total += float(valeur)
    return total


def alerte_position(
    at: float, premiere_image: float | None, *, fin: float | None, duree: float | None
) -> str:
    """Ce qu'il faut dire quand l'endroit atteint n'est pas l'endroit demande.

    Le cas qui l'a fait naitre : un fichier tronque a 1:12 qui annonce 2:00.
    « Aller a 90 % » y montre les dernieres images qui existent, vers 1:10 ;
    les afficher sans rien dire laissait croire que la fin etait la. Vide quand
    il n'y a rien a signaler.
    """
    if premiere_image is None:
        return ""
    loin_avant = premiere_image < at - ECART_SAUT
    if fin is not None and duree and fin < duree - ECART_DUREE_MAX:
        arret = (
            f"le fichier s'arrête vers {temps_lisible(fin)} alors qu'il annonce "
            f"{temps_lisible(duree)}."
        )
        if loin_avant:
            return f"Rien à {temps_lisible(at)} : {arret}"
        return arret[0].upper() + arret[1:]
    if loin_avant:
        return (
            f"Rien à {temps_lisible(at)} : les images montrées partent de "
            f"{temps_lisible(premiere_image)}, le point le plus proche que le fichier contient."
        )
    if premiere_image > at + ECART_SAUT:
        return (
            f"Aucune image entre {temps_lisible(at)} et {temps_lisible(premiere_image)} : "
            "la lecture reprend à la première qui suit."
        )
    return ""


# --- Lecture des segments fMP4 ------------------------------------------------


def _boites(donnees: bytes, debut: int = 0, fin: int | None = None):
    """Les boites ISO BMFF de ``donnees[debut:fin]`` : (type, debut du contenu, fin)."""
    fin = len(donnees) if fin is None else fin
    i = debut
    while i + 8 <= fin:
        taille, genre = struct.unpack_from(">I4s", donnees, i)
        entete = 8
        if taille == 1:
            if i + 16 > fin:
                return
            taille = struct.unpack_from(">Q", donnees, i + 8)[0]
            entete = 16
        elif taille == 0:
            taille = fin - i
        if taille < entete:
            return
        yield genre.decode("latin-1"), i + entete, min(i + taille, fin)
        i += taille


def _champ(donnees: bytes, position: int, large: bool) -> int:
    return struct.unpack_from(">Q" if large else ">I", donnees, position)[0]


def origines_hls(init: bytes, segment: bytes) -> tuple[float | None, float | None]:
    """(origine du lecteur, premiere image), en secondes du film, lues dans le fMP4.

    Pure : on lit les boites a la main plutot que de lancer ffprobe, parce que
    seules deux valeurs comptent — l'echelle de temps de chaque piste
    (``init.mp4``, boite ``mdhd``) et l'instant de depart de chaque piste dans
    le premier segment (``tfdt``). L'origine est la plus petite des deux : c'est
    ainsi que hls.js cale le « 0 » de la video.
    """
    pistes: dict[int, tuple[int, str]] = {}
    debuts: list[tuple[str, float]] = []
    try:
        for genre, a, b in _boites(init):
            if genre != "moov":
                continue
            for genre2, a2, b2 in _boites(init, a, b):
                if genre2 != "trak":
                    continue
                ident, echelle, nature = None, 0, ""
                for genre3, a3, b3 in _boites(init, a2, b2):
                    if genre3 == "tkhd":
                        ident = _champ(init, a3 + (20 if init[a3] == 1 else 12), False)
                    elif genre3 == "mdia":
                        for genre4, a4, _b4 in _boites(init, a3, b3):
                            if genre4 == "mdhd":
                                echelle = _champ(init, a4 + (20 if init[a4] == 1 else 12), False)
                            elif genre4 == "hdlr":
                                nature = init[a4 + 8 : a4 + 12].decode("latin-1")
                if ident is not None and echelle:
                    pistes[ident] = (echelle, nature)
        for genre, a, b in _boites(segment):
            if genre != "moof":
                continue
            for genre2, a2, b2 in _boites(segment, a, b):
                if genre2 != "traf":
                    continue
                ident, instant = None, None
                for genre3, a3, _b3 in _boites(segment, a2, b2):
                    if genre3 == "tfhd":
                        ident = _champ(segment, a3 + 4, False)
                    elif genre3 == "tfdt":
                        instant = _champ(segment, a3 + 4, segment[a3] == 1)
                if ident in pistes and instant is not None:
                    echelle, nature = pistes[ident]
                    debuts.append((nature, instant / echelle))
            break
    except (struct.error, IndexError):
        return None, None
    if not debuts:
        return None, None
    video = next((t for nature, t in debuts if nature == "vide"), None)
    return min(t for _nature, t in debuts), video


def _derniere_ligne(fichier: Path) -> str:
    try:
        lignes = fichier.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return _nettoyer_ligne(lignes[-1]) if lignes else ""


def _nettoyer_ligne(ligne: str) -> str:
    """« [h264 @ 0x7be8c2b800] … » -> « [h264] … » : l'adresse ne dit rien a personne."""
    return re.sub(r" @ 0x[0-9a-f]+\]", "]", ligne.strip())


def _lancer_ffmpeg(argv: list[str], journal: Path) -> subprocess.Popen:
    with journal.open("wb") as sortie_erreurs:
        return subprocess.Popen(  # noqa: S603 - argv fixe, pas de shell
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=sortie_erreurs,
        )


class Sessions:
    """Les conversions en cours pour le lecteur, et leurs garde-fous.

    Tout ce qui suit existe parce que l'application tourne sur un NAS :

    - **une seule conversion ``transcode`` a la fois** : un 4K HEVC converti en
      720p occupe tout un processeur de NAS ; deux, et la machine ne sert plus
      les films qu'on regarde vraiment. Une nouvelle session tue la precedente ;
    - **une session inactive depuis 60 s est arretee**, son dossier efface :
      fermer l'onglet ne doit pas laisser tourner une conversion de deux heures ;
    - **ffmpeg est suspendu** des qu'il a ~48 s d'avance sur ce que le lecteur a
      demande, et relance quand le lecteur se rapproche ;
    - **le cache est borne** en taille, derniere session comprise : ce qui a
      ete lu est libere d'abord, puis les sessions les plus anciennes sont
      arretees, et si la derniere deborde encore, sa production est suspendue ;
    - **aucun chemin ne vient du client** : une session est designee par un
      identifiant tire au hasard, et elle ne sert que des noms qu'elle produit.
    """

    def __init__(
        self,
        racine: Path,
        *,
        lancer: Callable[[list[str], Path], Any] = _lancer_ffmpeg,
        horloge: Callable[[], float] = time.monotonic,
        entretien_auto: bool = True,
        cache_max: int = CACHE_MAX_OCTETS,
    ) -> None:
        self.racine = racine
        self._lancer = lancer
        self._horloge = horloge
        self._entretien_auto = entretien_auto
        self._cache_max = cache_max
        self._sessions: dict[str, Session] = {}
        self._tombes: OrderedDict[str, RaisonFin] = OrderedDict()
        self._verrou = threading.RLock()
        self._fil: threading.Thread | None = None
        self._arret = threading.Event()
        self._reveil = threading.Event()
        """Reveille l'entretien avant la fin de son pas : une reprise de ffmpeg
        doit etre suivie de pres, pas une demi-seconde plus tard."""
        self._plein = False
        """Cache au-dela du plafond apres toutes les liberations possibles :
        la regulation suspend alors la production."""
        self._purger_orphelins()

    # -- cycle de vie --

    def ouvrir(
        self,
        *,
        cle: str,
        source: Path,
        analyse: Analyse,
        decision: Decision,
        at: float,
        ffmpeg: str,
    ) -> Session:
        """Demarre une session. Tue ce qu'il faut tuer AVANT de lancer ffmpeg."""
        if decision.mode not in (Mode.REMUX, Mode.TRANSCODE):
            raise ValueError(f"mode {decision.mode} : rien a convertir")
        with self._verrou:
            for autre in list(self._sessions.values()):
                if autre.cle == cle:
                    self._arreter(autre, RaisonFin.RELANCEE)
                elif decision.mode is Mode.TRANSCODE and autre.mode is Mode.TRANSCODE:
                    self._arreter(autre, RaisonFin.REMPLACEE)
            while len(self._sessions) >= SESSIONS_MAX:
                plus_vieille = min(self._sessions.values(), key=lambda s: s.dernier_acces)
                self._arreter(plus_vieille, RaisonFin.TROP)

            sid = uuid.uuid4().hex
            dossier = self.racine / sid
            dossier.mkdir(parents=True, exist_ok=False)
            argv = commande_hls(ffmpeg, source, dossier, analyse, decision, at)
            processus = self._lancer(argv, dossier / "ffmpeg.log")
            maintenant = self._horloge()
            session = Session(
                id=sid,
                cle=cle,
                source=source,
                decision=decision,
                at=at,
                dossier=dossier,
                processus=processus,
                cree=maintenant,
                dernier_acces=maintenant,
                duree=analyse.duree,
            )
            self._sessions[sid] = session
            logger.info(
                "lecture : session %s (%s) sur %s a %.0f s", sid[:8], decision.mode, source.name, at
            )
        if self._entretien_auto:
            self._assurer_entretien()
            # Un reemballage demarre a la vitesse du disque : la premiere
            # regulation ne doit pas attendre la fin d'un pas lent.
            self._reveil.set()
        return session

    def arreter(self, sid: str, raison: RaisonFin = RaisonFin.FERMEE) -> bool:
        with self._verrou:
            session = self._sessions.get(sid)
            if session is None:
                return False
            self._arreter(session, raison)
            return True

    def arreter_tout(self) -> None:
        """Appele a l'extinction : aucun ffmpeg ne doit survivre au serveur."""
        self._arret.set()
        self._reveil.set()
        with self._verrou:
            for session in list(self._sessions.values()):
                self._arreter(session, RaisonFin.FERMEE)

    def _arreter(self, session: Session, raison: RaisonFin) -> None:
        # SIGKILL et non SIGTERM : un processus SUSPENDU garderait un SIGTERM
        # en attente jusqu'a sa reprise, et resterait la indefiniment.
        processus = session.processus
        with contextlib.suppress(OSError, ProcessLookupError):
            if processus.poll() is None:
                processus.kill()
        with contextlib.suppress(Exception):
            processus.wait(timeout=5)
        self._effacer(session.dossier)
        self._sessions.pop(session.id, None)
        self._tombes[session.id] = raison
        while len(self._tombes) > TOMBES_MAX:
            self._tombes.popitem(last=False)
        logger.info("lecture : session %s arretee (%s)", session.id[:8], raison)

    def _effacer(self, dossier: Path) -> None:
        """N'efface QUE des dossiers de session, sous la racine. Jamais autre chose."""
        if dossier.parent != self.racine or not NOM_SESSION.fullmatch(dossier.name):
            logger.error("effacement refuse hors du cache de lecture : %s", dossier)
            return
        shutil.rmtree(dossier, ignore_errors=True)

    def _purger_orphelins(self) -> None:
        """Au demarrage, plus aucune session ne reclame ce qui traine dans le cache."""
        with contextlib.suppress(OSError):
            for entree in self.racine.iterdir():
                if entree.is_dir() and NOM_SESSION.fullmatch(entree.name):
                    shutil.rmtree(entree, ignore_errors=True)

    # -- acces --

    def _session(self, sid: str) -> Session:
        if not NOM_SESSION.fullmatch(sid):
            raise SessionInconnue()
        with self._verrou:
            session = self._sessions.get(sid)
            if session is None:
                raise SessionInconnue(self._tombes.get(sid))
            return session

    def fichier(self, sid: str, nom: str) -> Path:
        """Le chemin d'un fichier de session, apres TOUTES les verifications.

        Le nom est verifie contre une liste blanche AVANT de chercher la
        session : une tentative de traversee ne touche meme pas au registre. Le
        chemin resolu doit ensuite avoir le dossier de session pour parent —
        une seconde barriere, au cas ou la premiere aurait un trou.
        """
        if not NOMS_SERVIS.fullmatch(nom):
            raise NomRefuse(nom)
        session = self._session(sid)
        chemin = session.dossier / nom
        if chemin.resolve().parent != session.dossier.resolve():
            raise NomRefuse(nom)
        with self._verrou:
            if m := _SEGMENT.fullmatch(nom):
                session.dernier_segment = int(m.group(1))
                session.dernier_acces = self._horloge()
                self._reguler(session)
            elif nom == "init.mp4":
                session.dernier_acces = self._horloge()
        if not chemin.is_file():
            raise FichierAbsent(nom)
        return chemin

    def liste(self, sid: str, *, attente: float = 20.0, pas: float = 0.25) -> str:
        """La liste de lecture, une fois qu'elle annonce au moins un segment.

        On attend ici plutot que de repondre 404 : Safari, en HLS natif, ne
        reessaie guere une liste absente, et le premier segment d'un 4K
        converti met plusieurs secondes a sortir.

        ``#EXT-X-START`` est ajoute : sans lui, une liste de type EVENT se lit
        « en direct », a partir de la fin — un lecteur arrive en cours de
        conversion demarrerait trente secondes apres l'endroit demande.
        """
        session = self._session(sid)
        limite = self._horloge() + attente
        while True:
            texte = session.liste_texte()
            if texte and "#EXTINF" in texte:
                break
            if (code := session.processus.poll()) is not None:
                texte = session.liste_texte()
                if texte and "#EXTINF" in texte:
                    break
                self._constater_fin(session, code)
                raise ConversionEchouee(session.erreur or "ffmpeg s'est arrêté sans rien produire")
            if self._horloge() >= limite:
                raise FichierAbsent("index.m3u8")
            time.sleep(pas)
        if "#EXT-X-START" not in texte:
            texte = texte.replace(
                "#EXTM3U\n", "#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\n", 1
            )
        return texte

    def toucher(self, sid: str) -> dict[str, Any]:
        """Signe de vie du lecteur. Rend l'etat, pour qu'il sache s'il doit s'inquieter."""
        session = self._session(sid)
        with self._verrou:
            session.dernier_acces = self._horloge()
        return self.etat(sid)

    def etat(self, sid: str) -> dict[str, Any]:
        """L'etat de la session. Le demander est un signe de vie.

        Pendant la preparation, le lecteur n'a encore aucun segment a lire : il
        interroge l'etat en attendant le premier. Sans cela, un 4K converti qui
        met plus d'une minute a sortir son premier segment etait arrete comme
        « inactif » alors que quelqu'un l'attendait. Un lecteur en pause, lui,
        n'interroge rien : il ne tient pas la conversion ouverte.
        """
        session = self._session(sid)
        with self._verrou:
            session.dernier_acces = self._horloge()
        code = session.processus.poll()
        if code is not None and session.code_retour is None:
            self._constater_fin(session, code)
        return session.en_dict()

    @staticmethod
    def _constater_fin(session: Session, code: int) -> None:
        """Note la fin de ffmpeg, et ce qu'elle veut dire quand rien n'est sorti."""
        if session.code_retour is not None:
            return
        session.code_retour = code
        if code != 0:
            session.erreur = _derniere_ligne(session.dossier / "ffmpeg.log")
        elif session.at > 0 and "#EXTINF" not in (session.liste_texte() or ""):
            # Arret normal sans une image : l'endroit demande est au-dela de ce
            # que le fichier contient. Le dire, plutot que « a echoue ».
            session.erreur = f"aucune image à partir de {temps_lisible(session.at)}" + (
                f" : le fichier s'arrête avant, alors qu'il annonce {temps_lisible(session.duree)}"
                if session.duree
                else " : le fichier s'arrête avant"
            )

    def actives(self) -> list[Session]:
        with self._verrou:
            return list(self._sessions.values())

    # -- entretien --

    def _reguler(self, session: Session) -> None:
        """Suspend ffmpeg quand il a trop d'avance, le relance quand on le rattrape.

        SIGSTOP plutot que tuer : relancer a la bonne position demanderait de
        recommencer la conversion, et le lecteur perdrait ce qu'il a deja.

        Le cache plein freine aussi, sauf quand le lecteur a tout lu et attend
        le segment suivant : suspendre alors bloquerait la lecture pour de bon.
        Le plafond peut donc etre depasse du seul segment que le lecteur attend.
        """
        if not hasattr(signal, "SIGSTOP") or session.processus.poll() is not None:
            return
        avance = session.produits() - 1 - session.dernier_segment
        plein = self._plein and avance > 0
        try:
            if not session.suspendue and (avance >= AVANCE_MAX or plein):
                session.processus.send_signal(signal.SIGSTOP)
                session.suspendue = True
            elif session.suspendue and avance <= AVANCE_REPRISE and not plein:
                session.processus.send_signal(signal.SIGCONT)
                session.suspendue = False
                # ffmpeg repart a la vitesse du disque : l'entretien doit le
                # suivre au pas rapide des maintenant, pas au bout de son
                # attente — c'etait des dizaines de segments de trop.
                session.vus = -1
                self._reveil.set()
        except (OSError, ProcessLookupError):
            return

    def entretien(self, *, cache: bool = True) -> bool:
        """Un passage : inactivite, fin de ffmpeg, taille du cache, puis regulation.

        La taille d'abord : c'est elle qui dit si la regulation doit freiner.
        Rend vrai quand une session a produit depuis le passage precedent sans
        etre suspendue : le suivant doit alors venir vite.
        """
        maintenant = self._horloge()
        produit = False
        with self._verrou:
            for session in list(self._sessions.values()):
                if maintenant - session.dernier_acces > INACTIVITE:
                    self._arreter(session, RaisonFin.INACTIVE)
                    continue
                code = session.processus.poll()
                if code is not None and session.code_retour is None:
                    self._constater_fin(session, code)
                    if code != 0:
                        logger.warning("lecture : ffmpeg a echoue (%s) : %s", code, session.erreur)
            if cache:
                self._borner_cache()
            for session in list(self._sessions.values()):
                if session.processus.poll() is None:
                    self._reguler(session)
                    faits = session.produits()
                    produit = produit or (faits > session.vus and not session.suspendue)
                    session.vus = faits
        return produit

    def _taille(self) -> int:
        total = 0
        with contextlib.suppress(OSError):
            for fichier in self.racine.rglob("*"):
                with contextlib.suppress(OSError):
                    if fichier.is_file():
                        total += fichier.stat().st_size
        return total

    @staticmethod
    def _liberer(session: Session, seuil: int) -> None:
        """Efface les segments de la session d'indice inferieur a ``seuil``."""
        with contextlib.suppress(OSError):
            for entree in session.dossier.iterdir():
                if (m := _SEGMENT.fullmatch(entree.name)) and int(m.group(1)) < seuil:
                    entree.unlink(missing_ok=True)

    def _borner_cache(self) -> None:
        """Tient le plafond, derniere session comprise.

        Dans l'ordre : les segments lus il y a longtemps (au-dela de
        ``RETENUE``), puis tous les segments deja lus, puis les sessions les
        plus anciennes. Si la derniere deborde encore, ``_plein`` fait suspendre
        sa production (``_reguler``) jusqu'a ce que la lecture libere de la
        place. Avant, seule la duree de cache comptait : une session unique
        n'etait jamais freinee, et 272 Mo tenaient dans un plafond de 60.
        """
        taille = self._taille()
        if taille > self._cache_max:
            for retenue in (RETENUE, 0):
                for session in sorted(self._sessions.values(), key=lambda s: s.dernier_acces):
                    self._liberer(session, session.dernier_segment - retenue)
                taille = self._taille()
                if taille <= self._cache_max:
                    break
            while taille > self._cache_max and len(self._sessions) > 1:
                plus_vieille = min(self._sessions.values(), key=lambda s: s.dernier_acces)
                self._arreter(plus_vieille, RaisonFin.CACHE)
                taille = self._taille()
        if taille > self._cache_max:
            self._plein = True
        elif taille <= self._cache_max * REPRISE_CACHE:
            self._plein = False

    def _assurer_entretien(self) -> None:
        with self._verrou:
            if self._fil is not None and self._fil.is_alive():
                return
            self._arret.clear()
            self._fil = threading.Thread(target=self._boucle, name="sortilege-lecture", daemon=True)
            self._fil.start()

    def _boucle(self) -> None:
        """Tourne tant qu'il y a des sessions, s'eteint seul ensuite.

        Un vingtieme de seconde entre deux passages tant que ffmpeg produit,
        une demi-seconde sinon (``PAS_RAPIDE``) : un reemballage copie l'image
        a la vitesse du disque, et une demi-seconde sans frein suffisait a lui
        faire ecrire 150 Mo de segments. La taille du cache se mesure a chaque
        passage : mesuree toutes les dix secondes, elle laissait une session
        unique remplir le cache bien au-dela du plafond entre deux mesures. Le
        cout est faible, la regulation ne laissant que quelques dizaines de
        segments sur le disque.
        """
        pas = PAS_RAPIDE
        while True:
            self._reveil.wait(pas)
            self._reveil.clear()
            if self._arret.is_set():
                return
            try:
                pas = PAS_RAPIDE if self.entretien() else PAS_ENTRETIEN
            except Exception:
                logger.exception("entretien des sessions de lecture")
            with self._verrou:
                if not self._sessions:
                    self._fil = None
                    return


# --- Le controle de decodage --------------------------------------------------

POSITIONS_CONTROLE = (0.05, 0.25, 0.50, 0.75, 0.95)
ECHANTILLON = 4.0
DELAI_POSITION = 60.0
"""Quatre secondes d'un 4K HEVC 10 bits se decodent en quelques secondes sur un
NAS. Soixante, c'est qu'on ne peut plus rien conclure."""

ECART_DUREE_MAX = 2.0

# Messages que le decodeur emet juste apres un saut dans un fichier SAIN : un
# GOP ouvert (courant dans les remux de Blu-ray) reference des images d'avant
# le point de saut, que le decodeur n'a jamais vues. Les compter comme des
# erreurs ferait declarer abimes des fichiers intacts — l'erreur exacte que ce
# controle doit eviter. Verifie sur un H.264 sain en GOP ouvert, MKV et TS.
_BRUITS_DE_SAUT = re.compile(
    "|".join(
        [
            r"Missing reference picture",
            r"reference picture missing during reorder",
            r"mmco: unref short failure",
            r"co located POCs unavailable",
            r"Could not find ref with POC",
            r"non-existing PPS \d+ referenced",
            r"decode_slice_header error",
            r"no frame!",
            r"Last message repeated",
            r"non monotonically increasing dts",
            r"channel element \d+\.\d+ is not allocated",
            r"Header missing",
        ]
    )
)
BRUITS_TOLERES = 8
"""Un saut sain en produit deux ou trois. Au-dela, ce n'est plus un saut."""

_COUPURE = re.compile(r"Error submitting packet to decoder|Error while decoding stream")
"""Un paquet refuse en bloc par un decodeur (libelle de ffmpeg 7 et suivants, puis
celui des versions anterieures). Juste apres un saut, c'est le premier paquet
audio coupe en deux par le positionnement : un VOB sain (MPEG-2 + AC-3) en
produit un a CHAQUE saut, et le controle le declarait abime aux cinq points. Ce
n'est tolere qu'au point d'entree, et seulement apres verification (voir
``controler``) : le meme message plus loin dans la fenetre reste une erreur."""

_REPETE = re.compile(r"Last message repeated (\d+) times")

DEBUT_FENETRE = 0.5
"""Ce qu'on appelle « le point d'entree » : la premiere demi-seconde decodee."""

RECUL = 15.0
"""Une fenetre qui ne rend aucune image sans la moindre erreur est rejouee en
partant quinze secondes plus tot. Un MPEG-TS n'a pas d'index : ffmpeg s'y
positionne n'importe ou, et avec une image cle toutes les dix secondes (le
reglage par defaut de x264 a 25 i/s), la fenetre de 95 % d'un fichier court ne
contenait parfois aucune image decodable — un fichier sain declare abime.
Quinze secondes couvrent un groupe d'images de dix, plus l'imprecision du saut."""


class EtatControle(StrEnum):
    SAIN = "sain"
    """Valeur d'API historique : elle veut dire « aucune erreur aux points
    testes », jamais « intact ». Aucun texte montre ne dit « sain »."""

    ABIME = "abime"
    INDETERMINE = "indetermine"


def _points_testes() -> str:
    """« 5, 25, 50, 75 et 95 % » : ou le controle a regarde, et nulle part ailleurs."""
    valeurs = [str(round(f * 100)) for f in POSITIONS_CONTROLE]
    return (
        f"{', '.join(valeurs[:-1])} et {valeurs[-1]} %" if len(valeurs) > 1 else f"{valeurs[0]} %"
    )


LIBELLES_CONTROLE = {
    EtatControle.SAIN: f"aucune erreur aux {len(POSITIONS_CONTROLE)} points testés",
    EtatControle.ABIME: "abîmé",
    EtatControle.INDETERMINE: "indéterminé",
}
"""Le libelle court de chaque etat. « Sain » a disparu : cinq fenetres de quatre
secondes sans erreur ne prouvent pas qu'un fichier est intact — une corruption
legere d'un HEVC passe souvent sans le moindre message du decodeur."""


@dataclass(frozen=True, slots=True)
class Point:
    fraction: float
    secondes: float
    ok: bool | None
    """None : delai depasse, rien a conclure a cet endroit."""

    images: int | None = None
    erreurs: tuple[str, ...] = ()
    bruits: int = 0

    def en_dict(self) -> dict[str, Any]:
        return {
            "fraction": self.fraction,
            "secondes": self.secondes,
            "ok": self.ok,
            "images": self.images,
            "erreurs": list(self.erreurs),
            "bruits": self.bruits,
        }


@dataclass(frozen=True, slots=True)
class ResultatControle:
    etat: EtatControle
    message: str
    points: tuple[Point, ...] = ()
    position: float | None = None
    premiere_erreur: str = ""
    duree: float | None = None
    duree_originale: float | None = None
    cause: str = ""
    """« decodage », « duree » ou « delai » : ce qui a fait conclure. La fiche
    comparee s'en sert pour ne pas dire deux fois la meme chose — une duree
    trop courte n'y devient pas une « erreur de decodage »."""

    def en_dict(self) -> dict[str, Any]:
        return {
            "etat": str(self.etat),
            "libelle": LIBELLES_CONTROLE[self.etat],
            "message": self.message,
            "points": [p.en_dict() for p in self.points],
            "position": self.position,
            "premiere_erreur": self.premiere_erreur,
            "duree": self.duree,
            "duree_originale": self.duree_originale,
            "cause": self.cause,
        }


def commande_controle(
    ffmpeg: str,
    source: Path,
    secondes: float,
    *,
    duree: float = ECHANTILLON,
    ignorer: float = 0.0,
) -> list[str]:
    """Decode ``duree`` secondes a partir de ``secondes``, sans rien ecrire.

    Toutes les pistes audio sont decodees avec la video : un fichier abime
    l'est souvent partout, mais pas toujours. ``-progress`` donne le nombre
    d'images decodees — un fichier tronque ne produit pas d'erreur a la
    position qu'il n'atteint plus, il ne produit simplement RIEN.

    ``ignorer`` sert au rejeu parti plus tot : les images des premieres
    secondes sont decodees (il le faut, pour atteindre la fenetre) mais pas
    comptees, et le compte d'images reste celui de la fenetre elle-meme.
    """
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-ss",
        f"{max(0.0, secondes):.3f}",
        "-t",
        f"{duree:g}",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        # Borne aussi la sortie : la duree d'entree n'est qu'approximative dans
        # un MPEG-TS, et la fenetre comptee doit rester celle des autres points.
        *(["-ss", f"{ignorer:.3f}", "-t", f"{ECHANTILLON:g}"] if ignorer > 0 else []),
        "-f",
        "null",
        "-progress",
        "pipe:1",
        "-",
    ]


def _coupures(erreurs_brutes: str) -> int:
    """Paquets refuses en bloc, repetitions comprises.

    ffmpeg resume des lignes identiques en « Last message repeated 4 times » :
    sans compter ces repetitions, un paquet coupe au saut et quatre autres
    refuses plus loin sur le meme decodeur se confondraient avec le seul
    premier.
    """
    total = 0
    precedente = False
    for ligne in erreurs_brutes.splitlines():
        if _COUPURE.search(ligne):
            total += 1
            precedente = True
        elif m := _REPETE.search(ligne):
            if precedente:
                total += int(m.group(1))
        elif ligne.strip():
            precedente = False
    return total


def _manque_images(erreur: str) -> bool:
    return erreur == "aucune image décodée à cet endroit" or " images décodées sur " in erreur


def classer_sortie(
    erreurs_brutes: str,
    images: int | None,
    code: int,
    attendues: float | None,
    *,
    coupures_au_saut: bool = False,
) -> tuple[list[str], int]:
    """Separe les vraies erreurs des bruits de saut. Rend (erreurs, bruits).

    ``coupures_au_saut`` : les paquets refuses en bloc ont ete verifies comme
    TOUS situes au point d'entree ; ils comptent alors comme des bruits.
    """
    erreurs: list[str] = []
    bruits: list[str] = []
    for ligne in erreurs_brutes.splitlines():
        ligne = _nettoyer_ligne(ligne)
        if not ligne:
            continue
        bruit = _BRUITS_DE_SAUT.search(ligne) or (coupures_au_saut and _COUPURE.search(ligne))
        (bruits if bruit else erreurs).append(ligne)
    if len(bruits) > BRUITS_TOLERES:
        erreurs.append(bruits[0])
    if not erreurs and code != 0:
        erreurs.append(f"ffmpeg s'est arrêté en erreur (code {code})")
    if attendues is not None and attendues >= 1 and images is not None:
        if images == 0:
            erreurs.append("aucune image décodée à cet endroit")
        elif images < attendues * 0.5:
            erreurs.append(f"{images} images décodées sur environ {round(attendues)} attendues")
    return erreurs, len(bruits)


def _images(progression: str) -> int | None:
    valeurs = re.findall(r"^frame=(\d+)", progression, flags=re.MULTILINE)
    return int(valeurs[-1]) if valeurs else None


def controler(
    source: Path,
    *,
    analyse: Analyse | None = None,
    duree_originale: float | None = None,
    executer: Callable[..., Any] = subprocess.run,
    ffmpeg: str | None = None,
) -> ResultatControle:
    """Decode REELLEMENT le fichier a cinq endroits (5, 25, 50, 75, 95 %).

    C'est la seule facon de repondre a « Firefox dit corrompu » : le navigateur
    confond « je ne sais pas decoder » et « c'est casse ». ffmpeg, lui, decode
    ce qui existe. Le 95 % compte autant que le reste : c'est la fin qui trahit
    un fichier tronque.

    Ce que le resultat prouve, et rien de plus : une erreur trouvee est une
    erreur reelle ; AUCUNE erreur veut dire « aucune erreur dans ces cinq
    fenetres de quatre secondes ». Une corruption legere d'un HEVC passe souvent
    sans un seul message du decodeur, et tout ce qui est hors des fenetres
    n'est pas regarde. Les textes le disent ainsi, sans jamais ecrire
    « intact ».

    Deux precautions evitent un faux « abime » sur un fichier sain, chacune
    verifiee avant d'etre accordee (voir ``_examiner``) : le paquet coupe par le
    saut, et la fenetre sans image d'un MPEG-TS a images cles espacees.

    Pour un reencodage, la duree est comparee a celle de l'original : un
    encodage interrompu produit un fichier plus court qui se decode tres bien.
    """
    ffmpeg = ffmpeg or shutil.which("ffmpeg")
    if ffmpeg is None:
        return ResultatControle(
            EtatControle.INDETERMINE, "ffmpeg absent : impossible de décoder le fichier ici."
        )
    analyse = analyse if analyse is not None else sonder(source)
    duree = analyse.duree
    ips = analyse.images_par_seconde
    fractions = POSITIONS_CONTROLE if duree else (0.0,)

    points: list[Point] = []
    for fraction in fractions:
        secondes = (duree or 0.0) * fraction
        restant = (duree - secondes) if duree else None
        attendues = None
        if ips and restant is not None:
            attendues = ips * min(ECHANTILLON, max(0.0, restant))
        try:
            points.append(_examiner(executer, ffmpeg, source, fraction, secondes, attendues))
        except OSError as exc:
            return ResultatControle(
                EtatControle.INDETERMINE, f"ffmpeg ne se lance pas ({exc}).", tuple(points)
            )

    resultat = _conclure(points, duree, duree_originale)
    logger.info("controle de decodage : %s -> %s", source.name, resultat.etat)
    return resultat


def _decoder(executer: Callable[..., Any], argv: list[str], delai: float) -> Any | None:
    """Une passe de ffmpeg. None si elle depasse son delai ; OSError remonte."""
    try:
        return executer(argv, capture_output=True, text=True, timeout=delai, check=False)
    except subprocess.TimeoutExpired:
        return None


def _juger(
    executer: Callable[..., Any],
    ffmpeg: str,
    source: Path,
    depart: float,
    sortie: Any,
    attendues: float | None,
) -> tuple[list[str], int]:
    """Erreurs et bruits d'une passe, paquets coupes au saut ecartes s'ils le sont.

    Quand les SEULES erreurs sont des paquets refuses en bloc, on redecode la
    premiere demi-seconde depuis le MEME point d'entree : un saut est
    deterministe, le paquet qu'il coupe est le meme. Si cette demi-seconde en
    refuse autant que la fenetre entiere, tous etaient au point d'entree — des
    bruits de saut. S'il en manque un seul, il etait plus loin : c'est une
    vraie erreur, et le fichier est abime.
    """
    stderr = sortie.stderr or ""
    images = _images(sortie.stdout or "")
    erreurs, bruits = classer_sortie(stderr, images, sortie.returncode, attendues)
    lignes = [e for e in erreurs if not _manque_images(e)]
    if lignes and all(_COUPURE.search(e) for e in lignes):
        court = _decoder(
            executer,
            commande_controle(ffmpeg, source, depart, duree=DEBUT_FENETRE),
            DELAI_POSITION,
        )
        if court is not None and _coupures(court.stderr or "") >= _coupures(stderr):
            erreurs, bruits = classer_sortie(
                stderr, images, sortie.returncode, attendues, coupures_au_saut=True
            )
    return erreurs, bruits


def _examiner(
    executer: Callable[..., Any],
    ffmpeg: str,
    source: Path,
    fraction: float,
    secondes: float,
    attendues: float | None,
) -> Point:
    """Un point du controle : une fenetre, et au besoin son rejeu parti plus tot.

    Le rejeu n'a lieu que si la fenetre n'a rien rendu (ou trop peu) SANS la
    moindre erreur : un fichier tronque, lui, echoue aussi quinze secondes plus
    tot — il n'y a rien la non plus —, et reste abime.
    """
    sortie = _decoder(executer, commande_controle(ffmpeg, source, secondes), DELAI_POSITION)
    if sortie is None:
        return Point(fraction, secondes, None)
    images = _images(sortie.stdout or "")
    erreurs, bruits = _juger(executer, ffmpeg, source, secondes, sortie, attendues)
    recul = min(RECUL, secondes)
    if erreurs and recul > 0 and all(_manque_images(e) for e in erreurs):
        depart = secondes - recul
        rejeu = _decoder(
            executer,
            commande_controle(ffmpeg, source, depart, duree=ECHANTILLON + recul, ignorer=recul),
            DELAI_POSITION * 2,
        )
        if rejeu is None:
            return Point(fraction, secondes, None)
        images = _images(rejeu.stdout or "")
        erreurs, bruits = _juger(executer, ffmpeg, source, depart, rejeu, attendues)
    return Point(fraction, secondes, not erreurs, images, tuple(erreurs), bruits)


def _conclure(
    points: list[Point], duree: float | None, duree_originale: float | None
) -> ResultatControle:
    commun = {"points": tuple(points), "duree": duree, "duree_originale": duree_originale}
    abime = next((p for p in points if p.ok is False), None)
    if abime is not None:
        premiere = abime.erreurs[0] if abime.erreurs else ""
        if duree is None:
            return ResultatControle(
                EtatControle.ABIME,
                "Le fichier ne se décode pas dès le début : il est abîmé.",
                position=0.0,
                premiere_erreur=premiere,
                **commun,
            )
        return ResultatControle(
            EtatControle.ABIME,
            f"Erreur de décodage vers {round(abime.fraction * 100)} % : le fichier est abîmé.",
            position=abime.fraction,
            premiere_erreur=premiere,
            cause="decodage",
            **commun,
        )
    attente = next((p for p in points if p.ok is None), None)
    if attente is not None:
        return ResultatControle(
            EtatControle.INDETERMINE,
            f"Délai dépassé vers {round(attente.fraction * 100)} % : {ECHANTILLON:g} s de vidéo "
            f"non décodées en {DELAI_POSITION:g} s, impossible de conclure.",
            position=attente.fraction,
            cause="delai",
            **commun,
        )
    if duree is None:
        return ResultatControle(
            EtatControle.INDETERMINE,
            "Durée illisible : seul le début a pu être décodé, sans erreur.",
            **commun,
        )
    if duree_originale and abs(duree - duree_originale) > ECART_DUREE_MAX:
        return ResultatControle(
            EtatControle.ABIME,
            f"Aucune erreur de décodage aux {len(points)} points testés, mais durée de "
            f"{duree_lisible(duree)} contre {duree_lisible(duree_originale)} pour l'original : "
            "le fichier est incomplet.",
            cause="duree",
            **commun,
        )
    return ResultatControle(
        EtatControle.SAIN,
        f"Aucune erreur de décodage aux {len(points)} points testés ({_points_testes()}).",
        **commun,
    )


class Occupe(RuntimeError):
    """Un controle tourne deja, sur un autre fichier."""


class Controles:
    """Un seul controle a la fois pour tout le serveur, et le souvenir des resultats.

    Un controle decode cinq fois quatre secondes : sur un 4K, c'est une minute
    de processeur de NAS. Deux en parallele doubleraient la charge pour
    repondre deux fois moins vite. Le resultat est garde par empreinte du
    fichier : rouvrir l'ecran ne relance rien, un fichier modifie si.
    """

    def __init__(
        self,
        *,
        executer: Callable[..., ResultatControle] = controler,
        en_fond: bool = True,
        memoire: int = 64,
    ) -> None:
        self._executer = executer
        self._en_fond = en_fond
        self._memoire = memoire
        self._resultats: OrderedDict[str, ResultatControle] = OrderedDict()
        self._en_cours: str | None = None
        self._verrou = threading.Lock()

    def resultat(self, cle: str) -> ResultatControle | None:
        with self._verrou:
            return self._resultats.get(cle)

    def etat(self, cle: str) -> dict[str, Any]:
        with self._verrou:
            if (fait := self._resultats.get(cle)) is not None:
                return fait.en_dict()
            if self._en_cours == cle:
                return {
                    "etat": "en_cours",
                    "libelle": "en cours",
                    "message": "Décodage en cours à 5 endroits du fichier…",
                }
            return {"etat": "aucun", "libelle": "pas encore fait", "message": ""}

    def lancer(self, cle: str, source: Path, **options: Any) -> dict[str, Any]:
        """Demarre le controle de ce fichier, ou rend celui qui existe deja.

        Leve ``Occupe`` si un AUTRE fichier est en cours de controle : attendre
        en silence ferait croire a un controle lance, et la reponse viendrait
        une minute plus tard sans que rien ne l'annonce.
        """
        with self._verrou:
            if cle in self._resultats or self._en_cours == cle:
                demarrer = False
            elif self._en_cours is not None:
                raise Occupe()
            else:
                self._en_cours = cle
                demarrer = True
        if demarrer and self._en_fond:
            threading.Thread(
                target=self._executer_puis_retenir,
                args=(cle, source, options),
                name="sortilege-controle",
                daemon=True,
            ).start()
        elif demarrer:
            self._executer_puis_retenir(cle, source, options)
        return self.etat(cle)

    def _executer_puis_retenir(self, cle: str, source: Path, options: dict[str, Any]) -> None:
        try:
            resultat = self._executer(source, **options)
        except Exception as exc:
            logger.exception("controle de decodage")
            resultat = ResultatControle(
                EtatControle.INDETERMINE, f"Le contrôle a échoué ({type(exc).__name__})."
            )
        with self._verrou:
            self._resultats[cle] = resultat
            while len(self._resultats) > self._memoire:
                self._resultats.popitem(last=False)
            self._en_cours = None


# --- La fiche comparee d'un reencodage ----------------------------------------


class Gravite(StrEnum):
    GRAVE = "grave"
    ATTENTION = "attention"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class Verdict:
    code: str
    gravite: Gravite
    texte: str
    """La phrase complete, pour la fiche et la confirmation."""

    bref: str
    """Quelques mots, pour la phrase de conclusion."""

    def en_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "gravite": str(self.gravite),
            "texte": self.texte,
            "bref": self.bref,
        }


@dataclass(frozen=True, slots=True)
class Comparaison:
    verdicts: tuple[Verdict, ...]
    phrase: str
    gravite: str
    """grave, attention, ok, ou en_attente (rien a redire, controle pas encore fait)."""

    def en_dict(self) -> dict[str, Any]:
        return {
            "verdicts": [v.en_dict() for v in self.verdicts],
            "phrase": self.phrase,
            "gravite": self.gravite,
        }


def _manquantes(avant: list[str], apres: list[str]) -> str:
    """« fre, eng » : ce qui etait la et n'y est plus, par langue."""
    reste = Counter(apres)
    manque = []
    for langue in avant:
        if reste[langue] > 0:
            reste[langue] -= 1
        else:
            manque.append(langue or "langue non précisée")
    return ", ".join(manque)


def comparer(
    original: Analyse, reencode: Analyse, controle: ResultatControle | None = None
) -> Comparaison:
    """Ce qu'il faut savoir AVANT de remplacer l'original, verdict par verdict.

    Rien ici ne bloque : l'utilisateur decide. Mais chaque verdict grave passe
    dans la confirmation du remplacement, parce que c'est le seul geste de
    l'application dont les pertes ne se rattrapent pas.
    """
    verdicts: list[Verdict] = []

    if reencode.video_codec == "h264" and (reencode.profondeur or 8) > 8:
        if original.video_codec == "h264" and (original.profondeur or 8) > 8:
            # Le defaut etait deja la : remplacer ne rend le film ni plus ni
            # moins lisible. Le crier « grave » pousserait a garder un original
            # qui a exactement le meme probleme.
            verdicts.append(
                Verdict(
                    "h264_10_bits",
                    Gravite.INFO,
                    f"H.264 {reencode.profondeur} bits, comme l'original : peu de téléviseurs "
                    "le lisent, mais le réencodage n'y change rien.",
                    f"H.264 {reencode.profondeur} bits comme l'original",
                )
            )
        else:
            verdicts.append(
                Verdict(
                    "h264_10_bits",
                    Gravite.GRAVE,
                    f"Réencodé en H.264 {reencode.profondeur} bits : très peu de téléviseurs et "
                    "de clés de streaming le lisent. Ne remplace pas l'original.",
                    f"H.264 {reencode.profondeur} bits, illisible sur la plupart des téléviseurs",
                )
            )

    dv = original.dolby_vision
    if dv is not None and dv.profil == 5 and reencode.dolby_vision is None:
        verdicts.append(
            Verdict(
                "dolby_vision_5",
                Gravite.GRAVE,
                f"Source {dv.libelle} réencodée sans Dolby Vision : {dv.explication}. "
                "Ne remplace pas l'original.",
                "Dolby Vision 5 perdu, couleurs faussées",
            )
        )
    elif original.est_hdr and not reencode.est_hdr:
        verdicts.append(
            Verdict(
                "hdr_perdu",
                Gravite.GRAVE,
                f"Source {original.libelle_hdr} devenue SDR : couleurs délavées probables.",
                "HDR perdu",
            )
        )
    elif original.est_hdr and reencode.est_hdr:
        if dv is not None and reencode.dolby_vision is None:
            verdicts.append(
                Verdict(
                    "dolby_vision_perdu",
                    Gravite.INFO,
                    f"{dv.libelle} perdu, {reencode.libelle_hdr} conservé : un téléviseur "
                    "Dolby Vision affichera la couche de base.",
                    "Dolby Vision perdu",
                )
            )
        if original.hdr10plus and not reencode.hdr10plus:
            verdicts.append(
                Verdict(
                    "hdr10plus_perdu",
                    Gravite.INFO,
                    "HDR10+ perdu, HDR10 conservé : les réglages scène par scène ne suivent pas.",
                    "HDR10+ perdu",
                )
            )
        if original.mastering is not None and reencode.mastering is None:
            verdicts.append(
                Verdict(
                    "metadonnees_hdr",
                    Gravite.ATTENTION,
                    "Métadonnées HDR10 (luminance de mastering) perdues : certains téléviseurs "
                    "doseront mal la luminosité.",
                    "métadonnées HDR10 perdues",
                )
            )

    if original.duree and reencode.duree:
        ecart = abs(original.duree - reencode.duree)
        if ecart > ECART_DUREE_MAX:
            verdicts.append(
                Verdict(
                    "duree",
                    Gravite.GRAVE,
                    f"Durée différente de l'original : {duree_lisible(reencode.duree)} contre "
                    f"{duree_lisible(original.duree)} (écart de {duree_lisible(ecart)}).",
                    f"durée différente de {duree_lisible(ecart)}",
                )
            )

    if len(reencode.audio) < len(original.audio):
        manque = _manquantes([p.langue for p in original.audio], [p.langue for p in reencode.audio])
        verdicts.append(
            Verdict(
                "audio_perdu",
                Gravite.GRAVE,
                f"Piste audio perdue : {len(original.audio)} dans l'original, "
                f"{len(reencode.audio)} ici" + (f" (manque {manque})." if manque else "."),
                "piste audio perdue",
            )
        )
    if len(reencode.sous_titres) < len(original.sous_titres):
        manque = _manquantes(
            [p.langue for p in original.sous_titres], [p.langue for p in reencode.sous_titres]
        )
        verdicts.append(
            Verdict(
                "sous_titres_perdus",
                Gravite.GRAVE,
                f"Piste de sous-titres perdue : {len(original.sous_titres)} dans l'original, "
                f"{len(reencode.sous_titres)} ici" + (f" (manque {manque})." if manque else "."),
                "sous-titres perdus",
            )
        )

    deja = {v.code for v in verdicts}
    if controle is not None and controle.etat is EtatControle.ABIME:
        if controle.cause == "duree":
            # Le controle a conclu sur la duree, pas sur une image illisible :
            # ce n'est pas une « erreur de decodage », et si la fiche a deja
            # compare les durees, le redire ferait deux lignes pour un defaut.
            if "duree" not in deja:
                verdicts.append(
                    Verdict("duree", Gravite.GRAVE, controle.message, "durée incomplète")
                )
        else:
            verdicts.append(
                Verdict(
                    "decodage",
                    Gravite.GRAVE,
                    controle.message,
                    "erreur de décodage"
                    + (
                        f" vers {round(controle.position * 100)} %"
                        if controle.position is not None
                        else ""
                    ),
                )
            )
    elif controle is not None and controle.etat is EtatControle.INDETERMINE:
        verdicts.append(
            Verdict(
                "decodage_indetermine",
                Gravite.ATTENTION,
                f"Contrôle de décodage non concluant : {controle.message}",
                "décodage non vérifié",
            )
        )

    graves = [v for v in verdicts if v.gravite is Gravite.GRAVE]
    reserves = [v for v in verdicts if v.gravite is Gravite.ATTENTION]
    if graves:
        phrase = "Ne remplace pas l'original en l'état : " + ", ".join(v.bref for v in graves) + "."
        gravite = "grave"
    elif reserves:
        phrase = "Remplaçable, avec une réserve : " + ", ".join(v.bref for v in reserves) + "."
        gravite = "attention"
    elif controle is not None and controle.etat is EtatControle.SAIN:
        # Jamais « intact » : le controle n'a regarde que cinq fenetres.
        phrase = (
            "Rien à signaler dans ce qui a été vérifié : même durée, mêmes pistes, aucune "
            f"erreur de décodage aux {len(POSITIONS_CONTROLE)} points testés ({_points_testes()})."
        )
        gravite = "ok"
    else:
        phrase = (
            "Rien à signaler dans les métadonnées ; le contrôle de décodage n'a pas encore "
            "rendu son résultat."
        )
        gravite = "en_attente"
    return Comparaison(tuple(verdicts), phrase, gravite)
