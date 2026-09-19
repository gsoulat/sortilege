"""Copier des oeuvres de la mediatheque vers un disque externe USB.

Le cas reel : un NAS (Asustor, Synology...) ou Sortilege tourne en conteneur,
et un disque USB qu'on branche pour emporter des films chez quelqu'un ou pour
garder une copie. On choisit des oeuvres, on choisit le disque, Sortilege dit
ce qui y est deja et copie le reste, UN fichier a la fois — le port USB d'un
NAS sature vite, et deux ecritures concurrentes sur un disque a plateaux
coutent plus cher qu'elles ne rapportent.

**La regle qui prime sur toutes les autres.** Quand un disque USB est
debranche, son point de montage reste un dossier VIDE sur la partition systeme
du NAS. Y copier 500 Go remplit cette partition, et le NAS tombe. Un dossier
n'est donc propose comme disque que s'il est un point de montage reel TROUVE A
L'INTERIEUR d'un dossier de la racine externe — le parent des disques, monte
en ``rslave`` : un disque debranche y disparait avec son montage. Un disque
monte DIRECTEMENT sur un dossier de la racine est refuse : debranche, son
montage reste, et plus rien ne le distingue d'un dossier vide qu'un tiers
aurait rempli (voir ``_disques_sous``). La copie reverifie en plus avant
CHAQUE fichier que le disque est toujours le meme volume monte (meme
``st_dev``, toujours un point de montage) : un disque arrache en cours de route
arrete toute la file au lieu de la faire continuer sur la partition systeme.

**L'identite d'un disque, pour la reprise.** Ni le point de montage ni le
numero de peripherique ne designent un disque : rebranche sur un autre port, le
meme disque change des deux, et un autre disque peut prendre sa place. Au
depart d'une copie, Sortilege pose donc a la racine du disque un fichier
``.sortilege-disque`` qui porte un identifiant aleatoire ; l'etat de reprise
l'enregistre, et une reprise ne part que vers le disque qui le porte.

Ce controle ne suffit pas seul : entre lui et l'ecriture, le disque peut
partir. La racine du disque est donc ouverte UNE fois, au depart, et verifiee
(``st_dev``) ; tout ce qui s'ecrit ensuite — dossiers, ``.sortilege-part``,
publication — passe par ce descripteur, composant par composant, sans jamais
suivre un lien. Un disque arrache ne laisse alors viser que le descripteur
d'un volume mort (EIO, ENOENT), jamais le dossier qui reste a sa place.

Trois autres regles, heritees d'un defaut deja corrige ailleurs dans
l'application (des copies interrompues prises pour des films complets) :

- la mediatheque n'est jamais modifiee — elle n'est ouverte qu'en lecture ;
- rien n'est jamais ecrase sur le disque : une destination qui existe est un
  conflit signale, jamais une cible ;
- un fichier n'apparait sous son nom definitif qu'une fois complet, verifie en
  taille et force sur le disque (``fsync``). Avant, il s'appelle
  ``<nom>.sortilege-part``.

Tout ``stat`` sur un volume externe est borne dans le temps : un disque USB qui
s'endort, ou un montage fige, ne doit jamais bloquer une requete.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections import Counter, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from ..identite import MontageFige, stat_avec_delai
from .collection import FileRef, Work, group
from .companions import find_companions
from .parser import _season_from_dirs, parse
from .proprietaire import rend_lisible_avant_publication, tourne_en_root
from .scanner import DEFAULT_RULES, SKIP_DIRS, ScanRules, scan

logger = logging.getLogger(__name__)

T = TypeVar("T")

# --- Constantes ---------------------------------------------------------------

SUFFIXE_PARTIEL = ".sortilege-part"
"""Nom d'un fichier en cours d'ecriture : ``<nom definitif>.sortilege-part``.

Reconnaissable a coup sur : un reste d'une copie interrompue (redemarrage du
conteneur, disque arrache) est a nous, et la copie suivante l'ecrase sans
hesiter. Aucun lecteur multimedia ne le prend pour une video."""

TAILLE_BLOC = 8 * 1024 * 1024
"""Taille d'une lecture/ecriture. Assez gros pour que le cout par appel soit
negligeable, assez petit pour qu'une pause ou un arret reponde en une fraction
de seconde meme sur un port USB 2 (~30 Mo/s)."""

LIMITE_FAT32 = 4 * 1024**3 - 1
"""Taille maximale d'un fichier sur FAT32 (``vfat``). Un film 4K la depasse
couramment : mieux vaut le dire a l'analyse qu'echouer a 99 %."""

MARGE_SOCLE = 64 * 1024**2
MARGE_PAR_FICHIER = 2 * 1024**2
"""Marge gardee libre : un socle de 64 Mio (dossiers, tables d'allocation, un
disque rempli a ras se monte parfois mal ailleurs) plus 2 Mio par fichier —
une grappe exFAT entamee coute jusqu'a 1 Mio, et chaque fichier en entame une,
compagnon compris. Voir ``marge`` : une marge proportionnelle au disque
refusait des copies qui tenaient largement sur les gros disques."""

SYSTEMES_FAT32 = frozenset({"vfat", "msdos"})
SYSTEMES_WINDOWS = frozenset({"vfat", "msdos", "exfat", "ntfs", "ntfs3", "fuseblk"})
"""Systemes de fichiers qui refusent ``\\ : * ? " < > |`` dans un nom.

``fuseblk`` est ce que rapporte le pilote ntfs-3g. Ce sont aussi ceux ou
``chmod``/``chown`` n'ont pas de sens : on ne tente pas de les appliquer
fichier par fichier, ce qui remplirait le journal d'avertissements."""

CARACTERES_INTERDITS = frozenset('\\:*?"<>|')

PROFONDEUR_MONTAGES = 2
"""Profondeur a laquelle on cherche des points de montage sous chaque dossier
de la racine externe : « USB1/usbshare » est a un niveau, certains NAS en
ajoutent un second."""

DELAI_VOLUME = 5.0
"""Secondes accordees a un appel sur un volume externe avant de le declarer
muet. Plus que ``identite.DELAI_STAT`` : un disque USB en veille met quelques
secondes a repartir, et le declarer absent a ce moment serait faux."""

MOUNTINFO = Path("/proc/self/mountinfo")
"""Table des montages vue du conteneur. Absente hors Linux : le systeme de
fichiers est alors inconnu (``None``), rien d'autre ne change."""

REFUS_MONTAGE_DIRECT = (
    "montage direct : Sortilège ne peut pas distinguer ce disque d'un dossier vide s'il est "
    "débranché. Monte le dossier PARENT des disques avec :rslave (voir README)."
)
REFUS_SANS_DISQUE = "aucun disque monté ici — débranché ?"
REFUS_VOLUME_MEDIATHEQUE = "c'est le volume de la médiathèque, pas un disque externe : refusé"
REFUS_LIEN = "lien symbolique sur le disque : refusé"

_DRAPEAUX_DOSSIER = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
"""Ouverture d'un dossier du disque : jamais a travers un lien symbolique."""

ERREURS_DU_DISQUE = frozenset(
    {
        errno.EIO,
        errno.ENODEV,
        errno.ENXIO,
        errno.EROFS,
        errno.ENOENT,
        errno.ESTALE,
        errno.ENOTCONN,
        errno.EBADF,
    }
)
"""Erreurs d'ecriture qui disent que le DISQUE ne suit plus, pas ce fichier.

Toute la file s'arrete dessus, point de controle garde. EIO/ENODEV/ENXIO : le
volume est parti. EROFS : un exFAT ou un NTFS qui a vu une erreur repasse en
lecture seule (reglage par defaut sous Linux) — continuer echouerait fichier
apres fichier et effacerait la reprise. ENOENT : un dossier qu'on tient
ouvert n'existe plus, ce qui n'arrive qu'a un volume demonte sous nos pieds.
ENOTCONN : le pilote ntfs-3g (FUSE) est mort avec le disque. ESTALE, EBADF :
le descripteur de la racine ne designe plus rien."""

MESURE_MINIMALE_DEBIT = 1.0
"""En dessous d'une seconde de mesure, aucun debit n'est annonce."""

FENETRE_FICHIER = 3.0
"""Fenetre du debit instantane du fichier en cours, en secondes. Glissante et
non depuis le debut du fichier : un demarrage lent (disque qui sort de veille)
fausserait sinon tout le reste de la barre."""

FENETRE_GLOBALE = 5.0
"""Fenetre du debit global, sur lequel se calcule le temps restant."""

MESURE_MINIMALE_ETA = 2.0
"""Pas de temps restant avant deux secondes de mesure : une estimation faite
sur un seul bloc annonce n'importe quoi, et c'est celle qu'on retient."""

DERNIERS_FAITS = 50
FILE_VISIBLE = 20

MARQUEUR = ".sortilege-disque"
"""Fichier pose a la racine d'un disque au depart d'une copie. Il porte un
identifiant aleatoire (UUID), la seule identite du disque qui survive a un
debranchement : voir ``marquer_disque``."""

_FORME_MARQUE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_SLOT_SAISON = re.compile(r"^S(\d+)E\d+")
_OCTAL = re.compile(r"\\([0-7]{3})")


# --- Appels bornes dans le temps ------------------------------------------------


def _borne(action: Callable[[], T], quoi: Path, delai: float | None = None) -> T:
    """Execute ``action`` dans un fil, sans l'attendre plus que ``delai``.

    Meme mecanique que ``identite.stat_avec_delai``, pour ce qui n'est pas un
    ``stat`` : lister un dossier, lire la place libre, tester un point de
    montage. Le fil est un demon : s'il reste coince dans un appel systeme, il
    ne retient ni la requete ni l'arret du processus.

    ``delai`` est resolu a l'appel, pour qu'un test puisse abaisser
    ``DELAI_VOLUME`` sans attendre.
    """
    if delai is None:
        delai = DELAI_VOLUME
    resultats: list[tuple[bool, Any]] = []

    def execute() -> None:
        try:
            resultats.append((True, action()))
        except Exception as exc:
            resultats.append((False, exc))

    fil = threading.Thread(target=execute, daemon=True, name=f"volume {quoi}")
    fil.start()
    fil.join(delai)
    if not resultats:
        raise MontageFige(
            errno.ETIMEDOUT, f"n'a pas répondu en {delai:g} s (montage figé ?)", str(quoi)
        )
    ok, valeur = resultats[0]
    if ok:
        return valeur
    raise valeur


class _Engagement:
    """Le point de non-retour d'une action bornee dans le temps.

    ``_borne`` rend la main a l'expiration du delai, mais son fil continue.
    Pour une lecture, c'est sans consequence ; pour une suppression, la requete
    repondait « non retiré » pendant que le fil, lui, finissait par supprimer —
    la reponse mentait, et l'etat efface ou garde sur cette foi aussi. Le fil
    demande donc la permission (``engager``) juste avant l'acte irreversible,
    et la requete qui renonce (``renoncer``) la lui retire, sous le meme verrou :
    quand on repond, l'acte est termine, ou il ne commencera jamais.
    """

    def __init__(self) -> None:
        self._verrou = threading.Lock()
        self._renonce = False
        self._engage = False

    def engager(self) -> bool:
        """Vrai si l'acte peut commencer : personne n'y a renonce."""
        with self._verrou:
            if not self._renonce:
                self._engage = True
            return self._engage

    def renoncer(self) -> bool:
        """Vrai si l'acte n'avait pas commence : il ne commencera plus."""
        with self._verrou:
            if not self._engage:
                self._renonce = True
            return self._renonce


def _borne_engage(action: Callable[[_Engagement], T], quoi: Path, delai: float | None = None) -> T:
    """Comme ``_borne``, pour une action qui ECRIT : jamais abandonnee en cours.

    Au-dela du delai : si l'action n'a pas atteint son point de non-retour, on y
    renonce et elle ne l'atteindra plus (``MontageFige``, rien n'a ete fait) ;
    si elle l'a atteint, on attend qu'elle finisse, sans limite — c'est le seul
    moyen de dire ce qu'elle a fait. Le point de non-retour suit des appels qui
    viennent de repondre sur le meme disque : l'attente est courte en pratique.
    """
    if delai is None:
        delai = DELAI_VOLUME
    engagement = _Engagement()
    resultats: list[tuple[bool, Any]] = []

    def execute() -> None:
        try:
            resultats.append((True, action(engagement)))
        except Exception as exc:
            resultats.append((False, exc))

    fil = threading.Thread(target=execute, daemon=True, name=f"volume {quoi}")
    fil.start()
    fil.join(delai)
    if not resultats and not engagement.renoncer():
        fil.join()
    if not resultats:
        raise MontageFige(
            errno.ETIMEDOUT,
            f"n'a pas répondu en {delai:g} s (montage figé ?) : rien n'a été fait",
            str(quoi),
        )
    ok, valeur = resultats[0]
    if ok:
        return valeur
    raise valeur


