"""Ce que la bibliotheque contient, ce qu'il y manque, et ce qui y est en double.

Trois questions, une seule passe de regroupement — elles portent toutes sur la
meme structure « quelles oeuvres, quels episodes, quels fichiers ».

- **La collection** : les oeuvres possedees, avec leur affiche. Une grille se
  parcourt d'un coup d'oeil la ou une liste de 400 fichiers ne se lit pas.
- **Les manques** : les episodes qu'une saison compte et que le disque n'a pas.
- **Les doublons** : deux fichiers pour le meme episode ou le meme film.

Le regroupement s'appuie sur ce que le parseur lit, pas sur l'arborescence :
une bibliotheque rangee par Sortilege est bien structuree, mais une
bibliotheque heritee ne l'est pas — et c'est justement celle-la qu'on veut
pouvoir inspecter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .parser import MediaKind
from .scanner import ScannedFile

logger = logging.getLogger(__name__)

# Ordre de preference quand deux fichiers se disputent la meme place.
_RESOLUTION_RANK = {"2160p": 4, "1080p": 3, "720p": 2, "576p": 1}


@dataclass(slots=True)
class FileRef:
    """Un fichier, reduit a ce qui sert a arbitrer entre doublons."""

    relative_path: str
    size_bytes: int
    resolution: str = ""
    codec: str = ""
    source: str = ""

    @property
    def rank(self) -> tuple[int, int]:
        """Resolution d'abord, taille ensuite.

        La resolution prime : un 2160p compresse vaut mieux qu'un 1080p
        volumineux. A resolution egale, le plus gros a generalement le meilleur
        debit."""
        return (_RESOLUTION_RANK.get(self.resolution, 0), self.size_bytes)


@dataclass
class DuplicateGroup:
    """Plusieurs fichiers pour une meme place."""

    label: str
    files: list[FileRef] = field(default_factory=list)

    @property
    def best(self) -> FileRef:
        return max(self.files, key=lambda f: f.rank)

    @property
    def redundant(self) -> list[FileRef]:
        keep = self.best
        return [f for f in self.files if f is not keep]

    @property
    def wasted_bytes(self) -> int:
        return sum(f.size_bytes for f in self.redundant)


@dataclass
class SeasonHolding:
    """Une saison : ce qu'on a, et ce que le fournisseur en connait."""

    number: int
    owned: set[int] = field(default_factory=set)
    known: dict[int, str] = field(default_factory=dict)
    """Numero -> titre, pour les episodes DEJA DIFFUSES uniquement."""

    @property
    def missing(self) -> list[int]:
        if not self.known:
            return []
        return sorted(set(self.known) - self.owned)

    @property
    def complete(self) -> bool:
        return bool(self.known) and not self.missing


@dataclass
class Work:
    """Une oeuvre possedee, avec ses fichiers."""

    key: str
    kind: str
    title: str
    year: int | None = None
    poster_url: str = ""
    provider: str = ""
    external_id: str = ""
    file_count: int = 0
    total_bytes: int = 0
    seasons: dict[int, SeasonHolding] = field(default_factory=dict)
    duplicates: list[DuplicateGroup] = field(default_factory=list)
    identified: bool = False

    # Emplacement -> fichiers. « S02E07 » pour un episode, « film » pour un
    # film : c'est la cle qui definit ce qu'est un doublon.
    slots: dict[str, list[FileRef]] = field(default_factory=dict, repr=False)

    @property
    def missing_count(self) -> int:
        return sum(len(s.missing) for s in self.seasons.values())

    @property
    def owned_count(self) -> int:
        return sum(len(s.owned) for s in self.seasons.values())

    @property
    def has_gaps(self) -> bool:
        return self.missing_count > 0

    @property
    def wasted_bytes(self) -> int:
        return sum(g.wasted_bytes for g in self.duplicates)


def _slot_label(scanned: ScannedFile) -> str:
    parsed = scanned.parsed
    if parsed.kind is MediaKind.MOVIE:
        return "film"
    if parsed.season is not None and parsed.episode is not None:
        return f"S{parsed.season:02d}E{parsed.episode:02d}"
    if parsed.absolute_episode is not None:
        return f"ep {parsed.absolute_episode}"
    # Sans numero, impossible de savoir si deux fichiers occupent la meme
    # place : on leur donne des cles distinctes plutot que de les declarer
    # doublons a tort.
    return f"?{scanned.relative_path}"


def group(files: list[ScannedFile]) -> list[Work]:
    """Regroupe des fichiers scannes en oeuvres, doublons compris.

    La cle combine type et titre normalise : deux oeuvres homonymes de types
    differents ne doivent pas fusionner, et « Severance » ecrit avec ou sans
    majuscules designe la meme serie.
    """
    works: dict[str, Work] = {}

    for scanned in files:
        parsed = scanned.parsed
        title = parsed.title or scanned.probe.nfo_title or scanned.path.stem
        kind = "movie" if parsed.kind is MediaKind.MOVIE else str(parsed.kind)
        key = f"{kind}:{title.casefold()}"

        work = works.get(key)
        if work is None:
            work = Work(key=key, kind=kind, title=title, year=parsed.year)
            works[key] = work

        # L'annee la plus precise gagne : un episode peut la porter alors qu'un
        # autre de la meme serie ne l'a pas.
        if work.year is None and parsed.year is not None:
            work.year = parsed.year

        work.file_count += 1
        work.total_bytes += scanned.size_bytes

        reference = FileRef(
            relative_path=scanned.relative_path,
            size_bytes=scanned.size_bytes,
            resolution=scanned.probe.resolution_label or parsed.resolution or "",
            codec=scanned.probe.video_codec or parsed.codec or "",
            source=parsed.source or "",
        )
        work.slots.setdefault(_slot_label(scanned), []).append(reference)

        if kind == "movie":
            continue

        season_number = parsed.season if parsed.season is not None else 0
        season = work.seasons.setdefault(season_number, SeasonHolding(number=season_number))
        episode = parsed.episode if parsed.episode is not None else parsed.absolute_episode
        if episode is not None:
            season.owned.add(episode)

    for work in works.values():
        work.duplicates = [
            DuplicateGroup(label=label, files=refs)
            for label, refs in sorted(work.slots.items())
            if len(refs) > 1
        ]

    return sorted(works.values(), key=lambda w: (w.kind, w.title.casefold()))


def fill_known_episodes(season: SeasonHolding, episodes: dict[int, object] | None) -> None:
    """Renseigne ce que le fournisseur connait, en ignorant le non-diffuse.

    Compter un episode a paraitre comme manquant remplirait la liste de trous
    qu'on ne peut pas combler — elle cesserait d'etre lisible, donc utile.
    """
    if not episodes:
        return
    season.known = {
        number: getattr(info, "name", "")
        for number, info in episodes.items()
        if getattr(info, "aired", True)
    }
