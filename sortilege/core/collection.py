"""Ce que la bibliotheque contient, et ce qu'il y manque.

Deux vues sur la meme donnee :

- **La collection** : les oeuvres possedees, avec leur affiche. Une grille se
  parcourt d'un coup d'oeil la ou une liste de 400 fichiers ne se lit pas.
- **Les manques** : les episodes qu'une saison compte et que le disque n'a pas.

Le regroupement s'appuie sur ce que le parseur lit, pas sur l'arborescence :
une bibliotheque rangee par Sortilege est bien structuree, mais une
bibliotheque heritee ne l'est pas, et c'est justement celle-la qu'on veut
pouvoir inspecter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .parser import MediaKind
from .scanner import ScannedFile

logger = logging.getLogger(__name__)


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
    identified: bool = False

    @property
    def missing_count(self) -> int:
        return sum(len(s.missing) for s in self.seasons.values())

    @property
    def owned_count(self) -> int:
        return sum(len(s.owned) for s in self.seasons.values())

    @property
    def has_gaps(self) -> bool:
        return self.missing_count > 0


def group(files: list[ScannedFile]) -> list[Work]:
    """Regroupe des fichiers scannes en oeuvres.

    La cle combine type et titre normalise : deux series homonymes de types
    differents ne doivent pas fusionner, et « Severance » ecrit avec ou sans
    majuscules designe la meme oeuvre.
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

        if kind == "movie":
            continue

        season_number = parsed.season if parsed.season is not None else 0
        season = work.seasons.setdefault(season_number, SeasonHolding(number=season_number))
        episode = parsed.episode if parsed.episode is not None else parsed.absolute_episode
        if episode is not None:
            season.owned.add(episode)

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
