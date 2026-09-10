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

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .parser import VIDEO_EXTENSIONS

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


def trash_root_for(source: Path, library_root: Path, source_roots: list[Path]) -> Path:
    """Corbeille situee sur le MEME systeme de fichiers que le fichier a evacuer.

    Ce n'est pas un detail de rangement, c'est un facteur mille sur le temps.
    Un deplacement a l'interieur d'un volume est un simple renommage,
    instantane quelle que soit la taille. D'un volume a l'autre, il faut
    recopier puis supprimer : sur un NAS ou telechargements et bibliotheque
    sont deux partages distincts, evacuer trois cents fichiers revenait a
    recopier plusieurs centaines de gigaoctets pour ne rien produire.

    On cherche donc, parmi les racines connues, celle qui contient le fichier
    et se trouve sur son volume. La bibliotheque ne sert que de dernier
    recours — mieux vaut une copie lente qu'un refus.
    """
    try:
        device = source.stat().st_dev
    except OSError:
        return library_root / TRASH_DIRNAME

    def same_volume(root: Path) -> bool:
        try:
            return root.is_dir() and root.stat().st_dev == device
        except OSError:
            return False

    # La racine qui CONTIENT le fichier d'abord : c'est la plus specifique, et
    # la corbeille y reste visible a cote de ce dont elle provient.
    for root in source_roots:
        if same_volume(root) and source.is_relative_to(root):
            return root / TRASH_DIRNAME

    for root in [library_root, *source_roots]:
        if same_volume(root):
            return root / TRASH_DIRNAME

    return library_root / TRASH_DIRNAME


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


MAX_NAME_BYTES = 200
"""Plafond du nom aplati, en OCTETS.

La limite des systemes de fichiers courants est de 255 octets par composant —
pas par caractere, ce qui compte des qu'un titre porte des accents. On garde
une marge : le nom traverse ensuite des couches (SMB, sauvegardes) dont les
limites sont parfois plus basses."""


def trash_destination(trash_root: Path, batch: str, original: Path) -> Path:
    """Emplacement d'un reste dans la corbeille.

    Le chemin d'origine est aplati dans le nom : la corbeille reste plate et
    lisible, et on voit d'ou venait chaque fichier sans avoir a recreer une
    arborescence.

    Ce nom est BORNE. Une release 4K au titre a rallonge produisait un nom de
    plus de deux cent cinquante octets, et l'evacuation echouait sur
    « File name too long » — un echec d'autant plus deroutant qu'il ressemble a
    un probleme de place.

    Quand il faut couper, on garde la FIN : elle porte le nom reel du fichier,
    celui qui permet de le reconnaitre. Le debut, lui, n'est que le chemin des
    dossiers parents. Une empreinte du chemin complet est prefixee pour que
    deux fichiers tronques au meme endroit ne se recouvrent pas.
    """
    flat = re.sub(r"[/\\]+", "_", str(original).lstrip("/"))

    if len(flat.encode("utf-8")) > MAX_NAME_BYTES:
        empreinte = hashlib.blake2s(str(original).encode("utf-8"), digest_size=4).hexdigest()
        garde = MAX_NAME_BYTES - len(empreinte) - 1
        # Decoupe sur les OCTETS puis on ignore un eventuel caractere coupe en
        # deux : tronquer au milieu d'un accent produirait un nom invalide.
        queue = flat.encode("utf-8")[-garde:].decode("utf-8", errors="ignore")
        flat = f"{empreinte}_{queue}"

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


def _sans_interet(nom: str) -> bool:
    """Un fichier systeme du NAS ne fait pas d'un dossier un dossier occupe."""
    return nom.startswith(".") or nom in {"@eaDir", ".@__thumb"}


