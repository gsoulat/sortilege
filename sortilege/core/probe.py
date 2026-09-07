"""Ce que le FICHIER dit de lui-meme, independamment de son nom.

C'est l'approche de Plex, et la raison pour laquelle il se trompe moins qu'un
outil qui ne lit que le nom : trois sources s'ajoutent au parseur, et surtout
elles sont **decorrelees** de lui. Un nom de release et un titre de conteneur
mal lus ne se trompent pas de la meme facon, donc leur accord vaut confirmation.

Par valeur decroissante :

1. **Fichier .nfo** — convention Kodi/Emby, ecrite par Radarr et Sonarr. S'il
   porte un ``tmdbid`` ou un ``imdbid``, il n'y a plus rien a deviner : c'est
   une identite declaree, pas une ressemblance.
2. **Tags du conteneur** MKV/MP4 — beaucoup d'encodeurs y ecrivent le vrai
   titre, parfois la serie et le numero d'episode.
3. **Duree** — un episode fait 20 a 60 minutes, un film 80 a 240. Ce seul
   signal separe film et episode sans rien lire du nom, et contredit une
   identification absurde (un « film » de 22 minutes).

Tout est optionnel : sans ``ffprobe`` installe, la sonde renvoie ce qu'elle
peut et le reste du pipeline fonctionne avec un signal de moins.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

FFPROBE_TIMEOUT = 20.0

# Bornes de plausibilite, en secondes. Larges a dessein : elles servent a
# ecarter l'absurde, pas a trancher les cas limites.
MOVIE_RUNTIME = (60 * 60, 300 * 60)  # 1 h a 5 h
EPISODE_RUNTIME = (3 * 60, 90 * 60)  # 3 min a 1 h 30

_IMDB_ID = re.compile(r"tt\d{7,9}")


@dataclass(slots=True)
class FileProbe:
    """Ce qu'on a pu lire dans le fichier et a cote."""

    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    video_codec: str | None = None
    audio_languages: list[str] | None = None

    # Tags du conteneur
    container_title: str | None = None
    container_show: str | None = None
    container_season: int | None = None
    container_episode: int | None = None

    # Identifiants declares (.nfo ou tags) — les signaux les plus forts
    tmdb_id: str | None = None
    imdb_id: str | None = None
    tvdb_id: str | None = None
    nfo_title: str | None = None
    nfo_year: int | None = None

    probed: bool = False
    """False si ffprobe est absent ou a echoue : distingue « pas de donnee »
    de « donnee absente du fichier », ce qui n'a pas le meme sens pour le
    scoring."""

    @property
    def resolution_label(self) -> str | None:
        """Resolution REELLE, pas celle annoncee dans le nom de release.

        Les noms mentent regulierement : un fichier etiquete 1080p qui fait
        1280x720 est frequent. Autant ranger d'apres la verite."""
        if not self.height:
            return None
        if self.height >= 2000:
            return "2160p"
        if self.height >= 1000:
            return "1080p"
        if self.height >= 700:
            return "720p"
        if self.height >= 500:
            return "576p"
        return f"{self.height}p"

    @property
    def has_declared_id(self) -> bool:
        return bool(self.tmdb_id or self.imdb_id or self.tvdb_id)


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_media(path: Path) -> FileProbe:
    """Lit duree, pistes et tags via ffprobe. Ne leve jamais."""
    result = FileProbe()

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        logger.debug("ffprobe absent : analyse du conteneur ignoree")
        return result

    try:
        completed = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            [
                ffprobe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("ffprobe a echoue sur %s (%s)", path.name, type(exc).__name__)
        return result

    if completed.returncode != 0 or not completed.stdout:
        return result

    try:
        data = json.loads(completed.stdout)
    except ValueError:
        return result

    result.probed = True
    _fill_format(result, data.get("format") or {})
    _fill_streams(result, data.get("streams") or [])
    return result


def _fill_format(result: FileProbe, fmt: dict) -> None:
    if duration := fmt.get("duration"):
        try:
            result.duration_seconds = float(duration)
        except (TypeError, ValueError):
            pass

    # Les cles de tags varient selon le conteneur et la casse.
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    result.container_title = tags.get("title") or None
    result.container_show = tags.get("show") or tags.get("series") or None

    for key in ("season_number", "season"):
        if (value := _as_int(tags.get(key))) is not None:
            result.container_season = value
            break

    for key in ("episode_sort", "episode_id", "episode"):
        if (value := _as_int(tags.get(key))) is not None:
            result.container_episode = value
            break

    # Certains encodeurs collent l'IMDb id dans un commentaire.
    for field in ("comment", "description", "synopsis"):
        if text := tags.get(field):
            if m := _IMDB_ID.search(str(text)):
                result.imdb_id = m.group(0)
                break


def _fill_streams(result: FileProbe, streams: list[dict]) -> None:
    languages: list[str] = []
    for stream in streams:
        kind = stream.get("codec_type")
        if kind == "video" and result.video_codec is None:
            result.video_codec = stream.get("codec_name")
            result.width = _as_int(stream.get("width"))
            result.height = _as_int(stream.get("height"))
        elif kind == "audio":
            lang = (stream.get("tags") or {}).get("language")
            if lang and lang not in languages:
                languages.append(lang)
    result.audio_languages = languages or None


def read_nfo(media_path: Path) -> FileProbe:
    """Lit le .nfo associe, s'il existe.

    Deux emplacements conventionnels : « film.nfo » a cote de « film.mkv », ou
    « movie.nfo » / « tvshow.nfo » dans le dossier. Radarr et Sonarr ecrivent
    le premier.
    """
    result = FileProbe()

    candidates = [
        media_path.with_suffix(".nfo"),
        media_path.parent / "movie.nfo",
        media_path.parent / "tvshow.nfo",
    ]
    nfo = next((p for p in candidates if p.is_file()), None)
    if nfo is None:
        return result

    try:
        text = nfo.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result

    # Un .nfo peut etre du XML Kodi ou un simple dump texte de release. On tente
    # le XML, et on retombe sur une recherche d'identifiant dans le texte brut.
    try:
        root = ElementTree.fromstring(text)  # noqa: S314 - fichier local, pas reseau
    except ElementTree.ParseError:
        if m := _IMDB_ID.search(text):
            result.imdb_id = m.group(0)
        return result

    result.tmdb_id = _first_text(root, ("tmdbid", "tmdb_id")) or _uniqueid(root, "tmdb")
    result.tvdb_id = _first_text(root, ("tvdbid", "tvdb_id")) or _uniqueid(root, "tvdb")
    result.imdb_id = _first_text(root, ("imdbid", "imdb_id", "id")) or _uniqueid(root, "imdb")

    # Le champ <id> est generique : ne le garder que s'il ressemble a un IMDb id.
    if result.imdb_id and not _IMDB_ID.fullmatch(result.imdb_id):
        result.imdb_id = None

    result.nfo_title = _first_text(root, ("title", "originaltitle", "showtitle"))
    result.nfo_year = _as_int(_first_text(root, ("year",)))
    result.container_season = _as_int(_first_text(root, ("season",)))
    result.container_episode = _as_int(_first_text(root, ("episode",)))

    return result


def merge(media: FileProbe, nfo: FileProbe) -> FileProbe:
    """Fusionne sonde et .nfo. Le .nfo gagne sur les identifiants et le titre.

    Raison : un .nfo est ecrit deliberement par un outil de bibliotheque, alors
    qu'un tag de conteneur est souvent un residu de l'encodage.
    """
    return FileProbe(
        duration_seconds=media.duration_seconds,
        width=media.width,
        height=media.height,
        video_codec=media.video_codec,
        audio_languages=media.audio_languages,
        container_title=media.container_title,
        container_show=media.container_show,
        container_season=nfo.container_season or media.container_season,
        container_episode=nfo.container_episode or media.container_episode,
        tmdb_id=nfo.tmdb_id or media.tmdb_id,
        imdb_id=nfo.imdb_id or media.imdb_id,
        tvdb_id=nfo.tvdb_id or media.tvdb_id,
        nfo_title=nfo.nfo_title,
        nfo_year=nfo.nfo_year,
        probed=media.probed,
    )


def inspect(media_path: Path) -> FileProbe:
    """Point d'entree : tout ce que le fichier et son voisinage revelent."""
    return merge(probe_media(media_path), read_nfo(media_path))


def runtime_plausible(duration: float | None, kind: str) -> bool | None:
    """La duree est-elle compatible avec le type suppose ?

    Renvoie None si la duree est inconnue — « je ne sais pas » n'est pas
    « non », et le scoring doit pouvoir faire la difference.
    """
    if duration is None or duration <= 0:
        return None
    low, high = MOVIE_RUNTIME if kind == "movie" else EPISODE_RUNTIME
    return low <= duration <= high


def _as_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _first_text(root: ElementTree.Element, names: tuple[str, ...]) -> str | None:
    for name in names:
        element = root.find(name)
        if element is not None and element.text and element.text.strip():
            return element.text.strip()
    return None


def _uniqueid(root: ElementTree.Element, kind: str) -> str | None:
    """Kodi ecrit <uniqueid type="tmdb">1234</uniqueid>."""
    for element in root.findall("uniqueid"):
        if element.get("type") == kind and element.text:
            return element.text.strip()
    return None
