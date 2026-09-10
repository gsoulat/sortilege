"""Empreinte OpenSubtitles : identifier un fichier par son CONTENU.

Tout le reste de cette application devine. Le parseur lit un nom de release,
le probe lit des tags, le resolveur IA propose une meilleure requete — mais
tous partent de ce que le fichier RACONTE de lui-meme, et un nom de release
ment regulierement : titre traduit, saison decalee, film homonyme, faute du
groupe qui l'a publie.

L'empreinte ne devine pas. Deux fichiers de meme empreinte sont le meme
encodage, octet pour octet ou presque, et la base d'OpenSubtitles associe ces
empreintes a des oeuvres identifiees. Un fichier reconnu ainsi n'a plus besoin
d'etre interprete : on SAIT ce que c'est.

C'est ce qui manque a Sortilege face a FileBot, et ce qui separe une
identification probable d'une identification certaine.

L'algorithme est celui d'OpenSubtitles, en usage depuis vingt ans :

    taille du fichier + somme des 64 premiers Kio + somme des 64 derniers Kio

sur des entiers 64 bits non signes. Il ne lit que 128 Kio, quelle que soit la
taille du fichier — c'est pourquoi il reste utilisable sur une bibliotheque
entiere la ou un vrai condensat demanderait de tout relire.

Sa faiblesse est le revers de cette force : deux fichiers dont seul le milieu
differe partagent la meme empreinte. En pratique cela n'arrive pas sur des
videos, ou le debut et la fin different toujours des qu'un encodage change.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

logger = logging.getLogger(__name__)

CHUNK = 64 * 1024
"""Taille lue a chaque extremite. Fixee par l'algorithme, pas ajustable : la
changer produirait des empreintes que personne d'autre ne reconnait."""

MIN_SIZE = CHUNK * 2
"""En dessous, l'empreinte n'a aucun sens : les deux morceaux se
chevaucheraient et le fichier serait de toute facon trop petit pour etre une
video."""

_UINT64 = 0xFFFFFFFFFFFFFFFF


def compute(path: Path) -> str | None:
    """Empreinte du fichier, ou None si elle ne peut pas etre calculee.

    Renvoie None plutot que de lever : une empreinte est un signal en plus, et
    son absence ne doit jamais interrompre un scan. Un fichier illisible, trop
    court ou en cours de telechargement se contentera des autres signaux.
    """
    try:
        taille = path.stat().st_size
    except OSError:
        return None

    if taille < MIN_SIZE:
        return None

    empreinte = taille
    try:
        with path.open("rb") as handle:
            empreinte = _somme(handle, empreinte)
            handle.seek(max(0, taille - CHUNK))
            empreinte = _somme(handle, empreinte)
    except OSError as exc:
        logger.debug("empreinte impossible pour %s : %s", path.name, exc)
        return None

    return f"{empreinte & _UINT64:016x}"


def _somme(handle, depart: int) -> int:
    """Additionne 64 Kio par mots de 8 octets, en arithmetique 64 bits.

    Le masquage a chaque tour n'est pas cosmetique : Python n'a pas de
    debordement, et sans lui l'entier grandirait indefiniment pour donner une
    valeur qu'aucune autre implementation ne retrouverait.
    """
    total = depart
    donnees = handle.read(CHUNK)
    for (mot,) in struct.iter_unpack("<q", donnees[: len(donnees) // 8 * 8]):
        total = (total + mot) & _UINT64
    return total