def find_empty_dirs(roots: list[Path]) -> list[Path]:
    """Dossiers vides sous les racines donnees, les plus profonds d'abord.

    Le nettoyage a la volee ne rattrape que ce qu'il vient de vider. Une
    bibliotheque constituee garde donc les carcasses des rangements anterieurs,
    et le client de telechargement en cree de son cote — liens abandonnes,
    extractions ratees.

    Le parcours est REMONTANT, les feuilles avant les branches : un dossier qui
    ne contient que des dossiers vides est vide lui aussi, et ne serait jamais
    vu autrement. « Serie/Serie S01E01/ » compte ainsi pour deux.

    Une racine n'est jamais renvoyee, meme vide : la supprimer ferait echouer
    le scan suivant sur un dossier absent.
    """
    interdits = {r.resolve() for r in roots}
    condamnes: set[Path] = set()

    for root in roots:
        if not root.is_dir():
            continue
        for courant, sous_dossiers, fichiers in os.walk(root, topdown=False):
            chemin = Path(courant)
            try:
                if chemin.resolve() in interdits:
                    continue
            except OSError:
                continue

            # Un seul vrai fichier suffit a le garder.
            if any(not _sans_interet(nom) for nom in fichiers):
                continue
            # Et tous ses sous-dossiers doivent eux-memes etre condamnes.
            if any(chemin / nom not in condamnes for nom in sous_dossiers):
                continue

            condamnes.add(chemin)

    # Les plus profonds d'abord : c'est l'ordre dans lequel il faut supprimer.
    return sorted(condamnes, key=lambda c: (-len(c.parts), str(c)))


# Ce qui accompagne une video sans jamais la remplacer : jaquettes,
# metadonnees, sous-titres, sommes de controle, restes d'extraction. Aucun de
# ces fichiers n'a de valeur seul — ils decrivent ou completent une video qui
# n'est plus la.
ORPHAN_EXTENSIONS = {
    *ARTWORK_EXTENSIONS,
    ".nfo",
    ".xml",
    ".txt",
    ".srt",
    ".sub",
    ".idx",
    ".ass",
    ".ssa",
    ".sfv",
    ".md5",
    ".url",
    ".log",
    ".part",
    ".!ut",
}


@dataclass(frozen=True, slots=True)
class OrphanDir:
    """Un dossier qui ne contient plus de video, seulement ses accessoires."""

    path: Path
    files: list[Path]
    bytes: int


def find_orphan_dirs(roots: list[Path]) -> list[OrphanDir]:
    """Dossiers sans aucune video, ne contenant que ses accessoires.

    Ranger un film emporte la video et ses compagnons, mais un dossier de
    release contient souvent des fichiers qui n'accompagnent RIEN : une
    jaquette au nom de la release, un .nfo, un .xml de metadonnees, des
    sommes de controle. Ils restent, et le dossier n'est donc jamais vide au
    sens strict — il n'est plus qu'une coquille.

    Le critere est simple et volontairement strict : aucune video, et RIEN
    d'autre que des accessoires connus. Un dossier contenant une archive, un
    document ou un fichier d'un type inattendu n'est pas propose — mieux vaut
    laisser un residu que supprimer ce qu'on n'a pas su reconnaitre.
    """
    interdits = {r.resolve() for r in roots}
    orphelins: list[OrphanDir] = []

    for root in roots:
        if not root.is_dir():
            continue
        for courant, _, fichiers in os.walk(root):
            chemin = Path(courant)
            try:
                if chemin.resolve() in interdits:
                    continue
            except OSError:
                continue

            reels = [f for f in fichiers if not _sans_interet(f)]
            if not reels:
                continue  # vide au sens strict : c'est l'autre balayage

            extensions = {Path(f).suffix.lower() for f in reels}
            if extensions & VIDEO_EXTENSIONS:
                continue  # il reste une video : on n'y touche pas
            if not extensions <= ORPHAN_EXTENSIONS:
                continue  # quelque chose d'inattendu : on s'abstient

            chemins = [chemin / f for f in reels]
            taille = 0
            for f in chemins:
                try:
                    taille += f.stat().st_size
                except OSError:
                    continue
            orphelins.append(OrphanDir(path=chemin, files=chemins, bytes=taille))

    return sorted(orphelins, key=lambda o: str(o.path))