def _stat_volume(chemin: Path) -> os.stat_result:
    return stat_avec_delai(chemin, DELAI_VOLUME)


# --- Table des montages -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Montage:
    """Une ligne de ``/proc/self/mountinfo``, reduite a ce qui sert ici."""

    point: str
    fs: str
    source: str = ""
    racine: str = "/"


def _desechappe(texte: str) -> str:
    """Le noyau ecrit « \\040 » pour un espace dans un chemin de montage."""
    return _OCTAL.sub(lambda m: chr(int(m[1], 8)), texte)


def lire_montages(fichier: Path | None = None) -> dict[str, Montage]:
    """Point de montage -> montage, au mieux. Vide si la table est illisible.

    Ne sert qu'a nommer le systeme de fichiers (FAT32 limite la taille d'un
    fichier, exFAT refuse certains caracteres). La DETECTION d'un disque, elle,
    ne depend pas de cette table : hors Linux elle n'existe pas, et un disque
    ne doit pas devenir invisible parce qu'on ne sait pas le nommer.

    Un point monte plusieurs fois garde la DERNIERE ligne : c'est celle qui est
    visible, les precedentes sont recouvertes.
    """
    fichier = MOUNTINFO if fichier is None else fichier
    try:
        contenu = fichier.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return {}
    montages: dict[str, Montage] = {}
    for ligne in contenu.splitlines():
        champs = ligne.split()
        try:
            separateur = champs.index("-", 6)
            point = _desechappe(champs[4])
            racine = _desechappe(champs[3])
            fs = champs[separateur + 1]
        except (ValueError, IndexError):
            continue
        source = champs[separateur + 2] if len(champs) > separateur + 2 else ""
        montages[os.path.normpath(point)] = Montage(point, fs, source, racine)
    return montages


# --- Les disques --------------------------------------------------------------


@dataclass(slots=True)
class Disque:
    """Un disque externe candidat, tel que l'interface le presente."""

    id: str
    """Chemin relatif a la racine externe. C'est ce que le client renvoie : un
    chemin absolu venu du navigateur ne serait jamais utilise tel quel."""

    path: Path
    mounted: bool = False
    fs: str | None = None
    free_bytes: int = 0
    total_bytes: int = 0
    writable: bool = False
    refusal: str | None = None
    dev: int = 0
    """``st_dev`` releve a la detection : la copie le compare avant chaque
    fichier. Un disque debranche laisse un dossier dont le ``st_dev`` est celui
    de la partition systeme — c'est ce changement qui arrete tout. Il ne vaut
    que le temps d'une requete ou d'une copie : rebranche, le meme disque en
    change. C'est ``marque`` qui l'identifie d'une fois sur l'autre."""

    ino: int = 0
    """Inode de la racine, releve avec ``dev``. Zero = inconnu (non compare)."""

    marque: str | None = None
    """Identifiant lu dans son ``.sortilege-disque``, quand on l'a lu ou pose.
    Renseigne, il est EXIGE a chaque ouverture de la racine (``_ouvrir_racine``) :
    un autre disque arrive entre-temps au meme endroit ne recoit rien. Jamais
    envoye au client."""

    @property
    def label(self) -> str:
        return self.id

    @property
    def max_file_bytes(self) -> int | None:
        return LIMITE_FAT32 if self.fs in SYSTEMES_FAT32 else None

    @property
    def windows(self) -> bool:
        """Systeme de fichiers a la mode Windows : noms restreints, pas de droits."""
        return self.fs in SYSTEMES_WINDOWS

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "path": str(self.path),
            "label": self.label,
            "mounted": self.mounted,
            "fs": self.fs,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "writable": self.writable,
            "max_file_bytes": self.max_file_bytes,
            "refusal": self.refusal,
        }


@dataclass(slots=True)
class Inventaire:
    racine: Path
    existe: bool = False
    disques: list[Disque] = field(default_factory=list)


def _sous_dossiers(dossier: Path) -> list[Path]:
    """Sous-dossiers directs, sans suivre les liens, sans dossiers systeme.

    Un lien symbolique n'est jamais un disque : suivre « USB1 -> / » ferait
    proposer la racine du conteneur comme destination.
    """
    with os.scandir(dossier) as entrees:
        noms = sorted(
            e.name
            for e in entrees
            if e.is_dir(follow_symlinks=False)
            and not e.name.startswith(".")
            and e.name not in SKIP_DIRS
        )
    return [dossier / nom for nom in noms]


def _est_vide(dossier: Path) -> bool:
    with os.scandir(dossier) as entrees:
        return next(entrees, None) is None


def _rien_que_des_dossiers_vides(dossier: Path) -> bool:
    """Vide, ou seulement des dossiers vides : ce que montre le parent des disques
    quand aucun n'est branche (leurs points de montage restent, vides).

    Ne change que le MOTIF d'un refus : un point de montage ainsi fait se dit
    « aucun disque monté ici », un point de montage qui a du contenu est un
    disque monte directement. Refuse dans les deux cas.
    """
    with os.scandir(dossier) as entrees:
        for entree in entrees:
            if not entree.is_dir(follow_symlinks=False) or not _est_vide(Path(entree.path)):
                return False
    return True


def _montages_imbriques(dossier: Path) -> list[Path]:
    """Points de montage sous ``dossier``, jusqu'a ``PROFONDEUR_MONTAGES``.

    On ne descend pas dans un montage trouve : son contenu est le disque, pas
    un endroit ou chercher d'autres disques.
    """
    trouves: list[Path] = []
    niveau = [dossier]
    for _ in range(PROFONDEUR_MONTAGES):
        suivant: list[Path] = []
        for courant in niveau:
            try:
                enfants = _sous_dossiers(courant)
            except OSError:
                continue
            for enfant in enfants:
                if os.path.ismount(enfant):
                    trouves.append(enfant)
                else:
                    suivant.append(enfant)
        niveau = suivant
    return sorted(trouves)


def _chevauche(a: Path, b: Path) -> bool:
    """Un chemin contient l'autre, ou ce sont les memes."""
    return a == b or a in b.parents or b in a.parents


def _volume_de_la_mediatheque(mediatheque: Path) -> int | None:
    """``st_dev`` de la mediatheque, ou None si elle ne repond pas.

    Isole pour les tests : sans vrai montage, la mediatheque et le « disque »
    d'un test vivent sur le meme systeme de fichiers, ce qui est justement le
    cas que ce releve fait refuser.
    """
    try:
        return _stat_volume(mediatheque).st_dev
    except OSError:
        return None


def _decrire(
    racine: Path,
    dossier: Path,
    montages: dict[str, Montage],
    *,
    mediatheque: Path | None,
    dev_mediatheque: int | None = None,
) -> Disque:
    """Place, droits et systeme de fichiers d'un disque reconnu."""
    ident = dossier.relative_to(racine).as_posix()
    montage = montages.get(os.path.normpath(str(dossier)))
    disque = Disque(
        id=ident,
        path=dossier,
        mounted=True,
        fs=montage.fs if montage else None,
    )

    def mesure() -> tuple[os.stat_result, Any, bool, Path]:
        return (
            os.stat(dossier),
            shutil.disk_usage(dossier),
            os.access(dossier, os.W_OK | os.X_OK),
            Path(os.path.realpath(dossier)),
        )

    try:
        infos, usage, ecriture, reel = _borne(mesure, dossier)
    except OSError as exc:
        disque.mounted = False
        disque.refusal = f"le disque ne répond pas ({exc.strerror or exc})"
        return disque

    disque.dev = infos.st_dev
    disque.ino = infos.st_ino
    disque.free_bytes = usage.free
    disque.total_bytes = usage.total
    disque.writable = ecriture
    if mediatheque is not None and _chevauche(reel, Path(os.path.realpath(mediatheque))):
        # Une racine externe mal choisie (le parent de la mediatheque) ferait
        # proposer la mediatheque elle-meme comme disque de destination.
        disque.refusal = "c'est la médiathèque, ou un dossier qui la contient : refusé"
    elif dev_mediatheque is not None and infos.st_dev == dev_mediatheque:
        # ``realpath`` ne voit pas un montage bind : le volume de la
        # mediatheque remonte sous /externes a un autre chemin passe le test
        # ci-dessus. Le ``st_dev``, lui, est celui du volume, quel que soit le
        # chemin par lequel on y arrive.
        disque.refusal = REFUS_VOLUME_MEDIATHEQUE
    return disque


def _disques_sous(
    racine: Path,
    enfant: Path,
    montages: dict[str, Montage],
    mediatheque: Path | None,
    dev_mediatheque: int | None = None,
) -> list[Disque]:
    """Ce que contient un dossier de la racine externe : des disques, ou un refus.

    Seuls les points de montage trouves A L'INTERIEUR de ``enfant`` sont des
    disques. ``enfant`` lui-meme, s'il est un point de montage, est refuse :
    c'est un disque monte directement (« -v /disque:/externes/USB1 »), et un
    tel montage survit au debranchement — Docker garde le point de montage, qui
    montre alors un dossier de la partition systeme. Le contre-controle l'a
    prouve : ni le repere ``st_dev:inode`` releve a l'analyse (l'ecran
    reanalysait et obtenait celui de la partition systeme), ni aucun autre
    releve ne distingue alors un disque debranche d'un dossier vide qu'un tiers
    a rempli. Imbrique dans le parent monte en ``rslave``, un disque debranche
    emporte son montage avec lui : il n'y a plus rien a confondre.
    """
    ident = enfant.relative_to(racine).as_posix()
    try:
        _stat_volume(enfant)
        imbriques = _borne(lambda: _montages_imbriques(enfant), enfant)
        if imbriques:
            return [
                _decrire(
                    racine,
                    d,
                    montages,
                    mediatheque=mediatheque,
                    dev_mediatheque=dev_mediatheque,
                )
                for d in imbriques
            ]
        direct = _borne(
            lambda: os.path.ismount(enfant) and not _rien_que_des_dossiers_vides(enfant), enfant
        )
    except OSError as exc:
        return [Disque(id=ident, path=enfant, refusal=f"ne répond pas ({exc.strerror or exc})")]

    if direct:
        return [Disque(id=ident, path=enfant, refusal=REFUS_MONTAGE_DIRECT)]
    # Le cas qui justifie tout ce module : un dossier vide ou ordinaire n'est
    # PAS un disque. C'est ce que devient le point de montage d'un disque
    # debranche, et il vit sur la partition systeme du NAS.
    return [Disque(id=ident, path=enfant, refusal=REFUS_SANS_DISQUE)]


def inventorier(racine: Path, *, mediatheque: Path | None = None) -> Inventaire:
    """Les disques presents sous ``racine``, et les dossiers qui n'en sont pas.

    Pour chaque dossier C de la racine :

    - les points de montage trouves DANS C (profondeur 2) sont des disques.
      C'est la seule configuration prise en charge : le parent des disques USB
      est monte en ``rslave`` sur C, et chaque disque y apparait comme un
      montage imbrique ;
    - sinon, si C est lui-meme un point de montage NON VIDE, c'est un disque
      en « montage direct » : liste, mais refuse (voir ``_disques_sous``) ;
    - sinon, C est liste avec un refus, jamais selectionnable.

    Un disque qui est le volume de la mediatheque (meme ``st_dev``) est refuse
    quel que soit son chemin : un montage bind la ferait passer pour externe.
    """
    inventaire = Inventaire(racine=racine)
    try:
        infos = _stat_volume(racine)
    except OSError:
        return inventaire
    if not stat.S_ISDIR(infos.st_mode):
        return inventaire
    inventaire.existe = True

    montages = lire_montages()
    try:
        enfants = _borne(lambda: _sous_dossiers(racine), racine)
    except OSError as exc:
        logger.warning("racine des disques externes illisible (%s) : %s", racine, exc)
        return inventaire

    dev_mediatheque = _volume_de_la_mediatheque(mediatheque) if mediatheque is not None else None
    for enfant in enfants:
        inventaire.disques.extend(
            _disques_sous(racine, enfant, montages, mediatheque, dev_mediatheque)
        )
    inventaire.disques.sort(key=lambda d: d.id)
    return inventaire


def identifiant_valide(ident: str) -> bool:
    """Forme d'un identifiant de disque : relatif, sans « .. » ni separateur Windows.

    Verifie AVANT toute comparaison : un identifiant venu du client n'est
    jamais joint a un chemin tant qu'il n'a pas passe ce filtre, et il n'est
    ensuite accepte que s'il designe un disque detecte a l'instant.
    """
    if not ident or ident.startswith("/") or "\\" in ident or "\0" in ident:
        return False
    return all(part not in ("", ".", "..") for part in ident.split("/"))


