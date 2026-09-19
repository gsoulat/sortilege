"""Test de fumee du ffmpeg installe : le reencodage et le lecteur, de bout en bout.

Pourquoi ce script existe. Le reencodage HEVC 10 bits (HDR conserve, Dolby
Vision, desentrelacement, couvertures, sous-titres mov_text, audio E-AC-3) et
le lecteur HLS (reemballage, conversion d'apercu, controle de decodage) passent
a ffmpeg des options qui n'existent pas dans toutes ses versions :
``-dolbyvision``, ``-x265-params`` avec ``master-display``, ``max-cll`` et
``vbv-*``, ``setparams``, ``bwdif``, ``-attach``, ``0:V``,
``-hls_segment_options``, ``-fps_mode``, ``-output_ts_offset``,
``movflags=+frag_discont``, ``zscale`` et ``tonemap``... Les tests unitaires
verifient les commandes construites, jamais le ffmpeg qui les recoit. Or sur le
NAS, c'est le ffmpeg de l'image Docker (celui de Debian) qui tourne : une
option qu'il ne connait pas ferait echouer CHAQUE reencodage ou CHAQUE lecture,
tous les tests au vert.

Le script fabrique de vraies sources courtes (``ffmpeg -f lavfi``), les fait
passer par le VRAI chemin de l'application (``transcode.preparer`` puis
``transcode.run`` ; ``lecture.analyser``, ``lecture.decider``,
``lecture.Sessions``, ``lecture.controler``) et verifie chaque resultat avec
ffprobe. Il s'arrete au premier echec, en citant la commande et ce que ffmpeg a
ecrit sur sa sortie d'erreur.

    python -m sortilege.outils.verifier_ffmpeg            # poste de developpement
    python -m sortilege.outils.verifier_ffmpeg --strict   # image Docker (CI)

``--strict`` exige aussi ``zscale`` et ``tonemap`` : sans eux, l'apercu d'un
film HDR sort delave. L'image Docker doit les avoir ; le ffmpeg de Homebrew ne
les a pas (compile sans zimg), et ce n'est pas une raison d'echouer sur un Mac.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import logging
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sortilege.core import lecture, transcode

DELAI = 180.0
"""Delai de chaque commande auxiliaire (fabrication, sonde, attente d'une
session). Tout se fait en quelques secondes : au-dela, quelque chose est bloque,
et mieux vaut le dire que laisser la CI attendre son propre delai."""

DUREE = 3
"""Duree des sources a reencoder : assez pour un vrai encodage, assez peu pour
que l'ensemble tienne en moins de deux minutes sur un runner de CI."""

DUREE_LONGUE = 14
"""Duree des sources du lecteur : trois segments HLS de six secondes (6 + 6 + 2).
Une source de trois secondes n'en donnerait qu'un, et le decoupage ne serait
pas verifie."""

PRESET = transcode.PRESETS[0]
"""« veryfast ». Le preset ne change aucune option passee a ffmpeg, seulement le
temps de calcul : celui de la nuit (« medium ») triplerait la duree du script
sans rien verifier de plus."""

MASTER_DISPLAY = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
"""Mastering d'un disque UHD courant (primaires BT.2020, point blanc D65,
1000 cd/m², 0,005 cd/m²), a l'unite pres de x265."""

MAX_CLL = "1000,400"
SOUS_TITRE = "Sortilège vérifie ffmpeg"
POLICE = b"police factice : le Matroska ne lit pas ce qu'il transporte"

_CLES_MASTERING = (
    "red_x",
    "red_y",
    "green_x",
    "green_y",
    "blue_x",
    "blue_y",
    "white_point_x",
    "white_point_y",
    "min_luminance",
    "max_luminance",
)


# --- Le tableau -----------------------------------------------------------------


class Echec(Exception):
    """Premiere verification ratee : le script s'arrete, avec ce que ffmpeg a dit."""

    def __init__(self, verification: str, detail: str = "") -> None:
        super().__init__(verification)
        self.detail = detail


@dataclass
class Tableau:
    """Une ligne par verification, imprimee aussitot.

    Au fil de l'eau plutot qu'a la fin : un runner de CI qui coupe le script a
    mi-chemin laisse au moins voir ou il en etait.
    """

    faites: int = 0
    debut: float = field(default_factory=time.monotonic)

    @property
    def ecoule(self) -> float:
        return time.monotonic() - self.debut

    @staticmethod
    def section(titre: str) -> None:
        print(f"\n== {titre}", flush=True)

    def verifier(self, condition: bool, nom: str, info: str = "", detail: str = "") -> None:
        """OK et on continue, ou ECHEC et on s'arrete la."""
        if not condition:
            self._ligne("ÉCHEC", nom, info)
            raise Echec(nom, detail)
        self.faites += 1
        self._ligne("OK", nom, info)

    def noter(self, nom: str, info: str) -> None:
        """Ce qui n'est pas verifie mais merite d'etre lu (un filtre facultatif absent)."""
        self._ligne("--", nom, info)

    @staticmethod
    def _ligne(etat: str, nom: str, info: str) -> None:
        print(f"  {etat:<6} {nom:<62} {info}".rstrip(), flush=True)


# --- ffmpeg et ffprobe ----------------------------------------------------------


def _executer(argv: list[str], delai: float = DELAI) -> subprocess.CompletedProcess[str]:
    """Lance une commande auxiliaire. Un delai depasse devient un echec qui cite la commande."""
    try:
        return subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            argv, capture_output=True, text=True, timeout=delai, check=False
        )
    except subprocess.TimeoutExpired:
        raise Echec(f"délai de {delai:.0f} s dépassé", _citer(argv, "")) from None


