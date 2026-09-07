"""Extraction des informations depuis un nom de release.

Purement deterministe, zero appel reseau. C'est la premiere passe : elle doit
etre rapide et honnete sur son incertitude, pas maligne. Ce qu'elle n'arrive pas
a lire devient un signal faible pour le scoring, qui decidera d'escalader.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".m4v",
    ".mov",
    ".wmv",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
}


class MediaKind(StrEnum):
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

# « 07.mkv », « ep07.mkv », « episode 7.mkv » — uniquement exploitable a
# l'interieur d'un dossier de saison, sinon « 2012.mkv » deviendrait l'episode
# 2012.
_BARE_EPISODE = re.compile(r"^(?:[ée]p(?:isode)?[\s._-]*)?(?P<episode>\d{1,3})$", re.IGNORECASE)

_YEAR = re.compile(r"(?<!\d)(?P<year>19\d{2}|20\d{2})(?!\d)")
# Une annee entre parentheses ou crochets est une annee de sortie declaree,
# jamais un nombre du titre. Elle prime sur tout le reste.
_YEAR_DELIMITED = re.compile(r"[(\[](?P<year>19\d{2}|20\d{2})[)\]]")
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

# Dossiers qui ne portent aucune information de titre : classement, decoupage
# de disque, structure de support, pistes annexes. Les inclure dans le contexte
# ferait deriver le titre (« Films Dune Part Two » au lieu de « Dune Part Two »)
# et pourrait meme fournir une fausse annee.
_GENERIC_DIR = re.compile(
    r"^(?:"
    r"cd\s*\d+|disc\s*\d+|disk\s*\d+|part\s*\d+|"
    # Le nom est normalise (underscores -> espaces) avant le test, d'ou le
    # separateur souple : « VIDEO_TS » arrive ici sous la forme « VIDEO TS ».
    r"video[\s_-]?ts|bdmv|stream|playlist|certificate|"
    r"s(?:aison|eason)?\s*\d{1,2}|"
    r"subs?|subtitles|sous[-\s]?titres|extras|featurettes|bonus|"
    r"movies?|films?|series|s[ée]ries|tv[-\s]?shows?|animes?|mangas?|"
    r"downloads?|t[ée]l[ée]chargements?|complete|int[ée]grale|new|divers"
    r")$",
    re.IGNORECASE,
)

# « Season 02 », « Saison 2 », « S03 » : le numero est exploitable meme quand le
# dossier n'apporte rien au titre.
_SEASON_DIR = re.compile(r"^s(?:aison|eason)?[\s._-]*(?P<season>\d{1,2})$", re.IGNORECASE)


# Noms de fichiers qui ne designent rien : sorties d'encodeur, structures de
# disque, ou simple « film.mkv ». Dans ces cas le titre est forcement porte par
# un dossier, et s'obstiner sur le nom de fichier donne « movie » pour titre.
_UNINFORMATIVE_STEM = re.compile(
    r"^(?:"
    r"films?|movies?|video|vid|main|title|index|output|encode|"
    r"vts[_\s-]\d+[_\s-]\d+|title[_\s-]?t?\d+|\d{1,5}|"
    r"[ée]p(?:isode)?[\s._-]*\d{1,3}|"
    r"video[\s_-]?ts|bdmv"
    r")$",
    re.IGNORECASE,
)


def _is_uninformative_stem(stem: str) -> bool:
    return _UNINFORMATIVE_STEM.match(_SEPARATORS.sub(" ", stem).strip()) is not None


def _is_informative_dir(name: str) -> bool:
    """Ce dossier apporte-t-il quelque chose au titre ?"""
    cleaned = _SEPARATORS.sub(" ", name).strip()
    if len(cleaned) < 2:
        return False
    return _GENERIC_DIR.match(cleaned) is None


def _season_from_dirs(chain: list[str]) -> int | None:
    """Numero de saison porte par un dossier.

    Arborescence tres frequente : « Severance/Season 02/ep07.mkv ». Le nom de
    fichier seul ne dit pas la saison, le dossier si.
    """
    for name in reversed(chain):
        cleaned = _SEPARATORS.sub(" ", name).strip()
        if m := _SEASON_DIR.match(cleaned):
            return int(m.group("season"))
    return None


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


def _find_year(context: str) -> tuple[int | None, str]:
    """Trouve l'annee de sortie, en evitant les nombres du titre.

    « Blade Runner 2049 (2017) » et « 2012 (2009) » cassent la lecture naive :
    le premier nombre a l'air d'une annee mais appartient au titre. Deux regles
    suffisent a couvrir la quasi-totalite des cas reels :

    1. Une annee entre parentheses ou crochets gagne toujours.
    2. Sinon, on prend la DERNIERE occurrence — un titre precede son annee.
    """
    if m := _YEAR_DELIMITED.search(context):
        return int(m.group("year")), m.group("year")

    matches = list(_YEAR.finditer(context))
    if not matches:
        return None, ""
    last = matches[-1]
    return int(last.group("year")), last.group("year")


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
    # Les tirets cadratins sont voulus : les releases francaises et les fansubs
    # les utilisent comme separateurs. noqa car ruff les signale comme ambigus.
    title = re.sub(r"[-–—]+\s*$", "", title.strip())  # noqa: RUF001
    title = _MULTISPACE.sub(" ", title).strip(" -_")
    return title


def _title_from_folder(name: str) -> str:
    """Titre porte par un nom de dossier, ampute de son annee.

    « Severance (2022) » doit donner « Severance », pas « Severance 2022 » :
    l'annee est deja captee separement, la laisser dans le titre ferait chuter
    la similarite face au libelle du fournisseur.
    """
    _, year_text = _find_year(name)
    cut = name.rfind(year_text) if year_text else -1
    return _clean_title(name, cut if cut > 0 else None)


def parse(path: Path, ancestors: list[str] | None = None) -> ParsedName:
    """Analyse un chemin de fichier video.

    Les dossiers parents sont joints au nom : beaucoup de releases ne mettent
    l'annee ou le titre complet que sur le dossier.

    ``ancestors`` est la chaine de dossiers entre la racine source et le
    fichier, du plus haut au plus bas. Sans elle on se rabat sur le seul parent
    immediat, ce qui echoue des qu'il y a un sous-dossier : sur
    « Dune (2024)/CD1/film.mkv » le parent est « CD1 », et sur
    « Severance/Season 02/ep.mkv » c'est « Season 02 » — dans les deux cas le
    titre et l'annee sont un cran plus haut.
    """
    stem = path.stem
    chain = ancestors if ancestors is not None else [path.parent.name]
    informative = [d for d in chain if _is_informative_dir(d)]
    context = " ".join([*informative, stem])
    lowered = context.lower()
    signals: list[str] = []

    fansub = _FANSUB_GROUP.search(stem)
    fansub_group = fansub.group("group") if fansub else None

    season = episode = absolute = None
    cut_at: int | None = None
    kind = MediaKind.MOVIE

    for pattern, name in (
        (_SEASON_EPISODE, "SxxExx"),
        (_VERBOSE, "verbeux"),
        (_SEASON_X_EPISODE, "NxNN"),
    ):
        if m := pattern.search(stem):
            season = int(m.group("season"))
            episode = int(m.group("episode"))
            cut_at = m.start()
            kind = MediaKind.EPISODE
            signals.append(f"motif episodique {name}")
            break

    # Saison portee par un dossier plutot que par le nom de fichier.
    if season is None:
        if (folder_season := _season_from_dirs(chain)) is not None:
            season = folder_season
            signals.append("saison lue sur le dossier")
            # Un numero d'episode nu (« 07.mkv », « ep07.mkv ») ne devient
            # exploitable qu'une fois la saison connue.
            if episode is None and (m := _BARE_EPISODE.search(stem)):
                episode = int(m.group("episode"))
                kind = MediaKind.EPISODE
                cut_at = m.start()
                signals.append("numero d'episode nu dans un dossier de saison")

    if kind is MediaKind.MOVIE and fansub_group:
        # Un groupe de fansub en tete + un nombre isole : tres probablement un
        # anime en numerotation absolue. On ne tranche pas ici, on le signale.
        if m := _ABSOLUTE.search(stem):
            absolute = int(m.group("absolute"))
            cut_at = m.start()
            kind = MediaKind.ANIME
            signals.append("numerotation absolue + groupe de fansub")

    year, year_text = _find_year(context)
    if year is not None:
        signals.append("annee trouvee")
        if kind is MediaKind.MOVIE:
            # Pour un film, le titre s'arrete a l'annee.
            pos = stem.rfind(year_text)
            if pos > 0:
                cut_at = pos

    resolution = m.group("res").lower() if (m := _RESOLUTION.search(context)) else None
    source = _find_source(lowered)
    codec = next((c for c in _CODECS if c in lowered), None)
    language = _find_language(lowered)

    title = _clean_title(stem, cut_at)

    # Le nom de fichier n'a rien donne (« film.mkv », « 00001.m2ts »,
    # « ep07.mkv ») : on remonte la chaine des dossiers, du plus proche au plus
    # lointain. C'est le cas normal des arborescences a sous-dossiers.
    if not title or _is_uninformative_stem(stem):
        for folder in reversed(informative):
            folder_title = _title_from_folder(folder)
            if folder_title:
                title = folder_title
                signals.append(f"titre repris du dossier « {folder} »")
                break

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