# --- L'analyse ----------------------------------------------------------------


def racine_d_oeuvre(chemins: Iterable[str]) -> PurePosixPath:
    """Parent commun des fichiers d'une oeuvre, remonte d'un cran si c'est une saison.

    « Severance/Season 01/E01.mkv » et « .../E02.mkv » ont pour parent commun
    « Severance/Season 01 » : la serie, c'est « Severance ». Le numero de
    saison se reconnait par ``parser._season_from_dirs``, le meme test que la
    lecture des noms — « Season 01 », « Saison 2 », « S03 ».
    """
    parents = [PurePosixPath(c).parent.parts for c in chemins]
    if not parents:
        return PurePosixPath()
    commun = parents[0]
    for autre in parents[1:]:
        n = 0
        while n < min(len(commun), len(autre)) and commun[n] == autre[n]:
            n += 1
        commun = commun[:n]
    if commun and _season_from_dirs([commun[-1]]) is not None:
        commun = commun[:-1]
    return PurePosixPath(*commun)


def _texte(chemin: PurePosixPath) -> str:
    """« Films/Dune », et « » pour la racine (et non « . »)."""
    return "/".join(chemin.parts)


def _saison_du_label(label: str) -> int | None:
    trouve = _SLOT_SAISON.match(label)
    return int(trouve[1]) if trouve else None


def _annee(relatif: str) -> int | None:
    """Annee lue dans un chemin, par le meme parseur que l'index."""
    chemin = Path(relatif)
    return parse(chemin, list(chemin.parts[:-1])).year


def _annees_compatibles(a: int | None, b: int | None) -> bool:
    return a is None or b is None or a == b


def _nom_invalide(relatif: str) -> str | None:
    """Le premier composant qu'exFAT/NTFS/FAT32 refuserait, ou None."""
    for composant in PurePosixPath(relatif).parts:
        if any(c in CARACTERES_INTERDITS for c in composant):
            return composant
    return None


def compagnons(video: Path) -> list[Path]:
    """Sous-titres, affiches et fiches (.nfo, .opf) qui accompagnent une video.

    ``find_companions`` pour les sous-titres et les affiches — la meme regle
    que le rangement —, plus la fiche au meme radical, que Sortilege ecrit
    lui-meme a cote de chaque video et que ``find_companions`` ne rend pas.
    """
    trouves = [c.path for c in find_companions(video)]
    for extension in (".nfo", ".opf"):
        fiche = video.with_suffix(extension)
        if fiche != video and fiche.is_file() and fiche not in trouves:
            trouves.append(fiche)
    return sorted(trouves)


@dataclass(slots=True)
class Tache:
    """Un fichier a copier : source dans la mediatheque, cible sur le disque."""

    source: Path
    cible: Path
    source_rel: str
    cible_rel: str
    taille: int
    compagnon: bool = False


@dataclass(slots=True)
class _Placement:
    """Ou vont les fichiers d'une oeuvre, et ce que le disque en a deja."""

    racine_media: PurePosixPath
    jumelle: Work | None = None
    racine_disque: PurePosixPath | None = None
    saisons: dict[int, PurePosixPath] = field(default_factory=dict)
    films: list[tuple[int | None, PurePosixPath]] = field(default_factory=list)


def _dossiers_de_saison(
    jumelle: Work, racine_disque: PurePosixPath, disque: Path
) -> dict[int, PurePosixPath]:
    """Numero de saison -> dossier ou le disque range DEJA cette saison.

    D'abord la ou sont les episodes de cette saison (le dossier qui en porte le
    plus) : « Saison 1 » sur le disque recoit les episodes que la mediatheque
    range dans « Season 01 », sans creer un second dossier de saison a cote.
    Ensuite les dossiers de saison sans episode reconnu, sous la racine de
    l'oeuvre : un « Saison 2 » vide sur le disque est une intention a suivre.
    Le numero decide, pas le texte.
    """
    par_saison: dict[int, Counter[PurePosixPath]] = {}
    for label, refs in jumelle.slots.items():
        numero = _saison_du_label(label)
        if numero is None:
            continue
        for ref in refs:
            par_saison.setdefault(numero, Counter())[PurePosixPath(ref.relative_path).parent] += 1
    saisons = {
        numero: min(compte.items(), key=lambda kv: (-kv[1], str(kv[0])))[0]
        for numero, compte in par_saison.items()
    }
    try:
        dossiers = _sous_dossiers(disque / racine_disque)
    except OSError:
        dossiers = []
    for dossier in dossiers:
        numero = _season_from_dirs([dossier.name])
        if numero is not None and numero not in saisons:
            saisons[numero] = racine_disque / dossier.name
    return saisons


def _placer(work: Work, jumelle: Work | None, disque: Path) -> _Placement:
    fichiers = [ref.relative_path for refs in work.slots.values() for ref in refs]
    placement = _Placement(racine_media=racine_d_oeuvre(fichiers))
    if jumelle is None:
        return placement

    if work.kind == "movie":
        # Les homonymes partagent la cle (« movie:dune ») : l'annee de CHAQUE
        # fichier departage, sans quoi « Dune (2021) » passerait pour present
        # parce que le disque a « Dune (1984) ».
        placement.films = sorted(
            ((_annee(ref.relative_path), PurePosixPath(ref.relative_path)))
            for ref in jumelle.slots.get("film", [])
        )
        return placement

    if not _annees_compatibles(work.year, jumelle.year):
        return placement
    placement.jumelle = jumelle
    placement.racine_disque = racine_d_oeuvre(
        ref.relative_path for refs in jumelle.slots.values() for ref in refs
    )
    placement.saisons = _dossiers_de_saison(jumelle, placement.racine_disque, disque)
    return placement


def _destination(
    placement: _Placement, label: str, relatif: str
) -> tuple[PurePosixPath, str | None]:
    """Chemin cible (relatif au disque), et la copie deja presente sous un autre nom.

    Oeuvre absente du disque : on reproduit la mediatheque. Oeuvre presente :
    on range dans le dossier qui existe — la saison deja la si le numero
    correspond, sinon la racine de l'oeuvre sur le disque, avec le chemin
    relatif a la racine cote mediatheque.
    """
    chemin = PurePosixPath(relatif)

    if placement.films:
        annee = _annee(relatif)
        compatibles = [p for a, p in placement.films if _annees_compatibles(annee, a)]
        if compatibles:
            existant = compatibles[0]
            return existant.parent / chemin.name, _texte(existant)
        return chemin, None

    if placement.jumelle is None or placement.racine_disque is None:
        return chemin, None

    ailleurs = placement.jumelle.slots.get(label)
    trouve = _texte(PurePosixPath(ailleurs[0].relative_path)) if ailleurs else None

    saison = _saison_du_label(label)
    if saison is not None and saison in placement.saisons:
        return placement.saisons[saison] / chemin.name, trouve

    if placement.racine_media.parts:
        relatif_oeuvre = chemin.relative_to(placement.racine_media)
    else:
        relatif_oeuvre = chemin
    return placement.racine_disque / relatif_oeuvre, trouve


@dataclass(slots=True)
class _Bilan:
    """Ce qu'on sait d'une oeuvre au fil de l'analyse."""

    total: int = 0
    presents: int = 0
    a_copier: int = 0
    octets: int = 0
    problemes: list[dict[str, str]] = field(default_factory=list)
    details: list[dict[str, object]] = field(default_factory=list)

    @property
    def etat(self) -> str:
        if self.presents == self.total:
            return "present"
        if self.presents == 0:
            return "absent"
        return "partial"


def _octets_lisibles(octets: int) -> str:
    """« 1,2 Go » : pour les messages, jamais pour les champs chiffres."""
    valeur = float(octets)
    for unite in ("octets", "Ko", "Mo", "Go"):
        if abs(valeur) < 1024 or unite == "Go":
            break
        valeur /= 1024
    if unite == "octets":
        return f"{int(valeur)} octets"
    return f"{valeur:.1f} {unite}".replace(".", ",")


class _Analyseur:
    """Une analyse : un disque scanne une fois, des oeuvres evaluees une a une."""

    def __init__(self, disque: Disque, mediatheque: Path, sur_disque: dict[str, Work]) -> None:
        self.disque = disque
        self.mediatheque = mediatheque
        self.sur_disque = sur_disque
        self.taches: list[Tache] = []
        self.presents = 0
        self._prevues: dict[str, str] = {}
        self._dossiers: dict[str, tuple[str, Any]] = {}

    def _examiner(self, chemin: Path, relatif: str) -> tuple[str, Any]:
        """Un composant du disque, sans le suivre : (« absent », None), (« refus »,
        (raison, message)) ou (« la », son ``lstat``)."""
        try:
            infos = os.lstat(chemin)
        except OSError:
            return "absent", None
        if stat.S_ISLNK(infos.st_mode):
            return "refus", (
                "symlink",
                f"« {relatif} » : {REFUS_LIEN} — Sortilège ne suit jamais un lien, ce qui est "
                "derrière n'est ni compté comme présent ni copié.",
            )
        if self.disque.dev and infos.st_dev != self.disque.dev:
            return "refus", (
                "other_volume",
                f"« {relatif} » n'est pas sur le disque (un autre volume est monté dessous) : "
                "refusé.",
            )
        return "la", infos

    def _occupant(self, cible_rel: str) -> tuple[os.stat_result | None, tuple[str, str] | None]:
        """Ce qui occupe ``cible_rel`` sur le disque, et le refus s'il y en a un.

        Composant par composant, sans jamais suivre de lien : ``os.lstat`` sur
        le chemin entier ne protege que le DERNIER composant. Un lien « Films ->
        /media/Films » pose sur le disque faisait trouver, a travers lui, le
        film de la mediatheque elle-meme — meme taille, donc « déjà présent », et
        jamais copie. La copie refuse ce lien ; l'analyse doit le dire, et avant.
        Les dossiers deja examines ne le sont pas deux fois : une saison de
        vingt episodes partage les memes.
        """
        parties = PurePosixPath(cible_rel).parts
        chemin = self.disque.path
        for i, nom in enumerate(parties):
            chemin = chemin / nom
            relatif = "/".join(parties[: i + 1])
            dernier = i == len(parties) - 1
            if dernier:
                verdict, valeur = self._examiner(chemin, relatif)
            else:
                if relatif not in self._dossiers:
                    self._dossiers[relatif] = self._examiner(chemin, relatif)
                verdict, valeur = self._dossiers[relatif]
            if verdict == "absent":
                return None, None
            if verdict == "refus":
                return None, valeur
            if dernier:
                return valeur, None
            if not stat.S_ISDIR(valeur.st_mode):
                # Un fichier la ou il faut un dossier : la copie le refusera,
                # avec son motif. Rien n'occupe la cible elle-meme.
                return None, None
        return None, None

    def _cle(self, cible_rel: str) -> str:
        # exFAT, NTFS et FAT32 ignorent la casse : « Film.FR.srt » et
        # « Film.fr.srt » y designent le meme fichier.
        return cible_rel.casefold() if self.disque.windows else cible_rel

    def _evaluer(
        self,
        bilan: _Bilan,
        source_rel: str,
        source: Path,
        cible_rel: str,
        ailleurs: str | None,
        *,
        compagnon: bool,
    ) -> str:
        """Classe un fichier : copy, present, present_elsewhere ou issue."""
        bilan.total += 1
        detail: dict[str, object] = {
            "source": source_rel,
            "target": cible_rel,
            "bytes": 0,
            "status": "issue",
            "sidecar": compagnon,
            "found": None,
        }
        bilan.details.append(detail)

        def probleme(raison: str, message: str) -> str:
            bilan.problemes.append({"path": source_rel, "reason": raison, "message": message})
            return "issue"

        try:
            infos = os.stat(source)
        except OSError:
            return probleme(
                "missing_source",
                f"« {source_rel} » est introuvable dans la médiathèque — relis-la.",
            )
        taille = infos.st_size
        detail["bytes"] = taille

        cle = self._cle(cible_rel)
        if cle in self._prevues:
            return probleme(
                "conflict",
                f"« {cible_rel} » est déjà la destination de « {self._prevues[cle]} » : "
                "rien ne sera écrasé.",
            )

        interdit = _nom_invalide(cible_rel) if self.disque.windows else None
        if interdit is None:
            existante, refus = self._occupant(cible_rel)
            if refus is not None:
                return probleme(*refus)
            if existante is not None:
                if stat.S_ISREG(existante.st_mode) and existante.st_size == taille:
                    detail["status"] = "present"
                    bilan.presents += 1
                    return "present"
                return probleme(
                    "conflict",
                    f"« {cible_rel} » existe déjà sur le disque avec une autre taille "
                    f"({_octets_lisibles(existante.st_size)} au lieu de "
                    f"{_octets_lisibles(taille)}) : jamais écrasé.",
                )

        if ailleurs is not None:
            # La copie deja la sous un autre nom est un fichier que le scanner
            # a vu : un lien sur le disque passe ce filtre (il suit les liens
            # de fichiers). Elle n'est « présente » que si elle y est vraiment.
            _, refus = self._occupant(ailleurs)
            if refus is not None:
                return probleme(*refus)
            detail["status"] = "present_elsewhere"
            detail["found"] = ailleurs
            bilan.presents += 1
            return "present_elsewhere"

        if interdit is not None:
            return probleme(
                "name_invalid",
                f"« {interdit} » contient un caractère que {self.disque.fs} refuse "
                '(\\ : * ? " < > |) : non copié, et pas renommé en silence.',
            )

        limite = self.disque.max_file_bytes
        if limite is not None and taille > limite:
            return probleme(
                "too_large",
                f"« {source_rel} » pèse {_octets_lisibles(taille)} : FAT32 n'accepte pas "
                "un fichier de plus de 4 Go. Reformate le disque en exFAT pour le copier.",
            )

        self._prevues[cle] = source_rel
        detail["status"] = "copy"
        bilan.a_copier += 1
        bilan.octets += taille
        self.taches.append(
            Tache(
                source=source,
                cible=self.disque.path / cible_rel,
                source_rel=source_rel,
                cible_rel=cible_rel,
                taille=taille,
                compagnon=compagnon,
            )
        )
        return "copy"

    def oeuvre(self, work: Work, *, avec_compagnons: bool) -> dict[str, object]:
        jumelle = self.sur_disque.get(work.key)
        placement = _placer(work, jumelle, self.disque.path)
        bilan = _Bilan()
        existant: str | None = None

        fichiers = sorted(
            ((label, ref) for label, refs in work.slots.items() for ref in refs),
            key=lambda paire: paire[1].relative_path,
        )
        for label, ref in fichiers:
            cible, ailleurs = _destination(placement, label, ref.relative_path)
            if placement.films and ailleurs is not None and existant is None:
                existant = _texte(PurePosixPath(ailleurs).parent)
            source = self.mediatheque / ref.relative_path
            cible_rel = _texte(cible)
            verdict = self._evaluer(
                bilan, ref.relative_path, source, cible_rel, ailleurs, compagnon=False
            )
            # Les compagnons suivent la video la ou ELLE est ou va : pas a cote
            # d'une copie qui porte un autre nom, ni d'une video non copiee.
            if not avec_compagnons or verdict not in ("copy", "present"):
                continue
            for annexe in compagnons(source):
                annexe_rel = annexe.relative_to(self.mediatheque).as_posix()
                self._evaluer(
                    bilan,
                    annexe_rel,
                    annexe,
                    _texte(cible.parent / annexe.name),
                    None,
                    compagnon=True,
                )

        if placement.racine_disque is not None:
            existant = _texte(placement.racine_disque)
        self.presents += bilan.presents
        return {
            "key": work.key,
            "title": work.title,
            "year": work.year,
            "kind": work.kind,
            "state": bilan.etat,
            "files_total": bilan.total,
            "files_present": bilan.presents,
            "files_to_copy": bilan.a_copier,
            "bytes_to_copy": bilan.octets,
            "target_dir": existant if existant is not None else _texte(placement.racine_media),
            "existing_dir": existant,
            "issues": bilan.problemes,
            "details": bilan.details,
        }


