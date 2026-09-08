"""Fichiers qui accompagnent une video, et restes a evacuer.

Deplacer la seule video est une perte de donnees silencieuse : les sous-titres
restent en arriere, et Jellyfin affiche un film sans VOSTFR. Personne ne s'en
apercoit avant de lancer la lecture.

Deux notions distinctes, deliberement separees :

- **Compagnons** : ce qui appartient au fichier et doit le suivre en etant
  renomme comme lui. Operation sure et reversible.
- **Restes** : ce qui n'a plus de raison d'etre une fois la video partie. Rien
  n'est jamais supprime — les restes vont dans une corbeille datee, et c'est
  l'utilisateur qui la vide quand il a verifie.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".smi"}
ARTWORK_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tbn"}

# Jaquettes nommees par convention plutot que d'apres la video.
ARTWORK_NAMES = {
    "poster",
    "folder",
    "cover",
    "fanart",
    "banner",
    "thumb",
    "landscape",
    "clearlogo",
}

# Restes typiques d'une release. La liste est volontairement CONSERVATRICE :
# tout ce qui n'y figure pas est laisse en place. Mieux vaut oublier un dechet
# que deplacer par erreur quelque chose qui comptait.
JUNK_EXTENSIONS = {".txt", ".url", ".nfo", ".sfv", ".md5", ".exe", ".lnk", ".diz"}
JUNK_NAMES = {"rarbg", "readme", "downloaded from", "torrent downloaded", "www", "screens"}

TRASH_DIRNAME = ".sortilege-corbeille"


class CompanionKind(StrEnum):
    SUBTITLE = "subtitle"
    ARTWORK = "artwork"


@dataclass(slots=True)
class Companion:
    """Un fichier qui suit la video, avec le suffixe a preserver.

    ``suffix`` porte tout ce qui distingue ce compagnon : « .fr.srt »,
    « .eng.forced.srt », « -poster.jpg ». Le renommage remplace le radical par
    celui de la video et conserve ce suffixe — sans quoi deux pistes de
    sous-titres se recouvriraient.
    """

    path: Path
    kind: CompanionKind
    suffix: str

    def destination_for(self, video_destination: Path) -> Path:
        return video_destination.with_name(video_destination.stem + self.suffix)


def _subtitle_suffix(video_stem: str, companion: Path) -> str | None:
    """Extrait « .fr.srt » de « Film.fr.srt » quand la video est « Film.mkv »."""
    name = companion.name
    if not name.lower().startswith(video_stem.lower()):
        return None
    return name[len(video_stem) :]


def find_companions(video: Path, *, artwork: bool = True) -> list[Companion]:
    """Fichiers a emporter avec la video.

    Deux conventions couvrent l'essentiel : meme radical que la video (« Film.
    fr.srt »), ou nom canonique dans le dossier (« poster.jpg »). Les jaquettes
    canoniques ne sont prises que si la video est seule dans son dossier —
    sinon « poster.jpg » appartiendrait a plusieurs videos a la fois et on le
    deplacerait avec la premiere venue.
    """
    if not video.parent.is_dir():
        return []

    stem = video.stem
    found: list[Companion] = []
    videos_in_dir = 0

    try:
        siblings = sorted(video.parent.iterdir())
    except OSError as exc:
        logger.warning("lecture du dossier impossible (%s)", exc)
        return []

    for sibling in siblings:
        if not sibling.is_file():
            continue
        if sibling == video:
            continue

        extension = sibling.suffix.lower()

        if extension in SUBTITLE_EXTENSIONS:
            suffix = _subtitle_suffix(stem, sibling)
            if suffix:
                found.append(Companion(sibling, CompanionKind.SUBTITLE, suffix))
            continue

        if artwork and extension in ARTWORK_EXTENSIONS:
            suffix = _subtitle_suffix(stem, sibling)
            if suffix:
                found.append(Companion(sibling, CompanionKind.ARTWORK, suffix))
            elif sibling.stem.lower() in ARTWORK_NAMES:
                # Nom canonique : on retient le nom tel quel, il sera resolu
                # plus bas une fois le nombre de videos connu.
                found.append(
                    Companion(sibling, CompanionKind.ARTWORK, f"-{sibling.stem.lower()}{extension}")
                )

    for sibling in siblings:
        if sibling.is_file() and sibling.suffix.lower() in {".mkv", ".mp4", ".avi", ".m4v", ".ts"}:
            videos_in_dir += 1

    if videos_in_dir > 1:
        # Le dossier contient plusieurs videos : une jaquette canonique ne
        # designe personne en particulier, on ne l'emporte pas.
        found = [
            c
            for c in found
            if c.kind is not CompanionKind.ARTWORK or c.path.stem.lower() not in ARTWORK_NAMES
        ]

    return found


def _is_junk(path: Path) -> bool:
    lowered = path.stem.lower()
    if path.suffix.lower() in JUNK_EXTENSIONS:
        return True
    return any(needle in lowered for needle in JUNK_NAMES)


def find_leftovers(video: Path, companions: list[Companion]) -> list[Path]:
    """Restes evacuables une fois la video et ses compagnons partis.

    Ne renvoie QUE des dechets reconnus. Un fichier inconnu reste en place :
    l'oubli d'un dechet coute un peu de desordre, l'evacuation d'un fichier qui
    comptait coute bien plus.
    """
    if not video.parent.is_dir():
        return []

    taken = {c.path for c in companions} | {video}
    leftovers: list[Path] = []

    try:
        siblings = sorted(video.parent.iterdir())
    except OSError:
        return []

    for sibling in siblings:
        if sibling in taken or not sibling.is_file():
            continue
        if _is_junk(sibling):
            leftovers.append(sibling)

    return leftovers


def trash_destination(trash_root: Path, batch: str, original: Path) -> Path:
    """Emplacement d'un reste dans la corbeille.

    Le chemin d'origine est aplati dans le nom : la corbeille reste plate et
    lisible, et on voit d'ou venait chaque fichier sans avoir a recreer une
    arborescence.
    """
    flat = re.sub(r"[/\\]+", "_", str(original).lstrip("/"))
    return trash_root / batch / flat


def directory_now_empty(directory: Path) -> bool:
    """Le dossier ne contient-il plus rien d'exploitable ?

    Les fichiers systeme d'un NAS (.DS_Store, @eaDir) ne comptent pas : les
    considerer comme du contenu empecherait de reconnaitre un dossier
    reellement vide.
    """
    if not directory.is_dir():
        return False
    try:
        for entry in directory.iterdir():
            if entry.name.startswith(".") or entry.name in {"@eaDir", ".@__thumb"}:
                continue
            return False
    except OSError:
        return False
    return True