def _citer(argv: list[str] | None, erreurs: str, *, lignes: int = 40) -> str:
    """La commande, copiable telle quelle, puis la fin de ce que ffmpeg a ecrit."""
    morceaux = []
    if argv:
        morceaux.append("commande : " + shlex.join(argv))
    texte = [ligne for ligne in (erreurs or "").strip().splitlines() if ligne.strip()]
    if len(texte) > lignes:
        texte = ["[...]", *texte[-lignes:]]
    if texte:
        morceaux.append(
            "sortie d'erreur de ffmpeg :\n" + "\n".join(f"  {ligne}" for ligne in texte)
        )
    else:
        morceaux.append("ffmpeg n'a rien écrit sur sa sortie d'erreur.")
    return "\n".join(morceaux)


@dataclass(frozen=True)
class Outils:
    ffmpeg: str
    ffprobe: str

    def fabriquer(self, sortie: Path, *arguments: str) -> tuple[bool, str]:
        """Ecrit ``sortie`` avec ffmpeg. Rend (reussi, detail a citer en cas d'echec)."""
        argv = [self.ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
        argv += [*arguments, str(sortie)]
        resultat = _executer(argv)
        reussi = resultat.returncode == 0 and sortie.is_file() and sortie.stat().st_size > 0
        return reussi, _citer(argv, resultat.stderr)

    def sonder(self, chemin: Path) -> dict[str, Any]:
        argv = [self.ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json"]
        argv.append(str(chemin))
        resultat = _executer(argv)
        if resultat.returncode != 0:
            raise Echec(f"ffprobe ne lit pas {chemin.name}", _citer(argv, resultat.stderr))
        return json.loads(resultat.stdout or "{}")

    def images(self, chemin: Path, nombre: int = 1) -> list[dict[str, Any]]:
        """Les premieres images de la video principale : la que vivent le HDR et les trames."""
        argv = [
            self.ffprobe,
            "-v",
            "error",
            "-read_intervals",
            f"%+#{nombre}",
            "-select_streams",
            "V:0",
            "-show_frames",
            "-show_entries",
            "frame=pix_fmt,color_transfer,color_primaries,color_space,interlaced_frame,"
            "side_data_list",
            "-of",
            "json",
            str(chemin),
        ]
        resultat = _executer(argv)
        if resultat.returncode != 0:
            raise Echec(f"ffprobe ne décode pas {chemin.name}", _citer(argv, resultat.stderr))
        return json.loads(resultat.stdout or "{}").get("frames") or []

    def decoder(self, chemin: Path) -> tuple[bool, str]:
        """Decode tout le fichier, sans rien ecrire. Rend (sans erreur, detail)."""
        argv = [self.ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-i", str(chemin)]
        argv += ["-map", "0", "-f", "null", "-"]
        resultat = _executer(argv)
        return resultat.returncode == 0 and not resultat.stderr.strip(), _citer(
            argv, resultat.stderr
        )


def _flux(sonde: dict[str, Any], genre: str) -> list[dict[str, Any]]:
    return [f for f in sonde.get("streams") or [] if f.get("codec_type") == genre]


def _video(sonde: dict[str, Any]) -> dict[str, Any]:
    """La video principale : la premiere piste video qui n'est pas une couverture."""
    return next(
        (f for f in _flux(sonde, "video") if not (f.get("disposition") or {}).get("attached_pic")),
        {},
    )


def _couvertures(sonde: dict[str, Any]) -> list[dict[str, Any]]:
    return [f for f in _flux(sonde, "video") if (f.get("disposition") or {}).get("attached_pic")]


def _etiquettes(flux: dict[str, Any]) -> dict[str, str]:
    return {str(cle).lower(): str(valeur) for cle, valeur in (flux.get("tags") or {}).items()}


def _duree(sonde: dict[str, Any]) -> float:
    try:
        return float((sonde.get("format") or {}).get("duration") or 0.0)
    except ValueError:
        return 0.0


def _fraction(valeur: object) -> float | None:
    texte = str(valeur)
    try:
        if "/" in texte:
            numerateur, denominateur = texte.split("/", 1)
            return float(numerateur) / float(denominateur)
        return float(texte)
    except (ValueError, ZeroDivisionError):
        return None


def _annexe(image: dict[str, Any], genre: str) -> dict[str, Any] | None:
    return next(
        (d for d in image.get("side_data_list") or [] if d.get("side_data_type") == genre), None
    )


def _mastering(image: dict[str, Any]) -> tuple[float | None, ...] | None:
    donnees = _annexe(image, "Mastering display metadata")
    if donnees is None:
        return None
    return tuple(_fraction(donnees.get(cle)) for cle in _CLES_MASTERING)


def _lumiere(image: dict[str, Any]) -> tuple[object, object] | None:
    donnees = _annexe(image, "Content light level metadata")
    if donnees is None:
        return None
    return donnees.get("max_content"), donnees.get("max_average")


# --- Ce que ``transcode.run`` efface ------------------------------------------------


@dataclass
class Trace:
    """La commande d'un encodage, et ce que ffmpeg a ecrit sur sa sortie d'erreur."""

    argv: list[str] = field(default_factory=list)
    erreurs: str = ""


_TRACES: dict[str, Trace] = {}


@contextlib.contextmanager
def _traces_conservees() -> Iterator[None]:
    """Garde l'argv et la sortie d'erreur de chaque encodage, que ``run`` efface.

    ``run`` supprime le journal de ffmpeg des l'encodage fini et n'en garde que
    la ligne de la cause (``job.error``). Pour un test de fumee, la commande
    complete et le journal entier valent bien davantage : on les recopie au
    passage, sans rien changer a ce que ``run`` fait ni a ce qu'il lance.
    """
    original = getattr(transcode, "_executer", None)
    if original is None:  # renomme depuis : on se contente de job.error
        yield
        return

    def espion(job: transcode.Job, commande: list[str], journal: Path) -> tuple[int, str]:
        code, erreur = original(job, commande, journal)
        texte = ""
        with contextlib.suppress(OSError):
            texte = journal.read_text(encoding="utf-8", errors="replace")
        _TRACES[job.id] = Trace(list(commande), texte)
        return code, erreur

    transcode._executer = espion
    try:
        yield
    finally:
        transcode._executer = original


def _detail_travail(job: transcode.Job) -> str:
    trace = _TRACES.get(job.id)
    lignes = [
        f"état : {job.state}",
        f"erreur retenue par l'application : {job.error or '(aucune)'}",
    ]
    lignes += [f"note : {note}" for note in job.notes]
    if trace is not None:
        lignes.append(_citer(trace.argv, trace.erreurs))
    return "\n".join(lignes)


# --- 1. Le ffmpeg installe ----------------------------------------------------------


ENCODEURS = {
    "libx265": "réencodage HEVC 10 bits",
    "libx264": "conversion de l'aperçu",
    "aac": "audio de l'aperçu",
    "eac3": "compression de l'audio sans perte",
    "srt": "sous-titres mov_text convertis en SRT",
}

FILTRES = {
    "bwdif": "désentrelacement",
    "setfield": "étiquette « progressif »",
    "setparams": "couleurs déclarées en entrée",
    "scale": "réduction de taille",
}

FILTRES_HDR = {
    "zscale": "aperçu HDR ramené en SDR",
    "tonemap": "aperçu HDR ramené en SDR",
}


def verifier_installation(t: Tableau, outils: Outils, *, strict: bool) -> frozenset[str]:
    version = _executer([outils.ffmpeg, "-hide_banner", "-version"]).stdout.splitlines()
    premiere = version[0] if version else ""
    numero = m.group(1) if (m := re.match(r"ffmpeg version (\S+)", premiere)) else premiere
    t.verifier(bool(premiere), "ffmpeg", f"{numero} ({outils.ffmpeg})")

    encodeurs = transcode.encodeurs_ffmpeg(outils.ffmpeg)
    t.verifier(encodeurs is not None, "liste des encodeurs lisible par l'application")
    for nom, role in ENCODEURS.items():
        t.verifier(nom in (encodeurs or ()), f"encodeur {nom}", role)

    filtres = lecture.filtres_ffmpeg()
    t.verifier(bool(filtres), "liste des filtres lisible par l'application")
    for nom, role in FILTRES.items():
        t.verifier(nom in filtres, f"filtre {nom}", role)
    for nom, role in FILTRES_HDR.items():
        if strict or nom in filtres:
            t.verifier(nom in filtres, f"filtre {nom}", role)
        else:
            t.noter(f"filtre {nom}", "absent : aperçu HDR délavé (toléré sans --strict)")

    aide = _executer([outils.ffmpeg, "-hide_banner", "-h", "encoder=libx265"]).stdout
    t.verifier(
        re.search(r"^\s*-dolbyvision\b", aide, flags=re.MULTILINE) is not None,
        "option -dolbyvision de libx265",
        "passée à 0 pour toute source Dolby Vision",
        detail=aide,
    )
    return filtres


# --- 2. Les sources -----------------------------------------------------------------


def _mire(taille: str, cadence: int, duree: int) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={taille}:rate={cadence}:duration={duree}"]


def _son(duree: int, suite: str = "") -> list[str]:
    return ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={duree}{suite}"]


def _hdr10(image_cle: int) -> list[str]:
    """HEVC 10 bits HDR10 comme un disque UHD : PQ, BT.2020, mastering et MaxCLL dans le flux."""
    return [
        "-vf",
        "setparams=color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc,"
        "format=yuv420p10le",
        "-c:v",
        "libx265",
        "-preset",
        "ultrafast",
        "-x265-params",
        "hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc:"
        f"master-display={MASTER_DISPLAY}:max-cll={MAX_CLL}:"
        f"keyint={image_cle}:min-keyint={image_cle}:log-level=error",
    ]


H264 = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]


@dataclass(frozen=True)
class Source:
    cle: str
    libelle: str
    fichier: str
    arguments: tuple[str, ...]


def _sources(materiaux: Path) -> list[Source]:
    srt = str(materiaux / "sous-titres.srt")
    couverture = str(materiaux / "cover.jpg")
    police = str(materiaux / "police.ttf")
    return [
        Source(
            "a",
            "SDR H.264 8 bits 1080p, AAC, SRT (MKV)",
            "a-sdr-1080p.mkv",
            (
                *_mire("1920x1080", 25, DUREE),
                *_son(DUREE),
                "-i",
                srt,
                *("-map", "0:v", "-map", "1:a", "-map", "2:s"),
                *H264,
                *("-g", "50", "-c:a", "aac", "-b:a", "128k", "-c:s", "srt"),
                *("-metadata:s:a:0", "language=fre", "-metadata:s:s:0", "language=fre"),
            ),
        ),
        Source(
            "b",
            "HDR10 HEVC 10 bits 3840x1600 (MKV)",
            "b-hdr10-2160p.mkv",
            (
                *_mire("3840x1600", 24, DUREE),
                *_son(DUREE),
                *("-map", "0:v", "-map", "1:a"),
                *_hdr10(48),
                *("-c:a", "aac", "-b:a", "128k"),
            ),
        ),
        Source(
            "c",
            "1080i entrelacé H.264, AC-3 (MPEG-TS)",
            "c-1080i.ts",
            (
                *_mire("1920x1080", 25, DUREE),
                *_son(DUREE),
                *("-map", "0:v", "-map", "1:a", "-vf", "setfield=tff"),
                *H264,
                *("-flags", "+ildct+ilme", "-c:a", "ac3", "-f", "mpegts"),
            ),
        ),
        Source(
            "d",
            "couverture jointe et police jointe (MKV)",
            "d-jointes-720p.mkv",
            (
                *_mire("1280x720", 25, DUREE),
                *_son(DUREE),
                *("-map", "0:v", "-map", "1:a"),
                *H264,
                *("-c:a", "aac"),
                # Jointe comme le fait mkvmerge : une piece jointe image/jpeg, que
                # le demultiplexeur presente comme une piste video attached_pic.
                *("-attach", couverture, "-metadata:s:t:0", "mimetype=image/jpeg"),
                *("-metadata:s:t:0", "filename=cover.jpg"),
                *("-attach", police, "-metadata:s:t:1", "mimetype=application/x-truetype-font"),
                *("-metadata:s:t:1", "filename=police.ttf"),
            ),
        ),
        Source(
            "e",
            "sous-titres mov_text (MP4)",
            "e-mov-text-720p.mp4",
            (
                *_mire("1280x720", 25, DUREE),
                *_son(DUREE),
                "-i",
                srt,
                *("-map", "0:v", "-map", "1:a", "-map", "2:s"),
                *H264,
                *("-c:a", "aac", "-c:s", "mov_text", "-metadata:s:s:0", "language=fre"),
            ),
        ),
        Source(
            "f",
            "PCM 7.1 24 bits (MKV)",
            "f-pcm71-720p.mkv",
            (
                *_mire("1280x720", 25, DUREE),
                *_son(DUREE, ",pan=7.1|FL=c0|FR=c0|FC=c0|LFE=c0|BL=c0|BR=c0|SL=c0|SR=c0"),
                *("-map", "0:v", "-map", "1:a"),
                *H264,
                *("-c:a", "pcm_s24le"),
            ),
        ),
        Source(
            "h",
            "lecteur : H.264 720p, AAC, 14 s (MKV)",
            "h-lecteur-remux.mkv",
            (
                *_mire("1280x720", 25, DUREE_LONGUE),
                *_son(DUREE_LONGUE),
                *("-map", "0:v", "-map", "1:a"),
                *H264,
                # Une image cle toutes les deux secondes, comme une release
                # ordinaire : c'est la que la copie peut couper un segment.
                *("-g", "50", "-c:a", "aac"),
            ),
        ),
        Source(
            "i",
            "lecteur : HDR10 HEVC 10 bits 1280x536, 14 s (MKV)",
            "i-lecteur-hdr.mkv",
            (
                *_mire("1280x536", 24, DUREE_LONGUE),
                *_son(DUREE_LONGUE),
                *("-map", "0:v", "-map", "1:a"),
                *_hdr10(48),
                *("-c:a", "aac"),
            ),
        ),
    ]


def fabriquer_sources(t: Tableau, outils: Outils, dossier: Path) -> dict[str, Path]:
    materiaux = dossier / "materiaux"
    films = dossier / "bibliotheque" / "Films"
    materiaux.mkdir(parents=True)
    films.mkdir(parents=True)
    (materiaux / "sous-titres.srt").write_text(
        f"1\n00:00:00,500 --> 00:00:02,500\n{SOUS_TITRE}\n", encoding="utf-8"
    )
    (materiaux / "police.ttf").write_bytes(POLICE)
    reussi, detail = outils.fabriquer(
        materiaux / "cover.jpg", "-f", "lavfi", "-i", "testsrc2=size=320x480", "-frames:v", "1"
    )
    t.verifier(reussi, "couverture JPEG", "320x480", detail)

    chemins: dict[str, Path] = {}
    for source in _sources(materiaux):
        chemin = films / source.fichier
        debut = time.monotonic()
        reussi, detail = outils.fabriquer(chemin, *source.arguments)
        info = ""
        if reussi:
            info = (
                f"{_duree(outils.sonder(chemin)):.1f} s, {chemin.stat().st_size // 1024} Ko, "
                f"fabriquée en {time.monotonic() - debut:.1f} s"
            )
        t.verifier(reussi, f"source ({source.cle}) {source.libelle}", info, detail)
        chemins[source.cle] = chemin

    # Les sources doivent etre ce qu'elles pretendent : sans trames, le
    # desentrelacement ne serait pas verifie ; sans couverture, -attach non plus.
    sonde_c = outils.sonder(chemins["c"])
    ordre = _video(sonde_c).get("field_order")
    t.verifier(ordre in ("tt", "tb", "bt", "bb"), "source (c) marquée entrelacée", f"{ordre}")
    sonde_d = outils.sonder(chemins["d"])
    t.verifier(
        len(_couvertures(sonde_d)) == 1 and len(_flux(sonde_d, "attachment")) == 1,
        "source (d) : une couverture attached_pic, une police",
    )
    return chemins


# --- 3. Le reencodage -----------------------------------------------------------------


def _reencoder(
    t: Tableau,
    bibliotheque: Path,
    nom: str,
    source: Path,
    cible: str,
    *,
    compresser_audio: bool = False,
    preparation: transcode.Preparation | None = None,
) -> transcode.Job:
    """Le chemin de la file de nuit : ``preparer``, puis ``run``, et rien d'autre."""
    preparation = preparation or transcode.preparer(source)
    job = transcode.new_job(
        source,
        source.relative_to(bibliotheque).as_posix(),
        source.stem,
        cible,
        source.stat().st_size,
    )
    debut = time.monotonic()
    transcode.run(
        job,
        bibliotheque,
        preset=PRESET,
        compresser_audio=compresser_audio,
        preparation=preparation,
    )
    t.verifier(
        job.state is transcode.State.DONE and job.output is not None and job.output.is_file(),
        f"{nom} : réencodage terminé",
        f"{job.state}, {time.monotonic() - debut:.1f} s, {job.output_bytes // 1024} Ko",
        _detail_travail(job),
    )
    return job


def _verifier_hevc(
    t: Tableau, outils: Outils, nom: str, job: transcode.Job, largeur: int, hauteur: int
) -> dict[str, Any]:
    """Ce que tout reencodage doit produire : HEVC Main 10, 4:2:0 10 bits, la bonne taille."""
    if job.output is None:  # garanti par _reencoder, garde pour le typage
        raise Echec(f"{nom} : aucun fichier produit", _detail_travail(job))
    sonde = outils.sonder(job.output)
    video = _video(sonde)
    codec, profil, pixels = video.get("codec_name"), video.get("profile"), video.get("pix_fmt")
    t.verifier(
        (codec, profil, pixels) == ("hevc", "Main 10", "yuv420p10le"),
        f"{nom} : HEVC Main 10, yuv420p10le",
        f"{codec}, {profil}, {pixels}",
    )
    dimensions = (video.get("width"), video.get("height"))
    t.verifier(
        dimensions == (largeur, hauteur),
        f"{nom} : {largeur}x{hauteur}",
        f"{dimensions[0]}x{dimensions[1]}",
    )
    duree = _duree(sonde)
    ecart = abs(duree - job.source_duration)
    t.verifier(
        job.source_duration > 0 and ecart <= max(0.1, job.source_duration * 0.02),
        f"{nom} : durée égale à la source",
        f"{duree:.2f} s pour {job.source_duration:.2f} s",
    )
    return sonde


def verifier_reencodage(
    t: Tableau, outils: Outils, dossier: Path, sources: dict[str, Path]
) -> transcode.Job:
    """Chaque cas de la file de nuit. Rend le reencodage SDR, que le lecteur relit."""
    bibliotheque = dossier / "bibliotheque"

    # (a) SDR 1080p : couleurs declarees par setparams, pistes copiees.
    nom = "(a) SDR 1080p"
    job_a = _reencoder(t, bibliotheque, nom, sources["a"], "1080p")
    sonde = _verifier_hevc(t, outils, nom, job_a, 1920, 1080)
    video = _video(sonde)
    couleurs = tuple(video.get(c) for c in ("color_primaries", "color_transfer", "color_space"))
    t.verifier(couleurs == ("bt709",) * 3, f"{nom} : BT.709 déclaré", "/".join(map(str, couleurs)))
    audio, sous_titres = _flux(sonde, "audio"), _flux(sonde, "subtitle")
    t.verifier(
        [(f.get("codec_name"), _etiquettes(f).get("language")) for f in audio] == [("aac", "fre")],
        f"{nom} : audio AAC copié, langue gardée",
        ", ".join(f"{f.get('codec_name')} {_etiquettes(f).get('language')}" for f in audio),
    )
    t.verifier(
        [(f.get("codec_name"), _etiquettes(f).get("language")) for f in sous_titres]
        == [("subrip", "fre")],
        f"{nom} : sous-titres SRT copiés",
        ", ".join(f"{f.get('codec_name')} {_etiquettes(f).get('language')}" for f in sous_titres),
    )

    # (b) HDR10 4K -> 1080p : 1920x800, et le HDR conserve jusqu'aux metadonnees.
    nom = "(b) HDR10 4K -> 1080p"
    preparation_b = transcode.preparer(sources["b"])
    plan = transcode.planifier(preparation_b.analyse, encodeurs=preparation_b.encodeurs)
    t.verifier(
        plan.hdr and plan.master_display == MASTER_DISPLAY and plan.max_cll == MAX_CLL,
        f"{nom} : plan, mastering et MaxCLL relus",
        f"master-display={plan.master_display} max-cll={plan.max_cll}",
        detail=f"refus : {plan.refus}\nnotes : {plan.notes}",
    )
    job_b = _reencoder(t, bibliotheque, nom, sources["b"], "1080p", preparation=preparation_b)
    sonde = _verifier_hevc(t, outils, nom, job_b, 1920, 800)
    video = _video(sonde)
    couleurs = tuple(video.get(c) for c in ("color_primaries", "color_transfer", "color_space"))
    t.verifier(
        couleurs == ("bt2020", "smpte2084", "bt2020nc"),
        f"{nom} : PQ BT.2020 déclaré",
        "/".join(map(str, couleurs)),
    )
    avant = (outils.images(sources["b"]) or [{}])[0]
    apres = (outils.images(job_b.output) or [{}])[0] if job_b.output else {}
    t.verifier(
        apres.get("color_transfer") == "smpte2084"
        and _mastering(apres) is not None
        and _mastering(apres) == _mastering(avant),
        f"{nom} : mastering display identique",
        "présent dans le flux, valeurs de la source" if _mastering(apres) else "absent",
        detail=f"source : {_mastering(avant)}\nsortie : {_mastering(apres)}",
    )
    t.verifier(
        _lumiere(apres) == (1000, 400),
        f"{nom} : MaxCLL/MaxFALL identiques",
        f"{_lumiere(apres)}",
    )

    # (b') Dolby Vision : aucune source Dolby Vision ne se fabrique avec lavfi.
    # L'analyse de (b) est donc presentee comme un profil 8.1 (base HDR10) :
    # c'est ``run`` lui-meme qui en deduit ``-dolbyvision 0``.
    nom = "(b') Dolby Vision 8.1 simulé"
    analyse_dv = dataclasses.replace(
        preparation_b.analyse, dolby_vision=lecture.DolbyVision(profil=8, compatibilite=1)
    )
    preparation_dv = dataclasses.replace(preparation_b, analyse=analyse_dv)
    job_dv = _reencoder(t, bibliotheque, nom, sources["b"], "720p", preparation=preparation_dv)
    trace = _TRACES.get(job_dv.id)
    if trace is not None:
        passe = "-dolbyvision" in trace.argv
        t.verifier(passe, f"{nom} : -dolbyvision 0 passé à ffmpeg", "accepté par libx265")
    sonde = outils.sonder(job_dv.output) if job_dv.output else {}
    video = _video(sonde)
    annexes = [d.get("side_data_type") for d in video.get("side_data_list") or []]
    t.verifier(
        video.get("codec_name") == "hevc"
        and video.get("color_transfer") == "smpte2084"
        and "DOVI configuration record" not in annexes,
        f"{nom} : base HDR10 seule, sans Dolby Vision",
        f"{video.get('codec_name')}, {video.get('color_transfer')}",
    )
    t.verifier(
        any("Dolby Vision profil 8.1" in note for note in job_dv.notes),
        f"{nom} : perte signalée dans les notes",
    )

    # (c) 1080i : bwdif, une image par image (25p et non 50p), progressif.
    nom = "(c) 1080i -> progressif"
    job_c = _reencoder(t, bibliotheque, nom, sources["c"], "1080p")
    sonde = _verifier_hevc(t, outils, nom, job_c, 1920, 1080)
    video = _video(sonde)
    images = outils.images(job_c.output, 3) if job_c.output else []
    entrelacees = [i.get("interlaced_frame") for i in images]
    t.verifier(
        video.get("field_order") in (None, "progressive") and not any(entrelacees),
        f"{nom} : progressif",
        f"field_order={video.get('field_order')}, interlaced_frame={entrelacees}",
    )
    t.verifier(
        video.get("avg_frame_rate") == "25/1",
        f"{nom} : 25 images/s gardées",
        f"{video.get('avg_frame_rate')}",
    )

    # (d) Couverture et police : rejointes, a l'identique.
    nom = "(d) couverture et police"
    job_d = _reencoder(t, bibliotheque, nom, sources["d"], "720p")
    sonde = _verifier_hevc(t, outils, nom, job_d, 1280, 720)
    principales = [
        f for f in _flux(sonde, "video") if not (f.get("disposition") or {}).get("attached_pic")
    ]
    t.verifier(
        len(principales) == 1,
        f"{nom} : une seule piste vidéo encodée",
        ", ".join(str(f.get("codec_name")) for f in _flux(sonde, "video")),
    )
    couvertures = _couvertures(sonde)
    etiquettes = _etiquettes(couvertures[0]) if couvertures else {}
    t.verifier(
        len(couvertures) == 1
        and couvertures[0].get("codec_name") == "mjpeg"
        and etiquettes.get("filename") == "cover.jpg"
        and etiquettes.get("mimetype") == "image/jpeg",
        f"{nom} : couverture rejointe",
        f"{len(couvertures)} couverture(s) {etiquettes}",
        detail=_detail_travail(job_d),
    )
    if couvertures and job_d.output is not None:
        extraite = dossier / "couverture-extraite.jpg"
        reussi, detail = outils.fabriquer(
            extraite,
            "-i",
            str(job_d.output),
            *("-map", f"0:{couvertures[0].get('index')}", "-c", "copy", "-frames:v", "1"),
            *("-update", "1", "-f", "image2"),
        )
        originale = (dossier / "materiaux" / "cover.jpg").read_bytes()
        t.verifier(
            reussi and extraite.read_bytes() == originale,
            f"{nom} : couverture identique à l'octet",
            f"{len(originale)} octets",
            detail,
        )
    polices = [_etiquettes(f) for f in _flux(sonde, "attachment")]
    t.verifier(
        any(
            e.get("filename") == "police.ttf" and e.get("mimetype") == "application/x-truetype-font"
            for e in polices
        ),
        f"{nom} : police copiée",
        f"{polices}",
    )

    # (e) mov_text : converti en SRT, texte intact.
    nom = "(e) MP4 mov_text"
    job_e = _reencoder(t, bibliotheque, nom, sources["e"], "720p")
    sonde = _verifier_hevc(t, outils, nom, job_e, 1280, 720)
    sous_titres = _flux(sonde, "subtitle")
    t.verifier(
        [f.get("codec_name") for f in sous_titres] == ["subrip"],
        f"{nom} : sous-titres convertis en SRT",
        ", ".join(str(f.get("codec_name")) for f in sous_titres),
    )
    texte = dossier / "sous-titres-extraits.srt"
    reussi, detail = outils.fabriquer(texte, "-i", str(job_e.output), "-map", "0:s:0", "-f", "srt")
    t.verifier(
        reussi and SOUS_TITRE in texte.read_text(encoding="utf-8", errors="replace"),
        f"{nom} : texte identique",
        f"« {SOUS_TITRE} »",
        detail,
    )

    # (f) PCM 7.1 : E-AC-3 640 kbit/s, replie en 5.1.
    nom = "(f) PCM 7.1 -> E-AC-3"
    job_f = _reencoder(t, bibliotheque, nom, sources["f"], "720p", compresser_audio=True)
    sonde = _verifier_hevc(t, outils, nom, job_f, 1280, 720)
    audio = _flux(sonde, "audio")
    t.verifier(
        [(f.get("codec_name"), f.get("channels")) for f in audio] == [("eac3", 6)],
        f"{nom} : E-AC-3 5.1",
        ", ".join(f"{f.get('codec_name')} {f.get('channel_layout')}" for f in audio),
    )
    note = next((n for n in job_f.notes if "E-AC-3" in n), "")
    t.verifier(
        "repliées en 5.1" in note,
        f"{nom} : conversion signalée dans les notes",
        note,
    )
    return job_a


# --- 4. Le lecteur --------------------------------------------------------------------


def _horodatages(*fichiers: tuple[str, Path]) -> str:
    """Les 16 premieres images video de chaque fichier : pts, dts, duree, base."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return "ffprobe absent : pas d'horodatages"
    lignes = []
    for nom, chemin in fichiers:
        flux = subprocess.run(  # noqa: S603 - argv fixe
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=time_base,r_frame_rate,avg_frame_rate,has_b_frames",
                "-of",
                "compact",
                str(chemin),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        ).stdout.strip()
        paquets = (
            subprocess.run(  # noqa: S603 - argv fixe
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-read_intervals",
                    "%+#16",
                    "-show_entries",
                    "packet=pts,dts,duration,flags",
                    "-of",
                    "compact=p=0:nk=1",
                    str(chemin),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            .stdout.strip()
            .splitlines()
        )
        lignes.append(f"--- {nom} ({chemin.name}) : {flux}")
        lignes.append("    pts|dts|duree|drapeaux : " + "  ".join(paquets[:16]))
    return "\n".join(lignes)


def _segments(dossier: Path) -> list[Path]:
    return sorted(dossier.glob("seg_*.m4s"))


def _session(
    t: Tableau,
    outils: Outils,
    sessions: lecture.Sessions,
    nom: str,
    source: Path,
    mode: lecture.Mode,
    *,
    at: float = 0.0,
    segments_min: int = 1,
    codec: str = "h264",
    etiquette: str | None = None,
    **capacites: str,
) -> None:
    """Une vraie session HLS, telle que l'ouvre ``/api/media/.../session``, lue jusqu'au bout."""
    analyse = lecture.analyser(source)
    decision = lecture.decider(analyse, filtres=lecture.filtres_ffmpeg(), **capacites)
    t.verifier(
        decision.mode is mode,
        f"{nom} : décision « {mode} »",
        decision.resume,
        detail=decision.motif,
    )
    session = sessions.ouvrir(
        cle=f"verification:{nom}",
        source=source,
        analyse=analyse,
        decision=decision,
        at=at,
        ffmpeg=outils.ffmpeg,
    )
    argv = lecture.commande_hls(outils.ffmpeg, source, session.dossier, analyse, decision, at)
    journal = session.dossier / "ffmpeg.log"

    def detail() -> str:
        texte = ""
        with contextlib.suppress(OSError):
            texte = journal.read_text(encoding="utf-8", errors="replace")
        return _citer(argv, texte)

    debut = time.monotonic()
    try:
        try:
            sessions.liste(session.id, attente=DELAI)
            code = session.processus.wait(timeout=DELAI)
        except (lecture.ConversionEchouee, lecture.FichierAbsent, subprocess.TimeoutExpired) as exc:
            t.verifier(False, f"{nom} : production HLS", f"{type(exc).__name__} : {exc}", detail())
            return
        etat = sessions.etat(session.id)
        segments = _segments(session.dossier)
        t.verifier(
            code == 0 and etat["termine"] and not etat["erreur"],
            f"{nom} : production HLS terminée",
            f"{len(segments)} segment(s) en {time.monotonic() - debut:.1f} s",
            detail(),
        )
        liste = session.liste_texte() or ""
        t.verifier(
            len(segments) >= segments_min
            and liste.count("#EXTINF") == len(segments)
            and '#EXT-X-MAP:URI="init.mp4"' in liste
            and (session.dossier / "init.mp4").is_file(),
            f"{nom} : liste, init.mp4 et segments",
            f"{len(segments)} segment(s), {segments_min} au moins",
            detail=liste,
        )

        # Ce que recoit le navigateur : l'initialisation suivie des segments.
        assemblage = session.dossier.parent / f"{session.id}-assemblage.mp4"
        with assemblage.open("wb") as sortie:
            for morceau in [session.dossier / "init.mp4", *segments]:
                sortie.write(morceau.read_bytes())
        sonde = outils.sonder(assemblage)
        video = _video(sonde)
        audio = [f.get("codec_name") for f in _flux(sonde, "audio")]
        decode, detail_decodage = outils.decoder(assemblage)
        if not decode:
            # D'ou viennent des horodatages en double : de la source, de la
            # conversion ou de l'assemblage ? Les premieres images de chacun le
            # disent, et c'est ce qu'on ne peut pas reproduire hors de l'image.
            detail_decodage = (
                (detail_decodage or "")
                + "\n"
                + _horodatages(
                    ("source", source),
                    ("assemblage", assemblage),
                    ("premier segment", segments[0]) if segments else ("assemblage", assemblage),
                )
            )
        attendu_etiquette = etiquette is None or video.get("codec_tag_string") == etiquette
        t.verifier(
            decode and video.get("codec_name") == codec and attendu_etiquette and audio == ["aac"],
            f"{nom} : segments lisibles par ffprobe",
            f"{video.get('codec_name')} {video.get('codec_tag_string')} "
            f"{video.get('width')}x{video.get('height')}, audio {', '.join(map(str, audio))}",
            detail_decodage,
        )
        if decision.mode is lecture.Mode.TRANSCODE:
            couleurs = (video.get("color_transfer"), video.get("pix_fmt"), video.get("height"))
            attendu = ("bt709" if decision.tonemap else video.get("color_transfer"), "yuv420p")
            t.verifier(
                couleurs[:2] == attendu and couleurs[2] == decision.hauteur,
                f"{nom} : aperçu {decision.hauteur}p 8 bits"
                + (" SDR (zscale, tonemap)" if decision.tonemap else ""),
                f"{couleurs[0]}, {couleurs[1]}, {couleurs[2]} lignes",
            )
        premiere = etat["premiere_image"]
        t.verifier(
            premiere is not None and at - 2.5 <= premiere <= at + 0.5 and not etat["alerte"],
            f"{nom} : horodatages du film conservés",
            f"première image à {premiere if premiere is None else round(premiere, 2)} s "
            f"pour {at:g} s demandées",
            detail=f"état de la session : {etat}",
        )
    finally:
        sessions.arreter(session.id)


def verifier_lecteur(
    t: Tableau,
    outils: Outils,
    dossier: Path,
    sources: dict[str, Path],
    reencode: transcode.Job,
    filtres: frozenset[str],
) -> None:
    tonemap = {"zscale", "tonemap"} <= filtres

    analyse = lecture.analyser(sources["b"])
    t.verifier(analyse.premiere_image_lue, "analyse (b) : première image lue")
    t.verifier(
        analyse.hdr is lecture.TypeHdr.HDR10
        and analyse.profondeur == 10
        and (analyse.largeur, analyse.hauteur) == (3840, 1600),
        "analyse (b) : HDR10, 10 bits, 3840x1600",
        f"{analyse.libelle_hdr}, {analyse.libelle_profil}, {analyse.largeur}x{analyse.hauteur}",
    )
    t.verifier(
        analyse.mastering is not None
        and analyse.mastering.pour_x265() == MASTER_DISPLAY
        and analyse.lumiere is not None
        and analyse.lumiere.pour_x265() == MAX_CLL,
        "analyse (b) : mastering et MaxCLL",
        f"{analyse.lumiere.pour_x265() if analyse.lumiere else None} cd/m²",
    )

    decision = lecture.decider(lecture.analyser(sources["a"]), filtres=filtres)
    t.verifier(
        decision.mode is lecture.Mode.REMUX and decision.audio_copie,
        "décision (a) : réemballage, AAC copié",
        decision.resume,
        detail=decision.motif,
    )
    for capacite in ("", "main10"):
        decision = lecture.decider(analyse, filtres=filtres, hevc=capacite)
        t.verifier(
            decision.mode is lecture.Mode.TRANSCODE
            and decision.hauteur == lecture.HAUTEUR_APERCU
            and decision.tonemap == tonemap,
            f"décision (b), HEVC {capacite or 'non'} décodé : conversion",
            decision.resume + (", SDR" if decision.tonemap else ", sans tonemap"),
            detail=decision.motif,
        )

    sessions = lecture.Sessions(dossier / "lecture", entretien_auto=False)
    try:
        _session(
            t, outils, sessions, "HLS (h) réemballage", sources["h"], lecture.Mode.REMUX,
            segments_min=3,
        )  # fmt: skip
        _session(
            t, outils, sessions, "HLS (h) réemballage à 6 s", sources["h"], lecture.Mode.REMUX,
            at=6.0, segments_min=2,
        )  # fmt: skip
        if reencode.output is not None:
            _session(
                t, outils, sessions, "HLS (a) réencodé, HEVC Main 10", reencode.output,
                lecture.Mode.REMUX, codec="hevc", etiquette="hvc1", hevc="main10",
            )  # fmt: skip
        _session(
            t, outils, sessions, "HLS (i) conversion HDR", sources["i"], lecture.Mode.TRANSCODE,
            segments_min=3,
        )  # fmt: skip
        _session(
            t, outils, sessions, "HLS (i) conversion HDR à 6 s", sources["i"],
            lecture.Mode.TRANSCODE, at=6.0, segments_min=2,
        )  # fmt: skip
    finally:
        sessions.arreter_tout()

    for nom, chemin, originale in (
        ("contrôle de décodage (a)", sources["a"], None),
        ("contrôle de décodage (a) réencodé", reencode.output, reencode),
    ):
        if chemin is None:
            continue
        debut = time.monotonic()
        resultat = lecture.controler(
            chemin,
            analyse=lecture.analyser(chemin),
            duree_originale=originale.source_duration if originale else None,
        )
        t.verifier(
            resultat.etat is lecture.EtatControle.SAIN and len(resultat.points) == 5,
            f"{nom} : « sain » aux 5 points",
            f"{resultat.message} ({time.monotonic() - debut:.1f} s)",
            detail="\n".join(
                f"{p.fraction:.0%} ({p.secondes:.2f} s) : ok={p.ok}, images={p.images}, "
                f"erreurs={list(p.erreurs)}"
                for p in resultat.points
            ),
        )


# --- Le tout --------------------------------------------------------------------------


def verifier(t: Tableau, outils: Outils, dossier: Path, *, strict: bool) -> None:
    t.section("1. Le ffmpeg installé")
    filtres = verifier_installation(t, outils, strict=strict)
    t.section("2. Sources de test (ffmpeg -f lavfi)")
    sources = fabriquer_sources(t, outils, dossier)
    t.section(f"3. Réencodage HEVC 10 bits (transcode.preparer puis transcode.run, {PRESET})")
    with _traces_conservees():
        reencode = verifier_reencodage(t, outils, dossier, sources)
    t.section("4. Lecteur (lecture.analyser, decider, Sessions, controler)")
    verifier_lecteur(t, outils, dossier, sources, reencode, filtres)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m sortilege.outils.verifier_ffmpeg",
        description=(
            "Fait passer de vraies vidéos par le réencodage et le lecteur de Sortilège, avec le "
            "ffmpeg installé, et s'arrête au premier échec."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exiger aussi zscale et tonemap (l'image Docker les a, Homebrew non)",
    )
    parser.add_argument(
        "--garder",
        action="store_true",
        help="garder le dossier de travail (sources, réencodages) pour l'inspecter",
    )
    options = parser.parse_args(arguments)
    logging.basicConfig(
        level=logging.WARNING, format="         [application] %(levelname)s %(name)s : %(message)s"
    )

    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        print("ÉCHEC : ffmpeg ou ffprobe introuvable dans le PATH.", flush=True)
        return 1

    t = Tableau()
    dossier = Path(tempfile.mkdtemp(prefix="sortilege-ffmpeg-"))
    try:
        verifier(t, Outils(ffmpeg, ffprobe), dossier, strict=options.strict)
    except Echec as echec:
        print(f"\nÉCHEC : {echec}", flush=True)
        if echec.detail:
            print("\n".join(f"    {ligne}" for ligne in echec.detail.splitlines()), flush=True)
        print(
            f"\n{t.faites} vérification(s) passée(s) avant l'échec, en {t.ecoule:.0f} s.",
            flush=True,
        )
        return 1
    finally:
        if options.garder:
            print(f"\nDossier de travail conservé : {dossier}", flush=True)
        else:
            shutil.rmtree(dossier, ignore_errors=True)
    print(f"\nTout est OK : {t.faites} vérifications en {t.ecoule:.0f} s.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
