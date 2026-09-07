"""Extraction des informations depuis un nom de release.

Purement deterministe, zero appel reseau. C'est la premiere passe : elle doit
etre rapide et honnete sur son incertitude, pas maligne. Ce qu'elle n'arrive pas
a lire devient un signal faible pour le scoring, qui decidera d'escalader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".mpg", ".mpeg",
}


class MediaKind(str, Enum):
    MOVIE = "movie"
    EPISODE = "episode"
    ANIME = "anime"
    UNKNOWN = "unknown"


# --- Motifs episodiques, du plus fiable au plus douteux ---------------------

_SEASON_EPISODE = re.compile(r"[Ss](?P<season>\d{1,2})[\s._-]*[Ee](?P<episode>\d{1,3})")
_SEASON_X_EPISODE = re.compile(r"(?<!\d)(?P<season>\d{1,2})[xX](?P<episode>\d{2,3})(?!\d)")
_VERBOSE = re.compile(
    r"[Ss]aison[\s._-]*(?P<season>\d{1,2})[\s._-]*[EeÉé]pisode[\s._-]*(?P<episode>\d{1,3})"
)
# Numerotation absolue de fansub : « [Groupe] Titre - 147 [1080p] ».
_ABSOLUTE = re.compile(r"[\s._-]-[\s._-](?P<absolute>\d{1,4})(?!\d)")

_YEAR = re.compile(r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?!\d)")
_RESOLUTION = re.compile(r"(?P<res>\d{3,4}[pi]|4[kK]|[Uu][Hh][Dd])")
_FANSUB_GROUP = re.compile(r"^\[(?P<group>[^\]]{2,30})\]")

_SOURCES = {
    "bluray": ("bluray", "blu-ray", "bdrip", "brrip", "bdremux"),
    "web": ("web-dl", "webdl", "webrip", "web"),
    "hdtv": ("hdtv", "pdtv"),
    "dvd": ("dvdrip", "dvd"),
}
_CODECS = ("x265", "h265", "hevc", "x264", "h264", "avc", "av1", "xvid", "divx")
_LANGS = {
    "multi": ("multi",),
    "vff": ("vff", "truefrench", "french"),
    "vostfr": ("vostfr", "vost"),
    "vo": ("vo", "vosta"),
}

# Bruit a retirer du titre une fois les champs extraits.
_NOISE = re.compile(
    r"\b(?:"
    r"1080[pi]|720[pi]|2160[pi]|480[pi]|4k|uhd|hdr10?|dolby ?vision|dv|sdr|"
    r"bluray|blu-ray|bdrip|brrip|bdremux|web-?dl|webrip|web|hdtv|pdtv|dvdrip|dvd|"
    r"x265|x264|h ?265|h ?264|hevc|avc|av1|xvid|divx|"
    r"aac|ac3|eac3|dts(?:-hd)?|truehd|atmos|flac|opus|mp3|"
    r"multi|truefrench|french|vff|vostfr|vost|vo|subfrench|"
    r"repack|proper|remux|extended|unrated|directors? cut|imax|"
    r"complete|integrale|saison|season"
    r")\b",
    re.IGNORECASE,
)

_SEPARATORS = re.compile(r"[._]+")
_MULTISPACE = re.compile(r"\s{2,}")


@dataclass(slots=True)
class ParsedName:
    """Ce que le parseur a cru lire. ``quality`` dit a quel point il y croit."""

    raw: str
    title: str
    kind: MediaKind
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    absolute_episode: int | None = None
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    language: str | None = None
    release_group: str | None = None
    fansub_group: str | None = None
    quality: float = 0.0
    signals: list[str] = field(default_factory=list)


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def _find_source(haystack: str) -> str | None:
    for label, needles in _SOURCES.items():
        if any(n in haystack for n in needles):
            return label
    return None


def _find_language(haystack: str) -> str | None:
    for label, needles in _LANGS.items():
        if any(re.search(rf"\b{re.escape(n)}\b", haystack) for n in needles):
            return label
    return None


def _clean_title(raw: str, cut_at: int | None) -> str:
    title = raw[:cut_at] if cut_at is not None else raw
    title = _FANSUB_GROUP.sub("", title)
    title = _SEPARATORS.sub(" ", title)
    title = _NOISE.sub(" ", title)
    title = re.sub(r"[\[\](){}]", " ", title)
    title = re.sub(r"[-–—]+\s*$", "", title.strip())
    title = _MULTISPACE.sub(" ", title).strip(" -_")
    return title


def parse(path: Path) -> ParsedName:
    """Analyse un chemin de fichier video.

    Le dossier parent est joint au nom : beaucoup de releases mettent l'annee
    ou le titre complet uniquement sur le dossier.
    """
    stem = path.stem
    context = f"{path.parent.name} {stem}"
    lowered = context.lower()
    signals: list[str] = []

    fansub = _FANSUB_GROUP.search(stem)
    fansub_group = fansub.group("group") if fansub else None

    season = episode = absolute = None
    cut_at: int | None = None
    kind = MediaKind.MOVIE

    for pattern, name in ((_SEASON_EPISODE, "SxxExx"), (_VERBOSE, "verbeux"), (_SEASON_X_EPISODE, "NxNN")):
        if m := pattern.search(stem):
            season = int(m.group("season"))
            episode = int(m.group("episode"))
            cut_at = m.start()
            kind = MediaKind.EPISODE
            signals.append(f"motif episodique {name}")
            break

    if kind is MediaKind.MOVIE and fansub_group:
        # Un groupe de fansub en tete + un nombre isole : tres probablement un
        # anime en numerotation absolue. On ne tranche pas ici, on le signale.
        if m := _ABSOLUTE.search(stem):
            absolute = int(m.group("absolute"))
            cut_at = m.start()
            kind = MediaKind.ANIME
            signals.append("numerotation absolue + groupe de fansub")

    year = None
    if m := _YEAR.search(context):
        year = int(m.group("year"))
        signals.append("annee trouvee")
        if kind is MediaKind.MOVIE:
            # Pour un film, le titre s'arrete a l'annee.
            pos = stem.find(m.group("year"))
            if pos > 0:
                cut_at = pos

    resolution = m.group("res").lower() if (m := _RESOLUTION.search(context)) else None
    source = _find_source(lowered)
    codec = next((c for c in _CODECS if c in lowered), None)
    language = _find_language(lowered)

    title = _clean_title(stem, cut_at)
    if not title:
        title = _clean_title(path.parent.name, None)
        signals.append("titre repris du dossier parent")

    return ParsedName(
        raw=stem,
        title=title,
        kind=kind if title else MediaKind.UNKNOWN,
        year=year,
        season=season,
        episode=episode,
        absolute_episode=absolute,
        resolution=resolution,
        source=source,
        codec=codec,
        language=language,
        fansub_group=fansub_group,
        quality=_parse_quality(title, year, kind, season, episode, absolute),
        signals=signals,
    )


def _parse_quality(
    title: str,
    year: int | None,
    kind: MediaKind,
    season: int | None,
    episode: int | None,
    absolute: int | None,
) -> float:
    """Confiance du parseur dans sa propre lecture, 0 -> 1.

    Ce n'est PAS le score de confiance final : c'est un seul des signaux que
    ``scoring.py`` combine. Un titre long et propre avec une annee est un bon
    depart ; un titre de trois lettres sans rien d'autre ne l'est pas.
    """
    if not title:
        return 0.0

    score = 0.35
    if len(title) >= 4:
        score += 0.15
    if " " in title:
        score += 0.10
    if year is not None:
        score += 0.20
    if kind is MediaKind.EPISODE and season is not None and episode is not None:
        score += 0.20
    if kind is MediaKind.ANIME and absolute is not None:
        # L'absolu est un signal faible : la correspondance saison/episode
        # reste a faire cote provider.
        score += 0.05
    if re.fullmatch(r"[\d\s]+", title):
        score -= 0.30

    return max(0.0, min(1.0, score))
