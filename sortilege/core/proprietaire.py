"""A qui appartient ce que Sortilege depose, et qui a le droit de le lire.

Le cas reel : sur un NAS, les dossiers du client de telechargement
appartiennent a 999, la bibliotheque a 1000, le serveur multimedia tourne
encore sous un troisieme compte. Aucune identite ordinaire n'ecrit des deux
cotes — sauf root. Mais un processus root depose des fichiers root:root, que
l'utilisateur ne peut plus ni renommer ni supprimer depuis son partage reseau :
on remplacerait un blocage par un autre, plus sournois puisqu'il n'apparait
qu'apres coup.

D'ou la regle unique de ce module : **ce qui entre dans la bibliotheque prend
l'identite du dossier qui l'accueille**, jamais celle du processus ni celle de
la source. Le dossier d'accueil est le seul temoin fiable de qui possede
reellement cette bibliotheque, et c'est l'identite que portent deja tous ses
voisins.

**Pourquoi un module a part.** Ces fonctions sont nees dans ``core/journal``,
le passage unique des deplacements. Mais tout ce que Sortilege pose a cote d'un
media — fiches ``.nfo``, affiches, manifestes, sous-titres — ne passe PAS par un
deplacement : c'est ecrit sur place, par ``core/nfo`` et ``core/subtitles``. Or
``journal`` importe ``nfo`` ; leur faire importer ``journal`` en retour fermerait
un cycle. Un module neutre, qui n'importe rien du paquet, brise la boucle et
dit du meme coup que la regle ne concerne pas que les deplacements.

**Rien ici n'est bloquant.** Une identite qu'on n'a pas pu poser laisse un
fichier correctement range, au contenu intact, avec la mauvaise etiquette. Un
``chown`` la repare. Declarer le rangement en echec pour autant couterait le
travail utile pour un detail reparable : ces fonctions avalent donc les refus du
systeme de fichiers — les ``OSError``, et elles seules — et se contentent de les
dire. Tout le reste remonte : une erreur de programmation ici est un defaut a
corriger, pas un incident a journaliser, et une interruption doit interrompre.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

MODE_FICHIER_DEPOSE = 0o644
"""Droits poses sur un fichier que Sortilege ecrit a cote d'un media.

Fixe, et volontairement pas ``0o666 & ~umask`` comme le ferait une ouverture
ordinaire. Trois raisons :

1. **Ce fichier n'est pas ecrit pour nous.** Une fiche ``.nfo``, une affiche, un
   sous-titre n'existent que pour etre relus par un serveur multimedia qui
   tourne sous un AUTRE compte. Un umask restrictif — 077, courant sur un NAS
   durci — produirait un 0600 parfaitement conforme et parfaitement inutile :
   exactement le defaut qu'on vient corriger.
2. **Le umask ne se lit pas sans le changer.** ``os.umask`` est un
   get-and-set : le relever demande de le poser, et l'application ecrit depuis
   plusieurs fils. La fenetre pendant laquelle un autre fil creerait ses
   fichiers avec le mauvais masque est courte mais reelle, pour une information
   dont on vient de dire qu'on ne veut pas la suivre.
3. **L'ecriture reste reservee au proprietaire.** Apres adoption, c'est celui de
   la bibliotheque : lecture pour tous, ecriture pour lui seul, ce que tout le
   monde attend d'un fichier de metadonnees.
