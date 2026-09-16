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

import errno
import hashlib
import logging
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .ebook import BOOK_EXTENSIONS
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


def free_trash_destination(trash_root: Path, original: Path) -> Path:
    """Un emplacement LIBRE dans le lot du jour de la corbeille. Ne cree rien.

    Le nom calcule par ``trash_destination`` est deterministe : deux passages
    le meme jour sur le meme chemin produiraient le meme nom, et le second
    ecraserait — ou ferait supprimer pour faire place — ce que le premier avait
    mis a l'abri, c'est-a-dire souvent l'original qu'on voulait garder. Un nom
    deja pris recoit donc un numero d'ordre.

    Le deplacement reste a l'appelant, avec sa propre primitive et son propre
    refus d'ecraser.
    """
    lot = datetime.now(UTC).strftime("%Y-%m-%d")
    base = trash_destination(trash_root, lot, original)
    cible = base
    rang = 1
    while cible.exists() or cible.is_symlink():
        rang += 1
        cible = base.with_name(f"{rang}_{base.name}")
    return cible


def send_to_trash(path: Path, trash_root: Path) -> Path:
    """Met un fichier en corbeille, dans le lot du jour, et rend ou il est parti.

    Ne remplace JAMAIS un fichier deja en corbeille (voir
    ``free_trash_destination``). Leve ``OSError`` si le deplacement echoue :
    l'appelant sait s'il doit renoncer a ecrire par-dessus.
    """
    cible = free_trash_destination(trash_root, path)
    cible.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(path, cible)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Corbeille sur un autre volume que la bibliotheque : copie puis
        # suppression de l'original, une fois la copie achevee.
        shutil.move(str(path), str(cible))
    return cible


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
# Jamais de marqueur de telechargement en cours (.part, .!ut...) : un dossier
# qui n'en contient qu'un est un transfert vivant, pas une coquille.
ORPHAN_EXTENSIONS = {
    *ARTWORK_EXTENSIONS,
    ".nfo",
    # Manifeste de livre depose par Sortilege ou Calibre : sans le livre, il
    # ne decrit plus rien, exactement comme un .nfo sans sa video.
    ".opf",
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

    « Aucune video » s'entend dans le dossier ET dans tout ce qu'il contient.
    Une serie vivante range ses episodes dans « Season 01/ » et garde a sa
    racine « tvshow.nfo » et ses affiches : ne regarder que les fichiers du
    dossier courant la faisait passer pour une coquille, proposee au
    nettoyage avec la fiche qui porte son identite. Le parcours est donc
    REMONTANT, les sous-dossiers vus avant leur parent.
    """
    interdits = {r.resolve() for r in roots}
    orphelins: list[OrphanDir] = []
    # Dossiers contenant une video, eux-memes ou dans leur descendance.
    habites: set[Path] = set()
    # Dossiers effectivement parcourus. Parcours remontant : un sous-dossier est
    # toujours livre avant son parent, donc un sous-dossier absent de cet
    # ensemble n'a PAS pu etre parcouru (illisible, ou lien symbolique).
    parcourus: set[Path] = set()

    for root in roots:
        if not root.is_dir():
            continue
        for courant, sous_dossiers, fichiers in os.walk(root, topdown=False):
            parcourus.add(Path(courant))
            # La corbeille ne contient aucune video : sans cette exception elle
            # passait pour une coquille, et le mode « supprimer » du nettoyage
            # detruisait definitivement ce qu'elle gardait a l'abri.
            if TRASH_DIRNAME in Path(courant).parts:
                continue
            chemin = Path(courant)
            reels = [f for f in fichiers if not _sans_interet(f)]
            extensions = {Path(f).suffix.lower() for f in reels}

            # Marque AVANT tout autre test. Un sous-dossier non parcouru —
            # illisible, ou lien symbolique que os.walk ne suit pas — compte comme
            # habite : on ne sait pas ce qu'il contient, et le prendre pour vide
            # ferait proposer au nettoyage la fiche d'une serie vivante.
            if extensions & VIDEO_EXTENSIONS or any(
                (chemin / d) in habites or (chemin / d) not in parcourus for d in sous_dossiers
            ):
                habites.add(chemin)
                continue  # il reste une video, ici ou plus bas : on n'y touche pas

            try:
                if chemin.resolve() in interdits:
                    continue
            except OSError:
                continue

            if not reels:
                continue  # vide au sens strict : c'est l'autre balayage

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


# --- Fichiers annexes de la bibliotheque -------------------------------------

EXTRA_CATEGORIES: tuple[str, ...] = ("trickplay", "images", "fiches", "sous_titres", "autres")
"""Categories proposees au nettoyage, dans l'ordre ou elles s'affichent."""

FICHE_EXTENSIONS = {".nfo", ".opf"}

# Principe de surete : on ne propose que ce qu'on sait reconnaitre. Chaque
# categorie est une liste FERMEE. Un fichier qu'aucune ne retient n'est jamais
# propose : il est compte a part (``unknown``), pour que l'ecran dise ce qu'il
# laisse en place. « autres » a ete un fourre-tout — tout ce qui n'etait ni
# image, ni fiche, ni sous-titre — et une passe « supprimer » y a detruit des
# videos .ogm, des pistes TrueHD, des livres .azw et une base Calibre.

# Videos que ``VIDEO_EXTENSIONS`` ne range pas, mais qui restent des videos : les
# proposer ferait supprimer un film d'un seul geste. Les images de disque (.iso,
# .img, .bin, .mdf, .nrg) en font partie : ce sont le plus souvent des DVD.
_AUTRES_VIDEOS = {
    ".webm",
    ".flv",
    ".f4v",
    ".ogv",
    ".ogm",
    ".ogx",
    ".3gp",
    ".3g2",
    ".vob",
    ".evo",
    ".divx",
    ".xvid",
    ".rm",
    ".rmvb",
    ".asf",
    ".mts",
    ".m2t",
    ".m2v",
    ".m1v",
    ".mp2v",
    ".mpe",
    ".mpv",
    ".mk3d",
    ".trp",
    ".tp",
    ".mod",
    ".tod",
    ".dv",
    ".mxf",
    ".wtv",
    ".dvr-ms",
    ".vro",
    ".rec",
    ".qt",
    ".h264",
    ".264",
    ".hevc",
    ".y4m",
    ".amv",
    ".nsv",
    ".iso",
    ".img",
    ".bin",
    ".mdf",
    ".nrg",
}

_VIDEOS = VIDEO_EXTENSIONS | _AUTRES_VIDEOS

# Film decoupe en morceaux (« Film.mkv.001 », « Film.mkv.002 ») : chaque morceau
# est une partie de la video, pas un reste.
_MORCEAU_DE_VIDEO = re.compile(
    r"\.(?:" + "|".join(re.escape(e[1:]) for e in sorted(_VIDEOS)) + r")\.\d{2,3}$",
    re.IGNORECASE,
)

# Pistes audio : une piste .mka ou .thd posee a cote d'un film est son doublage,
# et une bibliotheque peut abriter des livres audio. Rien de tout cela n'est un
# reste.
AUDIO_EXTENSIONS = {
    ".mka",
    ".mp3",
    ".mp2",
    ".mpa",
    ".mpc",
    ".flac",
    ".m4a",
    ".m4b",
    ".m4r",
    ".aac",
    ".ac3",
    ".eac3",
    ".dts",
    ".dtshd",
    ".thd",
    ".truehd",
    ".mlp",
    ".ogg",
    ".oga",
    ".opus",
    ".spx",
    ".weba",
    ".wav",
    ".wma",
    ".aiff",
    ".aif",
    ".ape",
    ".wv",
    ".alac",
    ".tta",
    ".dsf",
    ".dff",
    ".amr",
    ".caf",
    ".aax",
    ".aa",
}

# Livres que ``BOOK_EXTENSIONS`` ne range pas (le scan ne sait pas les lire),
# mais qui restent des livres. Un .txt ou un .doc est un livre possible : dans le
# doute, il est protege.
_AUTRES_LIVRES = {
    ".azw",
    ".kfx",
    ".kepub",
    ".prc",
    ".pdb",
    ".lit",
    ".lrf",
    ".ibooks",
    ".chm",
    ".djv",
    ".xps",
    ".cb7",
    ".cbt",
    ".cba",
    ".rtf",
    ".doc",
    ".docx",
    ".odt",
    ".txt",
}

_LIVRES = BOOK_EXTENSIONS | _AUTRES_LIVRES

# Arborescences de disque (DVD, Blu-ray, HD DVD, AVCHD) : leurs fichiers ne sont
# pas des annexes mais la structure qui rend la video lisible.
_DOSSIERS_DE_DISQUE = {"video_ts", "audio_ts", "bdmv", "certificate", "hvdvd_ts"}

# Les memes fichiers, quand l'arborescence a ete aplatie (« DVD/VTS_01_0.IFO »
# sans dossier VIDEO_TS) : proteges ou qu'ils soient.
_FICHIERS_DE_DISQUE = {".ifo", ".bup", ".bdmv", ".mpls", ".clpi", ".evo"}

# Corbeilles, vignettes et dossiers techniques des NAS et des systemes, en plus
# de tout dossier cache. Compares sans casse et espaces normalises (voir
# ``_nom_de_dossier``).
_DOSSIERS_SYSTEME = {
    "@eadir",
    "@__thumb",
    "@synoresource",
    "#recycle",
    "#snapshot",
    "@recycle",
    "@recycle.bin",
    "@recently-snapshot",
    "$recycle.bin",
    "system volume information",
    "lost+found",
    "network trash folder",
    "temporary items",
}

# Dechets RECONNUS, seuls admis dans « autres ». Aucun format ambigu : .txt peut
# etre un livre, .xml un « ComicInfo.xml », .bak l'original qu'une fiche a
# remplace, .log le journal d'extraction d'un CD (EAC) qu'on ne refait pas sans
# le disque. Sommes de controle, raccourcis, fichiers de parite et d'index de
# release : rien qui ait de valeur une fois la video en place.
DECHETS_EXTENSIONS = frozenset(
    {
        ".sfv",
        ".md5",
        ".sha1",
        ".sha256",
        ".url",
        ".lnk",
        ".nzb",
        ".torrent",
        ".par2",
        ".srr",
        ".srs",
    }
)

# Fichiers systeme reconnus par leur NOM, compares sans casse.
DECHETS_NOMS = frozenset({"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"})

# Cache de Jellyfin : « <video>.trickplay/<largeur> - <n>x<n>/<i>.jpg ». Il le
# regenere, c'est le seul endroit ou un fichier non reconnu peut etre propose.
_MARQUE_TRICKPLAY = ".trickplay"

UNKNOWN_SAMPLES = 8
"""Exemples de fichiers inconnus gardes pour l'ecran."""


@dataclass(slots=True)
class ExtraCategory:
    """Les fichiers d'une categorie, dans l'ordre du parcours."""

    files: list[Path] = field(default_factory=list)
    bytes: int = 0


@dataclass(slots=True)
class Extras:
    """Inventaire des fichiers annexes sous la racine d'une bibliotheque."""

    root: Path
    categories: dict[str, ExtraCategory]
    videos: int = 0
    books: int = 0
    audio: int = 0
    unknown: int = 0
    """Fichiers qu'aucune liste ne reconnait : jamais proposes."""
    unknown_samples: list[Path] = field(default_factory=list)
    skipped_dirs: int = 0
    """Dossiers illisibles sautes : le releve est alors incomplet."""

    @property
    def total_bytes(self) -> int:
        return sum(c.bytes for c in self.categories.values())


def _nom_de_dossier(nom: str) -> str:
    """Nom compare sans casse, blancs normalises (espace insecable compris)."""
    return " ".join(nom.split()).casefold()


def _dans_un_disque(dossiers: tuple[str, ...]) -> bool:
    return any(_nom_de_dossier(d) in _DOSSIERS_DE_DISQUE for d in dossiers)


def _dans_un_trickplay(dossiers: tuple[str, ...]) -> bool:
    return any(_MARQUE_TRICKPLAY in d.casefold() for d in dossiers)


def classify_extra(relatif: Path) -> str:
    """Nature d'un fichier, d'apres son chemin RELATIF a la racine.

    Rend une categorie de ``EXTRA_CATEGORIES``, ou une famille jamais proposee :
    « videos », « books », « audio » (comptees), « disque » et « cache »
    (structure de disque, fichier cache), « unknown » (non reconnu).

    Le chemin doit etre relatif : un nom de racine comme « /srv/x.trickplay »
    ferait sinon basculer toute la bibliotheque dans la categorie trickplay.

    L'ordre est celui de la surete. Les protections passent AVANT tout : une
    video rangee par erreur dans un dossier .trickplay reste une video. Seuls
    les fichiers caches passent devant, et ils ne sont jamais proposes non plus
    (sauf « .DS_Store », dechet reconnu) : un « ._Film.mkv » de macOS n'est pas
    une video de plus a compter.
    """
    nom = relatif.name
    bas = nom.casefold()
    dossiers = relatif.parts[:-1]
    extension = relatif.suffix.casefold()

    if _sans_interet(nom) and bas not in DECHETS_NOMS:
        return "cache"
    if extension in _VIDEOS or _MORCEAU_DE_VIDEO.search(bas):
        return "videos"
    if extension in _LIVRES:
        return "books"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension in _FICHIERS_DE_DISQUE or _dans_un_disque(dossiers):
        return "disque"

    trickplay = _dans_un_trickplay(dossiers)
    if bas in DECHETS_NOMS:
        return "trickplay" if trickplay else "autres"
    if trickplay:
        return "trickplay"
    if extension in ARTWORK_EXTENSIONS:
        return "images"
    if extension in FICHE_EXTENSIONS:
        return "fiches"
    if extension in SUBTITLE_EXTENSIONS:
        return "sous_titres"
    if extension in DECHETS_EXTENSIONS:
        return "autres"
    return "unknown"


def extra_category(relatif: Path) -> str | None:
    """Categorie proposee d'un fichier (chemin relatif), ou None : jamais propose."""
    nature = classify_extra(relatif)
    return nature if nature in EXTRA_CATEGORIES else None


def _dossier_ignore(nom: str) -> bool:
    return nom.startswith(".") or _nom_de_dossier(nom) in _DOSSIERS_SYSTEME or nom == TRASH_DIRNAME


def find_extras(root: Path) -> Extras:
    """Les fichiers annexes RECONNUS sous la bibliotheque, par categorie.

    Les affiches, fiches et sous-titres deposes a cote de chaque media finissent
    par peser : quelques centaines de Mo sur une grosse bibliotheque. Ce
    parcours les classe pour que l'utilisateur choisisse ce qu'il retire.

    Ne sont JAMAIS proposes :

    - les videos, livres et pistes audio — comptes a part, pour que l'ecran
      dise ce qui est protege et pas seulement ce qui partira ;
    - tout fichier qu'aucune liste ne reconnait — compte dans ``unknown``, avec
      quelques exemples ;
    - la corbeille, les dossiers caches et les dossiers systeme ;
    - les structures de disque (VIDEO_TS, BDMV, HVDVD_TS, et les .ifo, .bup,
      .bdmv, .mpls, .clpi, .evo ou qu'ils soient) ;
    - les liens symboliques, dossiers comme fichiers : un lien peut designer un
      fichier HORS de la bibliotheque, et le mettre en corbeille ou le
      supprimer toucherait ce qu'on n'a jamais demande a ranger. Seuls les
      fichiers ordinaires sont retenus.

    Un dossier illisible est saute et compte (``skipped_dirs``), et le parcours
    continue.
    """
    extras = Extras(root=root, categories={cle: ExtraCategory() for cle in EXTRA_CATEGORIES})
    if not root.is_dir():
        return extras

    def illisible(exc: OSError) -> None:
        extras.skipped_dirs += 1
        logger.warning("dossier illisible ignore pendant l'inventaire (%s)", exc.strerror)

    for courant, sous_dossiers, fichiers in os.walk(root, onerror=illisible):
        chemin = Path(courant)
        # Elagage sur place : os.walk ne descend que dans ce qui reste. Il ne
        # suit pas les liens symboliques, mais les liste parmi les dossiers.
        sous_dossiers[:] = sorted(
            d for d in sous_dossiers if not _dossier_ignore(d) and not (chemin / d).is_symlink()
        )
        base = chemin.relative_to(root)

        for nom in sorted(fichiers):
            fichier = chemin / nom
            nature = classify_extra(base / nom)
            if nature == "videos":
                extras.videos += 1
                continue
            if nature == "books":
                extras.books += 1
                continue
            if nature == "audio":
                extras.audio += 1
                continue
            if nature == "unknown":
                extras.unknown += 1
                if len(extras.unknown_samples) < UNKNOWN_SAMPLES:
                    extras.unknown_samples.append(fichier)
                continue
            if nature not in EXTRA_CATEGORIES:
                continue  # structure de disque, fichier cache
            try:
                info = fichier.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue  # lien symbolique, tube, socket : jamais propose
            categorie = extras.categories[nature]
            categorie.files.append(fichier)
            categorie.bytes += info.st_size

    return extras