def marge(disque: Disque, fichiers: int = 0) -> int:
    """Ce que la place libre annoncee ne donne pas vraiment.

    Proportionnelle a la taille du disque, la marge refusait l'evidence : 1 %
    d'un disque de 4 To, c'est 40 Go, et une copie de 30 Go etait refusee avec
    45 Go libres. Ce qui se perd reellement ne depend pas de la taille du
    disque mais du NOMBRE de fichiers : chaque fichier occupe des grappes
    entieres (jusqu'a 1 Mio par grappe sur un exFAT de plusieurs To), plus les
    entrees de repertoire. D'ou un socle fixe et un surcout par fichier.
    """
    return MARGE_SOCLE + fichiers * MARGE_PAR_FICHIER


@dataclass(slots=True)
class Analyse:
    disque: Disque
    oeuvres: list[dict[str, object]]
    taches: list[Tache]
    erreurs_scan: list[str]
    presents: int = 0

    deja_ecrit: int = 0
    """Octets deja sur le disque dans le ``.sortilege-part`` qu'on va reprendre.

    Ils occupent deja leur place : les compter une seconde fois ferait refuser
    la fin d'un film de 40 Go sur un disque qui a toute la place pour elle."""

    @property
    def octets(self) -> int:
        return sum(t.taille for t in self.taches)

    @property
    def marge(self) -> int:
        return marge(self.disque, len(self.taches))

    @property
    def manque(self) -> int:
        if not self.taches:
            return 0
        return max(0, self.octets - self.deja_ecrit + self.marge - self.disque.free_bytes)

    @property
    def tient(self) -> bool:
        return self.manque == 0

    def crediter_reprise(self, reprise: dict[str, Any] | None) -> None:
        """Tient compte du ``.part`` qu'on reprendra, s'il est bien la et a nous."""
        if not reprise:
            return
        for tache in self.taches:
            if tache.source_rel == reprise.get("source") and (
                f"{tache.cible_rel}{SUFFIXE_PARTIEL}" == reprise.get("part")
            ):
                if partiel_designe(self.disque, str(reprise["part"])):
                    self.deja_ecrit = min(int(reprise.get("checkpoint", 0)), tache.taille)
                return

    def to_dict(self) -> dict[str, object]:
        return {
            "disk": self.disque.to_dict(),
            "works": self.oeuvres,
            "totals": {
                "files_to_copy": len(self.taches),
                "bytes_to_copy": self.octets,
                "files_present": self.presents,
            },
            "fits": self.tient,
            "missing_bytes": self.manque,
            "margin_bytes": self.marge,
            "scan_errors": self.erreurs_scan,
        }

    def motif_place(self) -> str:
        return (
            f"Place insuffisante sur « {self.disque.label} » : "
            f"{_octets_lisibles(self.octets)} à copier, "
            f"{_octets_lisibles(self.disque.free_bytes)} libres, "
            f"marge de sécurité {_octets_lisibles(self.marge)} — "
            f"il manque {_octets_lisibles(self.manque)} ({self.manque} octets)."
        )


def analyser(
    disque: Disque,
    oeuvres: list[Work],
    mediatheque: Path,
    *,
    avec_compagnons: bool = True,
    regles: ScanRules = DEFAULT_RULES,
) -> Analyse:
    """Ce qui est deja sur le disque, ce qui manque, et ce qui coince.

    Le disque est scanne avec le scanner de la mediatheque puis regroupe par
    ``collection.group`` : une oeuvre y est reconnue par la meme cle que dans
    l'index, quel que soit le nom de ses fichiers. Rien n'est ecrit.
    """
    resultat = scan([disque.path], deep=False, library_root=None, rules=regles)
    sur_disque = {w.key: w for w in group(resultat.files)}
    analyseur = _Analyseur(disque, mediatheque, sur_disque)
    bilans = [analyseur.oeuvre(w, avec_compagnons=avec_compagnons) for w in oeuvres]
    return Analyse(
        disque=disque,
        oeuvres=bilans,
        taches=analyseur.taches,
        erreurs_scan=resultat.errors,
        presents=analyseur.presents,
    )


# --- Le disque, par descripteur -----------------------------------------------


class _Refus(Exception):
    """Un chemin du disque qu'on refuse de traverser : lien, autre volume, fichier.

    Propre a CE chemin : le fichier concerne echoue, la file continue. Un
    disque parti, lui, se manifeste par une ``OSError`` (EIO, ENOENT...).
    """


class _AutreVolume(OSError):
    """La racine ouverte n'est pas le volume releve a la detection."""


def _ouvrir_racine(disque: Disque) -> int:
    """Descripteur de la racine du disque, verifiee contre la detection.

    ``st_dev`` doit etre celui releve a l'inventaire, et l'inode de la racine
    aussi quand il est connu : un disque debranche entre les deux laisse un
    dossier de la partition systeme, que ce controle refuse avant la moindre
    ecriture. Quand l'identifiant du disque est connu (``disque.marque``), son
    ``.sortilege-disque`` doit le porter, lu par ce meme descripteur : c'est ce
    qui refuse un AUTRE disque arrive au meme endroit. Ensuite, tout passe par
    ce descripteur — le chemin n'est plus jamais relu.
    """
    fd = os.open(disque.path, _DRAPEAUX_DOSSIER)
    try:
        infos = os.fstat(fd)
        if infos.st_dev != disque.dev or (disque.ino and infos.st_ino != disque.ino):
            raise _AutreVolume(
                errno.EXDEV,
                "n'est plus le volume détecté (débranché, remplacé ?)",
                str(disque.path),
            )
        if disque.marque is not None and _lire_marque(fd) != disque.marque:
            raise _AutreVolume(
                errno.EXDEV,
                f"ne porte plus le repère de cette copie ({MARQUEUR}) : un autre disque ?",
                str(disque.path),
            )
    except BaseException:
        os.close(fd)
        raise
    return fd


def _refus_composant(parent: int, nom: str, chemin: PurePosixPath, exc: OSError) -> Exception:
    """Pourquoi ``nom`` ne s'ouvre pas comme dossier : un lien, un fichier, ou autre chose."""
    try:
        infos = os.stat(nom, dir_fd=parent, follow_symlinks=False)
    except OSError:
        return exc
    if stat.S_ISLNK(infos.st_mode):
        return _Refus(f"{REFUS_LIEN} (« {chemin} »)")
    if not stat.S_ISDIR(infos.st_mode):
        return _Refus(f"« {chemin} » existe sur le disque et n'est pas un dossier : refusé")
    return exc


def _adopter_identite(cible: int, modele: int, quoi: object) -> None:
    """Donne a ``cible`` le proprietaire de ``modele`` (deux descripteurs). Mode root seul.

    Meme regle que ``proprietaire.adopte_identite_du_dossier`` — ce qui entre
    prend l'identite du dossier qui l'accueille —, mais par descripteur :
    ``fchown`` vise l'inode qu'on vient de creer, aucun nom n'est relu, donc
    aucun lien pose entre-temps ne peut detourner le changement. Un echec est
    dit, jamais leve : le contenu est bon et sa place est bonne.
    """
    if not tourne_en_root():
        return
    try:
        infos = os.fstat(modele)
        os.fchown(cible, infos.st_uid, infos.st_gid)
    except OSError as exc:
        logger.warning("« %s » garde son propriétaire (root) : %s", quoi, exc)


def _descendre(
    racine: int,
    parties: Iterable[str],
    dev: int,
    *,
    creer: bool = False,
    identite: bool = False,
) -> int:
    """Descripteur du dossier ``parties`` sous ``racine``, un composant a la fois.

    Chaque composant est cree (``creer``) PUIS ouvert relativement au dossier
    parent deja ouvert, avec ``O_NOFOLLOW`` : un lien « Films -> /media » pose
    sur le disque est refuse au lieu d'etre suivi — suivi, il faisait creer des
    dossiers DANS la mediatheque. Un composant d'un autre volume (un montage
    sous le disque) est refuse aussi. En root, un dossier cree prend
    l'identite de son parent (``identite``), etage par etage : le modele est
    toujours le parent immediat, jamais un ancetre releve avant coup.

    Leve ``_Refus`` pour ce qu'on refuse de traverser, ``OSError`` sinon.
    """
    courant = os.dup(racine)
    chemin = PurePosixPath()
    try:
        for nom in parties:
            chemin = chemin / nom
            cree = False
            if creer:
                try:
                    os.mkdir(nom, dir_fd=courant)
                    cree = True
                except FileExistsError:
                    pass
            try:
                suivant = os.open(nom, _DRAPEAUX_DOSSIER, dir_fd=courant)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
                    raise _refus_composant(courant, nom, chemin, exc) from exc
                raise
            try:
                if os.fstat(suivant).st_dev != dev:
                    raise _Refus(
                        f"« {chemin} » n'est pas sur le disque (autre volume monté dessous) : "
                        "refusé"
                    )
                if cree and identite:
                    _adopter_identite(suivant, courant, chemin)
            except BaseException:
                os.close(suivant)
                raise
            os.close(courant)
            courant = suivant
    except BaseException:
        os.close(courant)
        raise
    return courant