"""


def tourne_en_root() -> bool:
    """Le processus a-t-il l'identite root ?

    Pose a chaque depot, et la reponse est « non » sur toute installation
    ordinaire : tout ce qui en depend doit donc etre gratuit dans ce cas — un
    appel systeme, aucun stat, aucun message.
    """
    return os.geteuid() == 0


def adopte_identite_du_dossier(cible: Path, dossier: Path) -> None:
    """Donne a ``cible`` le proprietaire et le groupe de ``dossier``.

    N'existe QUE pour le mode root, et ne fait rien ailleurs (voir l'en-tete du
    module pour le cas reel).

    Le dossier d'ACCUEIL sert de modele, et non l'ancien proprietaire du
    fichier : reprendre celle de la source ramenerait l'identite des
    telechargements dans la bibliotheque, un dossier sur deux.

    Un echec n'interrompt RIEN. Le depot, lui, a reussi : transformer un
    rangement fait en rangement rate parce qu'une etiquette n'a pas pu changer
    couterait le travail utile pour un detail qu'un ``chown`` repare.
    """
    if not tourne_en_root():
        return
    try:
        info = dossier.stat()
    except OSError as exc:
        logger.warning(
            "identite de « %s » illisible, « %s » reste a root : %s", dossier, cible, exc
        )
        return
    try:
        # follow_symlinks=False : un compagnon peut etre un lien symbolique, et
        # suivre le lien changerait le proprietaire d'un fichier qu'on n'a pas
        # deplace — souvent hors de la bibliotheque.
        os.chown(cible, info.st_uid, info.st_gid, follow_symlinks=False)
    except OSError as exc:
        logger.warning(
            "« %s » garde son proprietaire au lieu de %s:%s : %s",
            cible,
            info.st_uid,
            info.st_gid,
            exc,
        )


def cree_dossier_d_accueil(dossier: Path) -> None:
    """Cree l'arborescence manquante, a l'identite de son premier parent existant.

    Les dossiers comptent autant que les fichiers, et meme davantage : sous
    Unix, c'est le dossier CONTENANT qui donne le droit de renommer ou de
    supprimer. Un « Season 01 » cree par root sous une serie qui appartient a
    l'utilisateur lui retirerait toute prise sur ce qu'il contient, quand bien
    meme les fichiers, eux, porteraient la bonne identite.

    Le modele est releve AVANT la creation : apres, chaque niveau cree existe
    deja et appartient a root, il ne dit plus rien de ce que la bibliotheque
    attendait.
    """
    if not tourne_en_root():
        dossier.mkdir(parents=True, exist_ok=True)
        return

    modele = dossier
    while not modele.exists() and modele != modele.parent:
        modele = modele.parent

    # Course examinee, et laissee telle quelle a dessein : entre ce releve et le
    # mkdir, un tiers peut creer un maillon intermediaire, et le modele retenu
    # n'est alors plus le parent immediat des niveaux crees. Aucune elevation
    # n'en decoule — l'identite posee reste celle d'un ancetre qui EXISTAIT
    # deja, jamais une identite choisie par le tiers. Fermer la fenetre
    # demanderait de creer les niveaux un a un en relevant un modele a chaque
    # etage ; ce serait plus de code pour la meme garantie.
    dossier.mkdir(parents=True, exist_ok=True)

    courant = dossier
    while courant != modele and courant != courant.parent:
        adopte_identite_du_dossier(courant, modele)
        courant = courant.parent


def rend_lisible_avant_publication(descripteur: int, cible: Path) -> None:
    """Pose ``MODE_FICHIER_DEPOSE`` sur un fichier ENCORE OUVERT, par son descripteur.

    Ce defaut-ci n'a RIEN a voir avec root et frappe toutes les installations :
    ``tempfile.NamedTemporaryFile`` cree en 0600, par securite, parce qu'un
    temporaire est cense rester prive. Publie tel quel sous son nom definitif,
    le fichier n'est plus temporaire ni prive : c'est une fiche que le serveur
    multimedia, sous un autre compte, doit pouvoir lire — et ne peut pas. Le
    symptome est muet cote Sortilege (le depot a reussi) et incomprehensible
    cote serveur (le fichier existe, il est simplement invisible pour lui).

    **Pourquoi un descripteur et non un chemin.** Poser le mode APRES le
    ``os.replace``, sur le nom definitif, ouvre une fenetre : entre la
    publication et le ``chmod``, un tiers qui ecrit dans ce dossier remplace le
    nom par un lien symbolique, et c'est le fichier pointe — ailleurs, a lui
    inaccessible jusque-la — qui passe en 0644. Un ``chown`` se protege avec
    ``follow_symlinks=False`` ; un ``chmod`` ne peut pas, Linux n'a pas de
    ``lchmod``. Le descripteur, lui, designe l'inode qu'on vient d'ecrire : il
    ne peut pas etre detourne, et le fichier est publie avec ses droits finaux
    plutot que corrige apres coup. Le mode est pose avant le ``fsync`` pour
    qu'il soit rendu durable avec le contenu.

    ``cible`` n'est la que pour le message : c'est le nom sous lequel le
    fichier va etre publie, le seul que l'utilisateur reconnaitra. Un
    descripteur ne se nomme pas.

    Un echec est dit, jamais leve : le contenu est bon et sa place est bonne.
    """
    try:
        os.fchmod(descripteur, MODE_FICHIER_DEPOSE)
    except OSError as exc:
        logger.warning(
            "« %s » garde ses droits au lieu de %o, un autre compte pourrait ne pas le lire : %s",
            cible,
            MODE_FICHIER_DEPOSE,
            exc,
        )
