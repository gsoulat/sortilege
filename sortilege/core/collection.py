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
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from . import quality
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

    strategy_key: str = ""
    """Strategie retenue pour departager. Vide = qualite maximale, l'ancien
    comportement code en dur."""

    @property
    def rank(self) -> tuple[int, int]:
        """Rang selon la strategie choisie pour ce type d'oeuvre.

        C'etait une regle codee en dur — la plus haute resolution gagne, puis le
        plus gros fichier. C'est une preference, pas une verite : un remux 4K
        n'a pas la meme valeur pour qui archive un film et pour qui garde deux
        cents episodes sur un NAS.
        """
        return quality.strategy(self.strategy_key).rank(self.resolution, self.size_bytes)


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
        label = f"S{parsed.season:02d}E{parsed.episode:02d}"
        if parsed.episode_end is not None:
            # Un fichier double n'occupe pas la meme place qu'un fichier
            # simple : les confondre les declarerait doublons a tort.
            label += f"-E{parsed.episode_end:02d}"
        return label
    if parsed.absolute_episode is not None:
        return f"ep {parsed.absolute_episode}"
    # Sans numero, impossible de savoir si deux fichiers occupent la meme
    # place : on leur donne des cles distinctes plutot que de les declarer
    # doublons a tort.
    return f"?{scanned.relative_path}"


def group(files: list[ScannedFile], reglages: quality.QualitySettings | None = None) -> list[Work]:
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
            # La strategie suit le TYPE de l'oeuvre : la meilleure image pour
            # les films, la plus petite empreinte pour les series.
            strategy_key=getattr(reglages, kind, "") if reglages else "",
        )
        work.slots.setdefault(_slot_label(scanned), []).append(reference)

        if kind == "movie":
            continue

        season_number = parsed.season if parsed.season is not None else 0
        season = work.seasons.setdefault(season_number, SeasonHolding(number=season_number))
        episode = parsed.episode if parsed.episode is not None else parsed.absolute_episode
        if episode is not None:
            # Un fichier double couvre toute la plage : ne compter que le
            # premier numero ferait apparaitre le second comme manquant alors
            # qu'il est bien la.
            end = parsed.episode_end if parsed.episode_end is not None else episode
            season.owned.update(range(episode, end + 1))

    for work in works.values():
        work.duplicates = [
            DuplicateGroup(label=label, files=refs)
            for label, refs in sorted(work.slots.items())
            if len(refs) > 1
        ]

    return sorted(works.values(), key=lambda w: (w.kind, w.title.casefold()))


_SLOT_EPISODE = re.compile(r"^S(\d+)E(\d+)(?:-E(\d+))?$")
_SLOT_ABSOLU = re.compile(r"^ep (\d+)$")


def _recalcule_saisons(work: Work) -> None:
    """Refait ce qu'on possede d'une serie a partir des emplacements restants.

    Sans cela, retirer le seul fichier d'un episode laissait la saison se dire
    complete : l'episode avait disparu du disque, et l'ecran continuait de
    l'annoncer possede. Les emplacements portent le numero — « S01E02 »,
    « S01E01-E03 » pour un fichier double, « ep 12 » en numerotation absolue —,
    donc ce qui reste se relit sans toucher au disque.

    Ce que le fournisseur CONNAIT est conserve : il ne depend pas de nos
    fichiers. Une saison dont aucun fichier ne porte de numero est gardee telle
    quelle — rien dans les emplacements ne permettrait de la reconstruire, et
    la faire disparaitre serait pire que de la laisser.
    """
    if work.kind == "movie":
        return

    saisons = {numero: s for numero, s in work.seasons.items() if not s.owned}
    for label in work.slots:
        if trouve := _SLOT_EPISODE.match(label):
            numero, debut = int(trouve[1]), int(trouve[2])
            fin = int(trouve[3]) if trouve[3] else debut
        elif trouve := _SLOT_ABSOLU.match(label):
            numero = 0
            debut = fin = int(trouve[1])
        else:
            continue
        saison = saisons.get(numero)
        if saison is None:
            ancienne = work.seasons.get(numero)
            saison = SeasonHolding(number=numero, known=ancienne.known if ancienne else {})
            saisons[numero] = saison
        saison.owned.update(range(debut, fin + 1))
    work.seasons = saisons


def forget_files(works: list[Work], chemins: Iterable[str]) -> list[Work]:
    """Retire de l'index des fichiers qui viennent de quitter le disque.

    Sans cela, supprimer un doublon laissait l'ecran affirmer « 2 fichiers,
    1 doublon » jusqu'a la prochaine lecture complete de la bibliotheque : le
    geste avait bien eu lieu, mais rien ne le montrait, et le meme bouton
    reproposait de supprimer un fichier deja parti.

    Relire toute la bibliotheque apres chaque suppression serait exact mais
    couteux — plusieurs minutes sur un NAS. Ce qui est derivable des fichiers
    restants est donc recalcule ici : nombre, poids, doublons, et ce qu'on
    possede de chaque saison. Ce dernier point n'est pas theorique : les routes
    acceptent des chemins quelconques, pas seulement des doublons, et vider un
    emplacement laissait la saison se dire complete sans son episode.
    """
    partis = set(chemins)
    if not partis:
        return works

    restants: list[Work] = []
    for work in works:
        slots = {
            label: [f for f in refs if f.relative_path not in partis]
            for label, refs in work.slots.items()
        }
        work.slots = {label: refs for label, refs in slots.items() if refs}
        fichiers = [f for refs in work.slots.values() for f in refs]
        if not fichiers:
            continue
        work.file_count = len(fichiers)
        work.total_bytes = sum(f.size_bytes for f in fichiers)
        work.duplicates = [
            DuplicateGroup(label=label, files=refs)
            for label, refs in sorted(work.slots.items())
            if len(refs) > 1
        ]
        _recalcule_saisons(work)
        restants.append(work)
    return restants


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