def _existe(dossier: int, nom: str) -> bool:
    """Un nom existe-t-il dans ``dossier`` (sans suivre de lien) ?"""
    try:
        os.stat(nom, dir_fd=dossier, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def _fsync_dossier(dossier: int) -> None:
    """Rend durable l'entree du dossier (le renommage). Au mieux."""
    try:
        os.fsync(dossier)
    except OSError:
        pass


# --- L'identite du disque : son fichier-marqueur -------------------------------


class ErreurMarque(Exception):
    """Le disque ne donne ni n'accepte son identifiant. Le message est redige."""


def marque_valide(texte: object) -> bool:
    """Un identifiant de disque tel que Sortilege les ecrit (UUID, minuscules)."""
    return isinstance(texte, str) and _FORME_MARQUE.match(texte) is not None


def _lire_marque(racine: int) -> str | None:
    """L'identifiant que porte le ``.sortilege-disque`` de ``racine``, ou None.

    Lu par descripteur, sans suivre de lien et sans bloquer (``O_NONBLOCK`` :
    une FIFO posee sous ce nom ne fige pas la requete). Un lien, un dossier, un
    fichier d'un autre volume ou un contenu qui n'est pas un identifiant ne
    sont pas un marqueur : None, et c'est au besoin a l'appelant d'en poser un.
    """
    drapeaux = (
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(MARQUEUR, drapeaux, dir_fd=racine)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return None
        raise
    try:
        infos = os.fstat(fd)
        if not stat.S_ISREG(infos.st_mode) or infos.st_dev != os.fstat(racine).st_dev:
            return None
        brut = os.read(fd, 128)
    finally:
        os.close(fd)
    texte = brut.decode("ascii", "replace").strip()
    return texte if marque_valide(texte) else None


def _poser_marque(racine: int, disque: Disque) -> str:
    """Ecrit un nouvel identifiant dans ``.sortilege-disque``, par descripteur.

    Comme une copie : un provisoire force sur le disque, puis renomme. Une
    coupure laisse l'ancien marqueur ou le nouveau, jamais un fichier vide qui
    ne designerait plus rien. Le renommage remplace un marqueur illisible (ou
    un lien pose sous ce nom, sans le suivre) ; un dossier a sa place le fait
    echouer, et la copie ne part pas.
    """
    marque = str(uuid.uuid4())
    provisoire = f"{MARQUEUR}{SUFFIXE_PARTIEL}"
    fd = os.open(
        provisoire,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_TRUNC
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        0o644,
        dir_fd=racine,
    )
    try:
        try:
            os.write(fd, f"{marque}\n".encode("ascii"))
            if not disque.windows:
                rend_lisible_avant_publication(fd, disque.path / MARQUEUR)
                _adopter_identite(fd, racine, MARQUEUR)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(provisoire, MARQUEUR, src_dir_fd=racine, dst_dir_fd=racine)
    except BaseException:
        try:
            os.unlink(provisoire, dir_fd=racine)
        except OSError:
            pass
        raise
    _fsync_dossier(racine)
    return marque


def marquer_disque(disque: Disque) -> str:
    """L'identifiant du disque : celui de son marqueur, ou un nouveau, pose ici.

    Appele au depart d'une copie, juste avant d'ecrire quoi que ce soit : c'est
    ce qui permettra de reconnaitre CE disque pour la reprendre, quel que soit
    le port ou il sera rebranche. Un marqueur deja la est garde tel quel — deux
    copies vers le meme disque portent le meme identifiant. Leve
    ``ErreurMarque`` si le disque ne le donne ni ne l'accepte : la copie ne
    part pas.
    """

    def faire(engagement: _Engagement) -> str:
        racine = _ouvrir_racine(disque)
        try:
            marque = _lire_marque(racine)
            if marque is not None:
                return marque
            if _existe(racine, MARQUEUR):
                logger.warning("« %s » : %s illisible, remplacé", disque.label, MARQUEUR)
            if not engagement.engager():
                raise MontageFige(errno.ETIMEDOUT, "délai dépassé", str(disque.path))
            return _poser_marque(racine, disque)
        finally:
            os.close(racine)

    try:
        return _borne_engage(faire, disque.path)
    except _AutreVolume as exc:
        raise ErreurMarque(f"le disque {exc.strerror}") from exc
    except MontageFige as exc:
        raise ErreurMarque(f"le disque {exc.strerror}") from exc
    except OSError as exc:
        raise ErreurMarque(_message_ecriture(exc)) from exc


def lire_marque(disque: Disque) -> str | None:
    """L'identifiant que porte le disque, ou None (pas de marqueur, ou muet)."""

    def faire() -> str | None:
        racine = _ouvrir_racine(disque)
        try:
            return _lire_marque(racine)
        finally:
            os.close(racine)

    try:
        return _borne(faire, disque.path)
    except OSError:
        return None


# --- L'etat de reprise --------------------------------------------------------

FICHIER_REPRISE = "copie-reprise.json"
"""Nom du fichier d'etat de reprise, dans le dossier de donnees.

Il survit au redemarrage du conteneur : c'est ce qui permet de reprendre un
film de 40 Go la ou il s'etait arrete, au lieu de le recommencer."""

VERSION_REPRISE = 3
"""3 : l'identifiant du disque (``disk_mark``, lu dans son ``.sortilege-disque``)
est enregistre et exige ; c'est lui, et non le point de montage, qui designe
le disque a reprendre. 2 : l'identite de la source (inode, ``ctime``). Un etat
plus ancien ne porte pas ce qu'on exige : il est ignore plutot que repris sur
une foi qu'on sait insuffisante."""

INTERVALLE_POINT_DE_CONTROLE = 256 * 1024 * 1024
"""Octets ecrits entre deux points de controle (``fsync`` puis etat enregistre).

Seul ce qui precede un point de controle est repute ecrit : apres un
arrachement ou une coupure, les derniers blocs ont pu ne jamais quitter le
cache, et la taille du ``.sortilege-part`` ment. Reprendre a cette taille
donnerait un film avec un trou, qui se lit puis se fige. 256 Mio : moins de dix
secondes a recopier sur un port USB 2, et un ``fsync`` qui ne pese rien devant
ce qu'il protege."""

FENETRE_VERIFICATION = 1024 * 1024
ECHANTILLONS_VERIFICATION = 5
"""Verification d'une reprise, en deux temps.

1. Le dernier segment avant le point de controle (``INTERVALLE_POINT_DE_CONTROLE``
   octets au plus) est relu EN ENTIER et compare a la source. C'est la que
   frappe une coupure : un pont USB qui acquitte un ``fsync`` sans avoir vide
   son cache laisse des blocs jamais ecrits juste avant le dernier point.
2. Avant ce segment, cinq fenetres d'un Mio — le debut, la fin, trois entre —,
   comme ``journal._debut_de``. Relire 40 Go sur un port USB pour s'assurer de
   39 d'entre eux couterait plus que de les recopier ; un octet change la,
   hors fenetre, n'est pas vu, et les textes le disent (« échantillons »)."""

_verrou_reprise = threading.Lock()


def instantane_oeuvres(oeuvres: list[Work]) -> list[dict[str, object]]:
    """Ce qu'il faut garder des oeuvres choisies pour refaire l'analyse plus tard.

    L'index de la mediatheque ne survit pas au redemarrage du conteneur : une
    reprise qui en dependrait obligerait a relire toute la mediatheque avant de
    pouvoir continuer une copie. On garde donc, par oeuvre, ses emplacements et
    leurs fichiers — exactement ce dont l'analyse a besoin.
    """
    return [
        {
            "key": w.key,
            "kind": w.kind,
            "title": w.title,
            "year": w.year,
            "slots": {
                label: [[ref.relative_path, ref.size_bytes] for ref in refs]
                for label, refs in w.slots.items()
            },
        }
        for w in oeuvres
    ]


def oeuvres_depuis_instantane(donnees: list[dict[str, Any]]) -> list[Work]:
    """L'inverse de ``instantane_oeuvres``. Leve ``ValueError`` sur une forme inattendue."""
    oeuvres: list[Work] = []
    for d in donnees:
        slots: dict[str, list[FileRef]] = {}
        for label, refs in dict(d["slots"]).items():
            slots[str(label)] = [
                FileRef(relative_path=str(r[0]), size_bytes=int(r[1])) for r in refs
            ]
            # Ces chemins redeviennent des sources a LIRE et des destinations a
            # ECRIRE : un « .. » ou un chemin absolu sortirait de la mediatheque
            # ou du disque. L'etat vit dans le dossier de donnees, mais un
            # fichier qu'on relit n'est pas une preuve.
            for ref in slots[str(label)]:
                if not _chemin_relatif_sur(ref.relative_path):
                    raise ValueError(f"chemin refusé dans l'état de reprise : {ref.relative_path}")
        annee = d.get("year")
        oeuvres.append(
            Work(
                key=str(d["key"]),
                kind=str(d["kind"]),
                title=str(d["title"]),
                year=int(annee) if annee is not None else None,
                file_count=sum(len(r) for r in slots.values()),
                total_bytes=sum(f.size_bytes for r in slots.values() for f in r),
                slots=slots,
            )
        )
    return oeuvres


def _chemin_relatif_sur(texte: object) -> bool:
    """Relatif, sans « .. » ni « . » ni composant vide, sans octet nul.

    Moins strict que ``identifiant_valide`` sur un point : une barre oblique
    inversee est un caractere legal dans un nom Linux, et un film qui en porte
    une ne doit pas rendre sa copie impossible a reprendre.
    """
    if not isinstance(texte, str) or not texte or texte.startswith("/") or "\0" in texte:
        return False
    return all(part not in ("", ".", "..") for part in texte.split("/"))


def _entier(valeur: object) -> bool:
    return isinstance(valeur, int) and not isinstance(valeur, bool) and valeur >= 0


CHAMPS_POINT = ("checkpoint", "source_size", "source_mtime_ns", "source_ino", "source_ctime_ns")
"""Ce qu'un point de controle enregistre, et qu'une reprise exige.

Taille et ``mtime_ns`` ne suffisent pas : « rsync -t », « cp -p » ou
« touch -r » posent une autre version du film avec la meme taille et la meme
date. L'inode change quand le fichier est remplace, le ``ctime`` quand il est
reecrit sur place — et aucun des deux ne se regle a la main."""


def _courant_valide(courant: object) -> bool:
    if courant is None:
        return True
    if not isinstance(courant, dict):
        return False
    return (
        _chemin_relatif_sur(courant.get("source"))
        and _chemin_relatif_sur(courant.get("target"))
        and _chemin_relatif_sur(courant.get("part"))
        and str(courant.get("part")).endswith(SUFFIXE_PARTIEL)
        and courant.get("part") == f"{courant.get('target')}{SUFFIXE_PARTIEL}"
        and all(_entier(courant.get(k)) for k in CHAMPS_POINT)
    )


def lire_reprise(fichier: Path) -> dict[str, Any] | None:
    """L'etat de reprise, ou None s'il n'existe pas ou ne se relit pas.

    Verifie champ par champ : un etat casse ou fabrique ne doit jamais faire
    supprimer ni ecrire ailleurs que la ou il le pretend — et ce qu'il pretend
    est encore reverifie au moment d'agir.
    """
    try:
        brut = fichier.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("état de reprise illisible (%s) : %s", fichier, exc)
        return None
    try:
        etat = json.loads(brut)
        valide = (
            isinstance(etat, dict)
            and etat.get("version") == VERSION_REPRISE
            and _chemin_relatif_sur(etat.get("disk"))
            and isinstance(etat.get("works"), list)
            and isinstance(etat.get("sidecars"), bool)
            and (etat.get("max_mb_per_s") is None or isinstance(etat["max_mb_per_s"], int | float))
            and marque_valide(etat.get("disk_mark"))
            and _courant_valide(etat.get("current"))
        )
        if valide:
            oeuvres_depuis_instantane(etat["works"])
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        valide = False
    if not valide:
        logger.warning("état de reprise invalide (%s) : ignoré", fichier)
        return None
    return etat


def ecrire_reprise(fichier: Path, etat: dict[str, object]) -> None:
    """Ecriture ATOMIQUE : temporaire, ``fsync``, puis ``os.replace``.

    Une coupure pendant l'ecriture laisse l'ancien etat ou le nouveau, jamais
    un melange illisible — c'est le moment ou l'on en a le plus besoin.
    """
    fichier.parent.mkdir(parents=True, exist_ok=True)
    provisoire = fichier.with_name(fichier.name + ".tmp")
    donnees = json.dumps(etat, ensure_ascii=False, indent=1).encode("utf-8")
    with _verrou_reprise:
        with open(provisoire, "wb") as sortie:
            sortie.write(donnees)
            sortie.flush()
            os.fsync(sortie.fileno())
        os.replace(provisoire, fichier)


def effacer_reprise(fichier: Path) -> None:
    with _verrou_reprise:
        try:
            fichier.unlink()
        except FileNotFoundError:
            pass


def _partiel_valide(relatif: str) -> bool:
    return _chemin_relatif_sur(relatif) and relatif.endswith(SUFFIXE_PARTIEL)


def partiel_designe(disque: Disque, relatif: str) -> bool:
    """Le ``.sortilege-part`` que l'etat designe est-il la, a nous, sur ce disque ?

    Memes conditions que ``retirer_partiel`` : atteint depuis la racine du
    disque sans suivre aucun lien, sur son volume, fichier ordinaire.
    """
    if not _partiel_valide(relatif):
        return False
    chemin = PurePosixPath(relatif)

    def faire() -> bool:
        racine = _ouvrir_racine(disque)
        try:
            dossier = _descendre(racine, chemin.parent.parts, disque.dev)
        finally:
            os.close(racine)
        try:
            infos = os.stat(chemin.name, dir_fd=dossier, follow_symlinks=False)
            return stat.S_ISREG(infos.st_mode) and infos.st_dev == disque.dev
        finally:
            os.close(dossier)

    try:
        return _borne(faire, disque.path)
    except (OSError, _Refus):
        return False


RETIRE = "retire"
ABSENT = "absent"
GARDE = "garde"


@dataclass(frozen=True, slots=True)
class Retrait:
    """Ce qu'est devenu le ``.sortilege-part`` qu'un etat designe, et pourquoi.

    Trois issues, parce que l'appelant n'en tire pas la meme chose : RETIRE
    (supprime a l'instant) et ABSENT (deja plus la) permettent d'oublier l'etat
    sans laisser d'orphelin ; GARDE (inatteignable, refuse, disque muet) oblige
    a le garder — l'effacer laissait sur le disque des gigaoctets que plus rien
    ne designait, et l'ecran croyait le disque propre.
    """

    etat: str
    motif: str | None = None

    @property
    def parti(self) -> bool:
        """Plus sur le disque : retire a l'instant, ou deja absent."""
        return self.etat in (RETIRE, ABSENT)


def retirer_partiel(disque: Disque, relatif: str) -> Retrait:
    """Supprime le ``.sortilege-part`` que l'etat designe, et dit ce qui s'est passe.

    Toutes les conditions, parce que c'est ce fichier qu'on SUPPRIME : relatif
    et sans « .. », suffixe ``.sortilege-part``, atteint depuis la racine du
    disque (verifiee, marqueur compris) composant par composant sans suivre
    aucun lien, sur le volume du disque, et fichier ordinaire. Un seul manque,
    et rien n'est touche (GARDE, avec le motif).

    Borne dans le temps, mais jamais a moitie : voir ``_borne_engage``. Quand
    la reponse dit « gardé », le fichier n'a pas ete supprime et ne le sera
    pas dans son dos.
    """
    if not _partiel_valide(relatif):
        return Retrait(GARDE, "ce n'est pas un fichier partiel de Sortilège")
    chemin = PurePosixPath(relatif)

    def faire(engagement: _Engagement) -> Retrait:
        racine = _ouvrir_racine(disque)
        try:
            try:
                dossier = _descendre(racine, chemin.parent.parts, disque.dev)
            except FileNotFoundError:
                return Retrait(ABSENT)
        finally:
            os.close(racine)
        try:
            try:
                infos = os.stat(chemin.name, dir_fd=dossier, follow_symlinks=False)
            except FileNotFoundError:
                return Retrait(ABSENT)
            if not stat.S_ISREG(infos.st_mode) or infos.st_dev != disque.dev:
                return Retrait(
                    GARDE,
                    f"« {relatif} » n'est pas un fichier ordinaire du disque : laissé tel quel",
                )
            if not engagement.engager():
                return Retrait(GARDE, "délai dépassé")
            try:
                os.unlink(chemin.name, dir_fd=dossier)
            except FileNotFoundError:
                return Retrait(ABSENT)
            except OSError as exc:
                return Retrait(GARDE, _message_ecriture(exc))
            _fsync_dossier(dossier)
            return Retrait(RETIRE)
        finally:
            os.close(dossier)

    try:
        retrait = _borne_engage(faire, disque.path)
    except _Refus as exc:
        retrait = Retrait(GARDE, str(exc))
    except (_AutreVolume, MontageFige) as exc:
        retrait = Retrait(GARDE, f"le disque {exc.strerror}")
    except OSError as exc:
        retrait = Retrait(GARDE, f"le disque ne répond pas ({exc.strerror or exc})")
    if retrait.etat == GARDE:
        logger.warning("fichier partiel « %s » non supprimé : %s", relatif, retrait.motif)
    return retrait


def supprimer_partiel(disque: Disque, relatif: str) -> bool:
    """Supprime un ``.sortilege-part`` designe par l'etat. Vrai s'il a ete supprime."""
    return retirer_partiel(disque, relatif).etat == RETIRE


class _SourceIllisible(Exception):
    """Une lecture de la MEDIATHEQUE a echoue : ce fichier echoue, la file continue.

    Distincte d'une ``OSError`` a dessein : pendant une reprise, ``_concorde``
    lit le ``.part`` ET la source, et une EIO de la source remontait comme une
    erreur d'ecriture — classee « le disque ne suit plus », elle arretait toute
    la file pour un film illisible de la mediatheque.
    """


def _lire_source(source: int, longueur: int, debut: int) -> bytes:
    try:
        return os.pread(source, longueur, debut)
    except OSError as exc:
        raise _SourceIllisible(exc.strerror or str(exc)) from exc


def _concorde(partiel: int, source: int, jusqu_a: int) -> bool:
    """Le ``.part`` egale-t-il la source sur ``[0, jusqu_a)`` ?

    Le dernier segment en entier, des echantillons avant lui : voir
    ``ECHANTILLONS_VERIFICATION``. Une lecture du ``.part`` qui echoue est une
    erreur du disque (``OSError``) ; une lecture de la source, une erreur de la
    mediatheque (``_SourceIllisible``).
    """

    def egaux(debut: int, longueur: int) -> bool:
        a = os.pread(partiel, longueur, debut)
        return len(a) == longueur and a == _lire_source(source, longueur, debut)

    segment = max(0, jusqu_a - INTERVALLE_POINT_DE_CONTROLE)
    position = segment
    while position < jusqu_a:
        longueur = min(TAILLE_BLOC, jusqu_a - position)
        if not egaux(position, longueur):
            return False
        position += longueur
    for i in range(ECHANTILLONS_VERIFICATION):
        debut = max(0, (segment - FENETRE_VERIFICATION) * i // (ECHANTILLONS_VERIFICATION - 1))
        longueur = min(FENETRE_VERIFICATION, segment - debut)
        if longueur > 0 and not egaux(debut, longueur):
            return False
    return True


# --- La copie -----------------------------------------------------------------


class _Arret(Exception):
    """Arret demande par l'utilisateur, entre deux blocs."""


class _DisqueDisparu(Exception):
    """Le disque n'est plus la : toute la file s'arrete."""


class _EchecFichier(Exception):
    """Un fichier n'a pas pu etre copie ; la file continue — sauf ``disque_en_cause``.

    ``disque_en_cause`` : l'erreur dit que le DISQUE ne suit plus (voir
    ``ERREURS_DU_DISQUE``). Le ``.part`` et le point de controle sont gardes,
    et ``motif_disque`` dit pourquoi toute la file s'arrete.
    """

    def __init__(
        self, message: str, *, disque_en_cause: bool = False, motif_disque: str | None = None
    ) -> None:
        super().__init__(message)
        self.disque_en_cause = disque_en_cause
        self.motif_disque = motif_disque


def _maintenant_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _message_ecriture(exc: OSError) -> str:
    if exc.errno == errno.ENOSPC:
        return "disque plein"
    if exc.errno in (errno.EINVAL, errno.ENAMETOOLONG, errno.EILSEQ):
        return f"nom refusé par le système de fichiers du disque ({exc.strerror})"
    if exc.errno == errno.EROFS:
        return f"le disque est passé en lecture seule ({exc.strerror})"
    if exc.errno == errno.ENOENT:
        return f"un dossier de la copie a disparu du disque ({exc.strerror})"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return f"écriture refusée sur le disque ({exc.strerror})"
    return f"écriture impossible ({exc.strerror or exc})"


def _motif_disque(label: str, exc: OSError) -> str:
    """Pourquoi toute la file s'arrete sur une erreur du disque, et ce qui est garde."""
    garde = "copie arrêtée, elle reprendra à son dernier point de contrôle"
    if exc.errno == errno.EROFS:
        return (
            f"le disque « {label} » est passé en lecture seule : son système de fichiers a "
            f"signalé une erreur, et Linux le protège ainsi. {garde.capitalize()} une fois le "
            "disque vérifié (sur un ordinateur) et rebranché."
        )
    return f"le disque « {label} » ne suit plus ({_message_ecriture(exc)}) : {garde}."


def _debit(echantillons: deque[tuple[float, int]], maintenant: float, fenetre: float) -> float:
    """Octets par seconde sur la fenetre glissante, mesures jusqu'a ``maintenant``.

    On garde le dernier echantillon anterieur a la fenetre comme point de
    depart : sans lui, une fenetre vide entre deux blocs lents donnerait zero.
    Mesurer jusqu'a maintenant, et non jusqu'au dernier bloc, fait retomber le
    debit quand le disque cale — c'est justement ce qu'on veut voir.
    """
    while len(echantillons) >= 2 and echantillons[1][0] <= maintenant - fenetre:
        echantillons.popleft()
    if not echantillons:
        return 0.0
    t0, o0 = echantillons[0]
    duree = maintenant - t0
    # Pas de debit sur moins d'une seconde : le premier bloc d'un fichier part
    # dans le cache en quelques millisecondes, et 8 Mio divises par 13 ms
    # s'affichaient « 600 Mo/s » sur un disque plafonne a 4. Zero = « mesure
    # en cours » pour l'interface, ce qui est la seule chose vraie a dire.
    if duree < MESURE_MINIMALE_DEBIT:
        return 0.0
    return max(0.0, (echantillons[-1][1] - o0) / duree)


class Copie:
    """Une file de copie, executee par un fil de fond, un fichier a la fois.

    L'etat vit en memoire et se lit par sondage (``etat``) ; l'etat de REPRISE,
    lui, est ecrit dans ``fichier_reprise`` a chaque point de controle et
    survit au processus. ``horloge`` et ``dormir`` sont injectables : les
    debits se verifient avec une horloge simulee plutot qu'avec de vraies
    attentes.

    ``instantane`` porte ce que l'etat de reprise doit rejouer (disque, oeuvres,
    options) ; ``reprise`` est le fichier en cours d'une copie precedente,
    tel que l'etat l'a enregistre. Il n'est repris qu'apres verification.
    """

    def __init__(
        self,
        disque: Disque,
        taches: list[Tache],
        *,
        max_mb_per_s: float | None = None,
        horloge: Callable[[], float] = time.monotonic,
        dormir: Callable[[float], object] | None = None,
        fichier_reprise: Path | None = None,
        instantane: dict[str, object] | None = None,
        reprise: dict[str, Any] | None = None,
    ) -> None:
        self.disque = disque
        self._taches = list(taches)
        self._max_mb_per_s = max_mb_per_s
        # Meme base que tout l'affichage de l'application (1 Mo = 1024 x 1024
        # octets) : en base 10, le plafond « 20 Mo/s » s'affichait 19,1 Mo/s.
        self._debit_max = max_mb_per_s * 1024 * 1024 if max_mb_per_s else None
        self._horloge = horloge
        self._arret = threading.Event()
        self._reprise_ok = threading.Event()
        self._reprise_ok.set()
        self._dormir = dormir if dormir is not None else self._arret.wait
        self._verrou = threading.Lock()
        self._fil: threading.Thread | None = None
        self._fichier_reprise = fichier_reprise
        self._instantane = instantane or {}
        self._reprise: dict[str, Any] | None = None
        self._racine: int | None = None
        """Descripteur de la racine du disque, ouvert au depart de la file."""

        if reprise is not None:
            # Le fichier a reprendre passe EN TETE : tant qu'il n'est pas
            # atteint, son ``.part`` n'est designe par aucun etat a jour.
            for i, tache in enumerate(self._taches):
                if tache.source_rel == reprise.get(
                    "source"
                ) and f"{tache.cible_rel}{SUFFIXE_PARTIEL}" == reprise.get("part"):
                    self._taches.insert(0, self._taches.pop(i))
                    self._reprise = reprise
                    break
            else:
                # Plus rien ne le reprendra : c'est un reste a nous, on le retire.
                if isinstance(reprise.get("part"), str) and supprimer_partiel(
                    disque, reprise["part"]
                ):
                    logger.info("fichier partiel abandonné supprimé : %s", reprise["part"])

        self.en_cours = False
        self._index = 0
        self._suivant = 0
        self._courant: dict[str, object] | None = None
        self._octets_fichier = 0
        self._debut_fichier = 0.0
        self._pause_fichier = 0.0
        self._fichiers_faits = 0
        self._octets_faits = 0
        self._octets_ecartes = 0
        self._octets_ecrits = 0
        self._faits: deque[dict[str, object]] = deque(maxlen=DERNIERS_FAITS)
        self._erreurs: list[dict[str, str]] = []
        self._arret_utilisateur = False
        self._abandon: str | None = None
        self._debut: str | None = None
        self._fin: str | None = None
        self._mesure_depuis = 0.0
        self._echantillons: deque[tuple[float, int]] = deque()
        self._echantillons_fichier: deque[tuple[float, int]] = deque()
        self._t_debit = 0.0
        self._octets_debit = 0

    # -- pilotage --

    def lancer(self) -> None:
        with self._verrou:
            self.en_cours = True
            self._debut = _maintenant_iso()
            self._redemarrer_mesures()
        self._persister(self._reprise)
        self._fil = threading.Thread(target=self.executer, daemon=True, name="copie-usb")
        self._fil.start()

    def attendre(self, delai: float | None = None) -> bool:
        """Attend la fin du fil. Vrai s'il est termine."""
        if self._fil is not None:
            self._fil.join(delai)
            return not self._fil.is_alive()
        return True

    def pause(self) -> None:
        if self.en_cours:
            self._reprise_ok.clear()

    def reprendre(self) -> None:
        self._reprise_ok.set()

    def arreter(self) -> None:
        if self.en_cours:
            self._arret.set()
        # Reveille une pause : un arret demande pendant une pause doit arreter.
        self._reprise_ok.set()

    # -- etat de reprise --

    def _persister(self, courant: dict[str, Any] | None) -> None:
        """Enregistre ou en est la file. Un echec est dit, jamais fatal."""
        if self._fichier_reprise is None:
            return
        with self._verrou:
            restantes = self._taches[self._index :]
            deja = int(courant["checkpoint"]) if courant else 0
        etat = {
            "version": VERSION_REPRISE,
            **self._instantane,
            "max_mb_per_s": self._max_mb_per_s,
            "saved_at": _maintenant_iso(),
            "files_remaining": len(restantes),
            "bytes_remaining": max(sum(t.taille for t in restantes) - deja, 0),
            "current": courant,
        }
        try:
            ecrire_reprise(self._fichier_reprise, etat)
        except OSError as exc:
            logger.warning("état de reprise non enregistré : %s", exc)

    def _point_enregistre(
        self,
        ecriture: int,
        tache: Tache,
        infos: os.stat_result,
        octets: int,
        *,
        forcer: bool = True,
    ) -> None:
        """``fsync``, PUIS l'etat : seul ce qui est force sur le disque compte.

        ``forcer=False`` au debut d'un fichier seulement : rien n'y a encore ete
        ecrit (ce qui precede une reprise l'a ete, et force, avant l'arret). On
        y enregistre seulement que ce ``.part`` est a nous — c'est ce qui permet
        de l'abandonner proprement — sans payer un ``fsync`` par sous-titre.
        """
        if forcer:
            os.fsync(ecriture)
        self._persister(
            {
                "source": tache.source_rel,
                "target": tache.cible_rel,
                "part": f"{tache.cible_rel}{SUFFIXE_PARTIEL}",
                "checkpoint": octets,
                "source_size": infos.st_size,
                "source_mtime_ns": infos.st_mtime_ns,
                "source_ino": infos.st_ino,
                "source_ctime_ns": infos.st_ctime_ns,
            }
        )

    def _depart(
        self, ecriture: int, source: int, tache: Tache, infos: os.stat_result
    ) -> tuple[int, str | None]:
        """Ou reprendre ce fichier, et pourquoi pas plus loin. Jamais a l'aveugle.

        Toutes les conditions, dans l'ordre : un etat qui decrit CE ``.part``
        pour CETTE source, une source au meme IDENTITE (taille, ``mtime_ns``,
        inode et ``ctime_ns`` : voir ``CHAMPS_POINT``), un ``.part`` au moins
        aussi long que le point de controle — tronque a celui-ci, ce qui suit
        n'etant pas repute ecrit —, et un contenu egal a la source sur tout le
        dernier segment et aux echantillons avant lui (voir ``_concorde``).
        Sinon : depuis zero, en le disant.
        """
        reprise, self._reprise = self._reprise, None
        deja_la = os.fstat(ecriture).st_size
        if reprise is None or reprise.get("source") != tache.source_rel:
            if deja_la:
                return (
                    0,
                    "fichier partiel sans point de contrôle enregistré : copie depuis le début",
                )
            return 0, None
        if (
            reprise.get("source_size") != infos.st_size
            or reprise.get("source_mtime_ns") != infos.st_mtime_ns
            or reprise.get("source_ino") != infos.st_ino
            or reprise.get("source_ctime_ns") != infos.st_ctime_ns
        ):
            return 0, "la source a changé depuis l'interruption : copie depuis le début"
        point = int(reprise["checkpoint"])
        if point <= 0:
            return 0, None
        if point > infos.st_size:
            return 0, "point de contrôle au-delà de la source : copie depuis le début"
        if deja_la < point:
            return 0, (
                "le fichier partiel est plus court que son point de contrôle (écriture perdue) : "
                "copie depuis le début"
            )
        os.ftruncate(ecriture, point)
        if not _concorde(ecriture, source, point):
            return 0, "le fichier partiel ne correspond pas à la source : copie depuis le début"
        return point, None

    # -- execution --

    def _redemarrer_mesures(self) -> None:
        maintenant = self._horloge()
        self._mesure_depuis = maintenant
        self._echantillons = deque([(maintenant, self._octets_ecrits)])
        self._echantillons_fichier = deque([(maintenant, self._octets_fichier)])
        self._t_debit = maintenant
        self._octets_debit = 0

    def _point_de_controle(self) -> None:
        """Entre deux blocs et entre deux fichiers : arret, pause, debit."""
        if self._arret.is_set():
            raise _Arret
        if not self._reprise_ok.is_set():
            debut_pause = self._horloge()
            while not self._reprise_ok.wait(0.2):
                pass
            if self._arret.is_set():
                raise _Arret
            with self._verrou:
                self._pause_fichier += self._horloge() - debut_pause
                self._redemarrer_mesures()

    def _attendre_debit(self, taille: int) -> None:
        """Retarde l'ecriture d'un bloc de ``taille`` octets sous le plafond.

        Le bloc est compte AVANT d'etre ecrit : on attend que sa FIN respecte
        le plafond. Attendre seulement apres coup laissait chaque bloc partir
        en rafale, un bloc entier au-dessus de la ligne du plafond — et le
        debit mesure le depassait. Appele apres la lecture, avec la taille
        reelle : appele avant, il attendait encore un bloc apres la fin du
        fichier.
        """
        if self._debit_max:
            while True:
                prevu = self._octets_debit + taille
                reste = self._t_debit + prevu / self._debit_max - self._horloge()
                if reste <= 0:
                    break
                self._dormir(min(reste, 0.5))
                if self._arret.is_set():
                    raise _Arret

    def _progression(self, octets: int) -> None:
        with self._verrou:
            maintenant = self._horloge()
            self._octets_fichier += octets
            self._octets_ecrits += octets
            self._octets_debit += octets
            self._echantillons.append((maintenant, self._octets_ecrits))
            self._echantillons_fichier.append((maintenant, self._octets_fichier))

    def _disque_perdu(self) -> str | None:
        """Motif d'arret si le disque n'est plus le volume monte du depart."""
        chemin = self.disque.path
        try:
            infos = _stat_volume(chemin)
        except OSError as exc:
            return (
                f"le disque « {self.disque.label} » ne répond plus ({exc.strerror or exc}) : "
                "copie arrêtée."
            )
        # Ce qui suit la virgule est vrai par construction, pas par espoir :
        # toutes les ecritures passent par la racine ouverte et verifiee au
        # depart (``_ouvrir_racine``), jamais par le chemin qu'on relit ici.
        pas_ailleurs = (
            "toutes ses écritures passent par le disque ouvert au départ, aucune n'a pu "
            "atteindre le dossier resté à sa place."
        )
        if infos.st_dev != self.disque.dev:
            return (
                f"le disque « {self.disque.label} » a changé de volume (débranché ?) : copie "
                f"arrêtée ; {pas_ailleurs}"
            )
        try:
            monte = _borne(lambda: os.path.ismount(chemin), chemin)
        except OSError:
            monte = False
        if not monte:
            return (
                f"le disque « {self.disque.label} » n'est plus monté (débranché ?) : copie "
                f"arrêtée ; {pas_ailleurs}"
            )
        return None

    def _ouvrir_la_racine(self) -> str | None:
        """Ouvre la racine du disque pour toute la file. Motif d'abandon, ou None.

        Le controle du chemin d'abord (borne dans le temps : un montage fige ne
        doit pas figer le fil sans rien dire), puis le descripteur, compare a
        la detection. Un echec ici arrete la file avant la moindre ecriture.
        """
        motif = self._disque_perdu()
        if motif:
            return motif
        try:
            self._racine = _borne(lambda: _ouvrir_racine(self.disque), self.disque.path)
        except _AutreVolume as exc:
            return (
                f"le disque « {self.disque.label} » {exc.strerror} : copie arrêtée avant "
                "d'écrire quoi que ce soit."
            )
        except OSError as exc:
            return (
                f"le disque « {self.disque.label} » ne s'ouvre pas ({exc.strerror or exc}) : "
                "copie arrêtée avant d'écrire quoi que ce soit."
            )
        return None

    def _sur_le_disque(self, dev: int, quoi: str) -> None:
        """Refuse d'ecrire ailleurs que sur le volume du disque."""
        if dev == self.disque.dev:
            return
        motif = self._disque_perdu()
        if motif:
            raise _DisqueDisparu(motif)
        raise _EchecFichier(f"« {quoi} » n'est pas sur le disque (autre volume) : refusé")

    def executer(self) -> None:
        complete = False
        try:
            motif = self._ouvrir_la_racine()
            if motif is not None:
                self._abandonner(None, motif)
            else:
                complete = self._executer_la_file()
        finally:
            if self._racine is not None:
                try:
                    os.close(self._racine)
                except OSError:
                    pass
                self._racine = None
            # Une file allee au bout n'a plus rien a reprendre ; une file
            # arretee ou abandonnee garde son etat : c'est tout son interet.
            if complete and self._fichier_reprise is not None:
                effacer_reprise(self._fichier_reprise)
            with self._verrou:
                self._courant = None
                self._octets_fichier = 0
                self.en_cours = False
                self._fin = _maintenant_iso()
            logger.info(
                "copie vers %s terminée : %s fichier(s) copié(s), %s erreur(s)",
                self.disque.label,
                self._fichiers_faits,
                len(self._erreurs),
            )

    def _executer_la_file(self) -> bool:
        """La file, un fichier a la fois. Vrai si elle est allee au bout."""
        for index, tache in enumerate(self._taches):
            with self._verrou:
                self._index = index
            try:
                self._point_de_controle()
                motif = self._disque_perdu()
                if motif:
                    raise _DisqueDisparu(motif)
                self._copier(tache)
            except _Arret:
                with self._verrou:
                    self._arret_utilisateur = True
                logger.info("copie vers %s arrêtée par l'utilisateur", self.disque.label)
                return False
            except _DisqueDisparu as exc:
                self._abandonner(tache, str(exc))
                return False
            except _EchecFichier as exc:
                self._noter_erreur(tache, str(exc))
                motif = self._disque_perdu()
                if motif is None and exc.disque_en_cause:
                    motif = exc.motif_disque or (
                        f"le disque « {self.disque.label} » ne suit plus ({exc}) : copie arrêtée."
                    )
                if motif:
                    self._abandonner(None, motif)
                    return False
            except Exception as exc:
                logger.exception("copie de %s interrompue", tache.source_rel)
                self._noter_erreur(tache, f"erreur inattendue : {exc}")
        return True

    def _noter_erreur(self, tache: Tache, message: str) -> None:
        logger.warning("copie de « %s » : %s", tache.source_rel, message)
        with self._verrou:
            self._erreurs.append(
                {"source": tache.source_rel, "target": tache.cible_rel, "message": message}
            )
            self._octets_ecartes += tache.taille
            self._courant = None
            self._octets_fichier = 0

    def _abandonner(self, tache: Tache | None, motif: str) -> None:
        logger.error("copie vers %s abandonnée : %s", self.disque.label, motif)
        if tache is not None:
            self._noter_erreur(tache, motif)
        with self._verrou:
            self._abandon = motif

    def _commencer(self, tache: Tache, taille: int, depart: int, note: str | None) -> None:
        with self._verrou:
            maintenant = self._horloge()
            self._suivant = self._index + 1
            self._courant = {
                "source": tache.source_rel,
                "target": tache.cible_rel,
                "bytes_total": taille,
                "started_at": _maintenant_iso(),
                "resumed_from_bytes": depart,
                "resume_note": note,
            }
            self._octets_fichier = depart
            self._debut_fichier = maintenant
            self._pause_fichier = 0.0
            self._echantillons_fichier = deque([(maintenant, depart)])
            # Le plafond repart a chaque fichier. Compte depuis le debut de la
            # file, il laissait un fichier « rattraper » le temps qu'un petit
            # fichier lent venait de perdre — 20,7 Mo/s sous un plafond de 20.
            self._t_debit = maintenant
            self._octets_debit = 0

    def _abandonner_partiel(self, dossier: int, nom: str) -> None:
        """Ce ``.part`` ne sera pas repris : on le retire, et l'etat ne le designe plus."""
        try:
            os.unlink(nom, dir_fd=dossier)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("fichier partiel « %s » non supprimé : %s", nom, exc)
        self._persister(None)

    def _ouvrir_dossier(self, relatif: PurePosixPath) -> int:
        """Le dossier d'accueil sur le disque, cree au besoin, par la racine ouverte."""
        if self._racine is None:
            raise _DisqueDisparu(
                f"le disque « {self.disque.label} » n'a pas été ouvert : copie arrêtée."
            )
        try:
            return _descendre(
                self._racine,
                relatif.parts,
                self.disque.dev,
                creer=True,
                identite=not self.disque.windows,
            )
        except _Refus as exc:
            raise _EchecFichier(str(exc)) from exc
        except OSError as exc:
            raise self._echec_ecriture(exc) from exc

    def _copier(self, tache: Tache) -> None:
        """Un fichier : ``.sortilege-part``, blocs, ``fsync``, taille, publication.

        Tout ce qui touche le disque passe par le descripteur du dossier
        d'accueil, lui-meme ouvert depuis la racine verifiee au depart : aucun
        chemin n'est relu entre le controle et l'ecriture.
        """
        relatif = PurePosixPath(tache.cible_rel)
        nom = relatif.name
        nom_partiel = nom + SUFFIXE_PARTIEL
        partiel_rel = f"{tache.cible_rel}{SUFFIXE_PARTIEL}"

        try:
            source = os.open(tache.source, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise _EchecFichier(f"source illisible ({exc.strerror or exc})") from exc
        try:
            infos = os.fstat(source)
            dossier = self._ouvrir_dossier(relatif.parent)
            try:
                self._copier_dans(tache, source, infos, dossier, nom, nom_partiel, partiel_rel)
            finally:
                os.close(dossier)
        finally:
            os.close(source)

    def _copier_dans(
        self,
        tache: Tache,
        source: int,
        infos: os.stat_result,
        dossier: int,
        nom: str,
        nom_partiel: str,
        partiel_rel: str,
    ) -> None:
        taille = infos.st_size
        try:
            deja = _existe(dossier, nom)
        except OSError as exc:
            raise self._echec_ecriture(exc) from exc
        if deja:
            raise _EchecFichier(f"« {tache.cible_rel} » existe déjà sur le disque : jamais écrasé")

        try:
            # Sans O_TRUNC : un ``.part`` existant est peut-etre a reprendre.
            # Il est tronque plus bas, a son point de controle ou a zero.
            ecriture = os.open(
                nom_partiel,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o644,
                dir_fd=dossier,
            )
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                raise _EchecFichier(f"{REFUS_LIEN} (« {partiel_rel} »)") from exc
            raise self._echec_ecriture(exc) from exc

        try:
            try:
                self._sur_le_disque(os.fstat(ecriture).st_dev, partiel_rel)
                depart, note = self._depart(ecriture, source, tache, infos)
                if depart == 0:
                    os.ftruncate(ecriture, 0)
                else:
                    logger.info("reprise de « %s » à %s octets", tache.cible_rel, depart)
                if note:
                    logger.warning("« %s » : %s", tache.cible_rel, note)
                os.lseek(ecriture, depart, os.SEEK_SET)
                try:
                    os.lseek(source, depart, os.SEEK_SET)
                except OSError as exc:
                    raise _SourceIllisible(exc.strerror or str(exc)) from exc
                self._commencer(tache, taille, depart, note)
                self._point_enregistre(ecriture, tache, infos, depart, forcer=False)
                ecrits = self._transferer(source, ecriture, tache, infos, depart)
                if not self.disque.windows:
                    rend_lisible_avant_publication(ecriture, tache.cible)
                    # Le ``.part`` prend l'identite de son dossier AVANT d'etre
                    # publie, par son descripteur : voir ``_adopter_identite``.
                    _adopter_identite(ecriture, dossier, tache.cible_rel)
                os.fsync(ecriture)
                sur_disque = os.fstat(ecriture).st_size
            finally:
                os.close(ecriture)
        except _Arret:
            # Le point de controle vient d'etre pose : le ``.part`` et
            # l'etat restent, la copie se reprendra ici.
            raise
        except _DisqueDisparu:
            raise
        except _SourceIllisible as exc:
            # La mediatheque, pas le disque : ce fichier echoue, la file
            # continue. Son ``.part`` ne sera plus repris par personne.
            self._abandonner_partiel(dossier, nom_partiel)
            raise _EchecFichier(f"lecture de la source impossible ({exc})") from exc
        except _EchecFichier as exc:
            if not exc.disque_en_cause:
                self._abandonner_partiel(dossier, nom_partiel)
            raise
        except OSError as exc:
            echec = self._echec_ecriture(exc)
            if not echec.disque_en_cause:
                self._abandonner_partiel(dossier, nom_partiel)
            raise echec from exc

        if ecrits != taille or sur_disque != taille:
            self._abandonner_partiel(dossier, nom_partiel)
            raise _EchecFichier(
                f"taille incorrecte après copie ({sur_disque} octets écrits, {taille} "
                "attendus) : fichier partiel supprimé"
            )

        # Publication : SEULEMENT si le nom definitif est toujours libre. Un
        # fichier apparu entre-temps n'est jamais ecrase.
        try:
            apparu = _existe(dossier, nom)
            if not apparu:
                os.rename(nom_partiel, nom, src_dir_fd=dossier, dst_dir_fd=dossier)
        except OSError as exc:
            echec = self._echec_ecriture(exc)
            if not echec.disque_en_cause:
                self._abandonner_partiel(dossier, nom_partiel)
            raise echec from exc
        if apparu:
            self._abandonner_partiel(dossier, nom_partiel)
            raise _EchecFichier(
                f"« {tache.cible_rel} » est apparu sur le disque pendant la copie : jamais écrasé"
            )
        _fsync_dossier(dossier)

        with self._verrou:
            duree = max(self._horloge() - self._debut_fichier - self._pause_fichier, 0.0)
            ecrits_ici = (
                taille - int(self._courant["resumed_from_bytes"]) if self._courant else taille
            )
            self._fichiers_faits += 1
            self._octets_faits += taille
            self._octets_fichier = 0
            self._courant = None
            self._faits.append(
                {
                    "target": tache.cible_rel,
                    "bytes": taille,
                    "speed_bps": round(ecrits_ici / duree) if duree > 0 else None,
                    "duration_s": round(duree, 3),
                }
            )
            self._index += 1
        self._persister(None)

    def _echec_ecriture(self, exc: OSError) -> _EchecFichier:
        du_disque = exc.errno in ERREURS_DU_DISQUE
        return _EchecFichier(
            _message_ecriture(exc),
            disque_en_cause=du_disque,
            motif_disque=_motif_disque(self.disque.label, exc) if du_disque else None,
        )

    def _transferer(
        self, source: int, ecriture: int, tache: Tache, infos: os.stat_result, depart: int
    ) -> int:
        """Blocs de ``TAILLE_BLOC`` ; arret, pause et debit entre chacun ; points de controle."""
        ecrits = depart
        dernier_point = depart
        while True:
            try:
                self._point_de_controle()
            except _Arret:
                # Un dernier point de controle : l'arret garde tout ce qui est
                # ecrit, et la reprise repartira exactement d'ici.
                self._point_enregistre(ecriture, tache, infos, ecrits)
                raise
            try:
                bloc = os.read(source, TAILLE_BLOC)
            except OSError as exc:
                # Erreur de LECTURE : c'est la mediatheque qui a un probleme,
                # pas le disque — ce fichier echoue, la file continue.
                raise _EchecFichier(f"lecture de la source impossible ({exc.strerror})") from exc
            if not bloc:
                return ecrits
            try:
                self._attendre_debit(len(bloc))
            except _Arret:
                self._point_enregistre(ecriture, tache, infos, ecrits)
                raise
            vue = memoryview(bloc)
            while vue:
                n = os.write(ecriture, vue)
                vue = vue[n:]
            ecrits += len(bloc)
            self._progression(len(bloc))
            if ecrits - dernier_point >= INTERVALLE_POINT_DE_CONTROLE:
                self._point_enregistre(ecriture, tache, infos, ecrits)
                dernier_point = ecrits

    # -- lecture --

    def etat(self) -> dict[str, object]:
        with self._verrou:
            maintenant = self._horloge()
            en_pause = self.en_cours and not self._reprise_ok.is_set()
            vitesse = 0.0 if en_pause else _debit(self._echantillons, maintenant, FENETRE_GLOBALE)
            octets_faits = self._octets_faits + self._octets_fichier
            total = sum(t.taille for t in self._taches)
            reste = max(total - octets_faits - self._octets_ecartes, 0)
            mesure = maintenant - self._mesure_depuis
            eta = (
                round(reste / vitesse)
                if self.en_cours and vitesse > 0 and mesure >= MESURE_MINIMALE_ETA
                else None
            )
            # None entre deux fichiers, en pause entre deux fichiers, et apres
            # la fin : un objet vide s'affichait « 0 Mo sur 0 Mo », et faisait
            # croire a l'ecran qu'un fichier (sans nom) avait commence.
            courant: dict[str, object] | None = None
            if self._courant is not None:
                courant = {
                    "source": self._courant["source"],
                    "target": self._courant["target"],
                    "bytes_done": self._octets_fichier,
                    "bytes_total": self._courant["bytes_total"],
                    "speed_bps": 0
                    if en_pause
                    else round(_debit(self._echantillons_fichier, maintenant, FENETRE_FICHIER)),
                    "started_at": self._courant["started_at"],
                    "resumed_from_bytes": self._courant["resumed_from_bytes"],
                    "resume_note": self._courant["resume_note"],
                }
            debut = self._suivant if self._courant is not None else self._index
            file = (
                [
                    {"target": t.cible_rel, "bytes": t.taille}
                    for t in self._taches[debut : debut + FILE_VISIBLE]
                ]
                if self.en_cours
                else []
            )
            return {
                "running": self.en_cours,
                "paused": en_pause,
                "stopping": self.en_cours and self._arret.is_set(),
                "disk": self.disque.to_dict(),
                "current": courant,
                "files_done": self._fichiers_faits,
                "files_total": len(self._taches),
                "bytes_done": octets_faits,
                "bytes_total": total,
                "speed_bps": round(vitesse),
                "eta_s": eta,
                "queue_next": file,
                "done": list(self._faits),
                "errors": list(self._erreurs),
                "stopped_by_user": self._arret_utilisateur,
                "aborted": self._abandon,
                "started_at": self._debut,
                "finished_at": self._fin,
            }


def etat_au_repos() -> dict[str, object]:
    """L'etat quand aucune copie n'a encore ete lancee depuis le demarrage."""
    return {
        "running": False,
        "paused": False,
        "stopping": False,
        "disk": None,
        "current": None,
        "files_done": 0,
        "files_total": 0,
        "bytes_done": 0,
        "bytes_total": 0,
        "speed_bps": 0,
        "eta_s": None,
        "queue_next": [],
        "done": [],
        "errors": [],
        "stopped_by_user": False,
        "aborted": None,
        "started_at": None,
        "finished_at": None,
    }


def octets_lisibles(octets: int) -> str:
    """Version publique, pour les messages de la couche HTTP."""
    return _octets_lisibles(octets)
