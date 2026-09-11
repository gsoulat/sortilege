"""Ecrire ce que Sortilege a decide, la ou les serveurs multimedia le lisent.

Jusqu'ici le flux d'information n'allait que dans un sens : ``core/probe`` LIT
les ``.nfo`` poses par Radarr ou tinyMediaManager, et ``core/companions``
deplace les jaquettes trouvees. Sortilege n'en produisait aucun. Un arbitrage
rendu a la main — « non, ce Dark Matter est celui de 2015 » — mourait donc dans
la file de revue : le serveur multimedia refaisait sa propre identification et
retombait sur la meme erreur au rafraichissement suivant.

**Ce que change un ``.nfo``, et pourquoi c'est plus qu'un fichier de plus.**
Jellyfin recupere TOUJOURS les metadonnees locales et leur donne la PRIORITE
sur les fournisseurs distants ; ce comportement ne se desactive pas. Plex lit
nativement le format Kodi depuis sa version 1.43.1. Un ``.nfo`` ecrit ici n'est
donc pas une suggestion adressee au serveur : il fait autorite.

Trois regles decoulent de cette autorite, et gouvernent tout le module.

1. **Un element vide n'est jamais ecrit.** Un element ABSENT laisse le
   fournisseur distant completer le champ ; un element VIDE ecrase ce qu'il
   aurait trouve par du vide, definitivement. La difference est invisible dans
   le fichier et considerable a l'ecran.
2. **L'ecriture est atomique.** Fichier temporaire dans le meme dossier, puis
   renommage. Un ``.nfo`` tronque par une coupure serait relu comme faisant
   autorite : il est pire qu'absent.
3. **Rien n'ecrase un ``.nfo`` existant sans qu'on l'ait demande, et rien ne
   le detruit meme quand on l'a demande.** Quelqu'un a pu le poser a la main,
   ou tinyMediaManager l'a ecrit avec bien plus de details que nous n'en
   avons. Le defaut est donc de ne pas y toucher ; et quand on le remplace,
   l'ancien part en corbeille, ou a cote sous un nom qui n'ecrase rien.
4. **On n'ecrit que ce qu'on sait MIEUX que le serveur.** L'identite et la
   numerotation, oui : elles sortent d'un arbitrage humain ou d'une conversion
   depuis un numero absolu, et rien ne permettrait au serveur de les retrouver.
   Le synopsis, les genres, le studio : non — il les telecharge complets, la ou
   nous n'en avons qu'un extrait garde pour departager deux homonymes a
   l'ecran. Les inscrire figerait, avec autorite, une phrase coupee au milieu.
   Le format les prevoit et ce module sait les ecrire ; la fiche construite
   depuis un plan les laisse simplement vides.

Aucune dependance nouvelle : ``xml.etree`` suffit a produire ce XML, comme il
suffit deja a lire les manifestes EPUB dans ``core/ebook``.
"""

from __future__ import annotations

import errno
import logging
import os
import re
import shutil
import tempfile
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from xml.etree import ElementTree

import httpx

from .companions import send_to_trash
from .probe import FileProbe, parse_nfo, read_nfo_file

if TYPE_CHECKING:  # pragma: no cover - uniquement pour le typage
    from .planner import Plan
    from .scanner import ScannedFile

logger = logging.getLogger(__name__)

XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
"""En-tete que Kodi ecrit lui-meme. Reproduit a l'identique : certains outils
de la famille reconnaissent leurs fichiers a cette premiere ligne."""

# Dossier de saison tel que NOS gabarits l'ecrivent — « Season 01 », « Saison 2 ».
# Le meme motif existe dans core/journal, et la duplication est deliberee : ce
# module y REMONTE pour trouver la racine d'une serie, l'autre y DESCEND pour
# ne pas supprimer un dossier encore utile. Les deux usages n'ont aucune raison
# d'evoluer ensemble, et les lier ferait dependre le journal de ce module.
_SEASON_FOLDER = re.compile(r"^s(?:aison|eason)?[\s._-]*\d{1,2}$", re.IGNORECASE)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

IMAGE_TIMEOUT = 15.0
"""Delai BORNE pour une affiche. Le rangement est deja fait quand on arrive
ici : attendre une image plus longtemps que cela reviendrait a faire payer au
lot entier un serveur d'images lent."""

MAX_IMAGE_BYTES = 12 * 1024 * 1024
"""Plafond de ce qu'on accepte d'ecrire. Une affiche pese quelques centaines de
kilo-octets ; au-dela, ce n'est plus une affiche, et la bibliotheque n'est pas
l'endroit ou le decouvrir."""

# Ces deux noms sont lus par Jellyfin ET par Plex, ce qui evite d'avoir a
# deposer deux jeux de fichiers pour deux serveurs.
POSTER_NAME = "poster.jpg"
FANART_NAME = "fanart.jpg"


class OnExisting(StrEnum):
    """Que faire quand un fichier depose (fiche, affiche, manifeste) est deja la."""

    SKIP = "skip"
    """Ne pas y toucher. Defaut : le fichier existant peut etre le fruit d'un
    travail bien plus soigne que le notre."""

    BACKUP = "backup"
    """Ecrire, en mettant l'ancien de cote. Le compromis pour qui veut nos
    donnees sans jeter ce qui etait la.

    L'ancien part en corbeille quand elle est connue. Sans corbeille, il est
    decale sous « .nfo.bak » — et sous un nom horodate si ce « .bak » existe
    deja : au second passage, il contient l'ORIGINAL, celui qu'on voulait
    justement garder, et l'ecraser par notre propre fiche du premier passage
    reviendrait a le perdre."""

    OVERWRITE = "overwrite"
    """Remplacer. Demande explicitement, jamais par defaut.

    Remplacer n'est pas detruire : l'ancien part en corbeille, ou a cote sous
    un nom horodate faute de corbeille. La seule difference avec BACKUP est de
    ne pas promettre de « .bak » a cote du media."""


ON_EXISTING_CHOICES: tuple[str, ...] = tuple(str(c) for c in OnExisting)


class Outcome(StrEnum):
    """Ce qui a reellement ete fait d'un fichier."""

    WRITTEN = "written"
    REPLACED = "replaced"
    BACKED_UP = "backed_up"
    SKIPPED = "skipped"


@dataclass
class LocalMetadataSettings:
    """Depot de metadonnees locales a cote des medias.

    « Local metadata » est le vocabulaire de Jellyfin lui-meme, et il dit
    exactement de quoi il s'agit : des donnees qui vivent dans la bibliotheque
    et qui priment sur celles du reseau.

    Tout est DESACTIVE par defaut, et ce n'est pas de la prudence decorative :
    ecrire des fichiers dans la bibliotheque de quelqu'un est un geste qui doit
    etre demande. Un utilisateur qui met a jour son image ne s'attend pas a
    retrouver deux cents fichiers nouveaux le lendemain.
    """

    nfo: bool = False
    """Ecrire les fiches XML apres un rangement reussi."""

    artwork: bool = False
    """Deposer « poster.jpg » et « fanart.jpg » a cote du media."""

    opf: bool = False
    """Ecrire « metadata.opf » et la couverture a cote d'un LIVRE.

    Reglage distinct de ``nfo`` et non un alias : Jellyfin ne lit aucun ``.nfo``
    pour les livres, il attend le format de Calibre. Les confondre ferait croire
    qu'activer les fiches suffit, et rien n'apparaitrait sur les livres."""

    on_existing: str = OnExisting.SKIP
    """Conduite face a un ``.nfo`` deja present. Voir ``OnExisting``."""


@dataclass(slots=True)
class MediaInfo:
    """Ce qu'on sait d'une oeuvre, sous la forme que le format Kodi attend.

    Volontairement independante de ``Plan`` : la meme fiche se construit depuis
    un plan qu'on vient d'appliquer ou depuis un fichier deja range dont on ne
    connait que ce qu'il declare. Un seul producteur de XML, deux sources.
    """

    title: str = ""
    """Titre de l'OEUVRE : le film, ou la serie. Jamais celui de l'episode."""

    original_title: str = ""
    year: int | None = None

    # Champs que le format prevoit et que le serveur telecharge mieux que nous
    # — voir la regle 4 du module. Ecrits s'ils sont renseignes ; la fiche
    # construite depuis un plan les laisse vides, faute d'en tenir mieux que ce
    # qu'un fournisseur ira chercher lui-meme.
    plot: str = ""
    genres: list[str] = field(default_factory=list)
    studios: list[str] = field(default_factory=list)

    tmdb_id: str = ""
    imdb_id: str = ""
    tvdb_id: str = ""

    season: int | None = None
    episode: int | None = None
    episode_title: str = ""

    poster_url: str = ""
    backdrop_url: str = ""


# --- Production du XML ------------------------------------------------------


def _text(parent: ElementTree.Element, tag: str, value: object, **attrs: str) -> None:
    """Ajoute un element, SAUF si sa valeur est vide.

    C'est la regle 1 du module, appliquee en un seul endroit pour qu'aucun
    appelant ne puisse l'oublier : un element vide n'informe pas, il efface.
    """
    if value is None or value == "":
        return
    child = ElementTree.SubElement(parent, tag, attrs)
    child.text = str(value)


def _unique_ids(root: ElementTree.Element, info: MediaInfo) -> None:
    """Identifiants declares, du plus fiable au moins.

    Le PREMIER ecrit porte ``default="true"`` : c'est celui que le lecteur
    retiendra pour aller chercher le reste. N'en marquer aucun laisserait le
    choix au hasard de l'ordre de lecture, et TMDB est notre source.
    """
    premier = True
    for kind, value in (("tmdb", info.tmdb_id), ("imdb", info.imdb_id), ("tvdb", info.tvdb_id)):
        if not value:
            continue
        attrs = {"type": kind}
        if premier:
            attrs["default"] = "true"
            premier = False
        _text(root, "uniqueid", value, **attrs)


def _serialise(root: ElementTree.Element) -> str:
    ElementTree.indent(root, space="  ")
    return f"{XML_HEADER}\n{ElementTree.tostring(root, encoding='unicode')}\n"


def movie_xml(info: MediaInfo) -> str:
    """Fiche d'un film, racine ``<movie>``."""
    root = ElementTree.Element("movie")
    _text(root, "title", info.title)
    _text(root, "originaltitle", info.original_title)
    _text(root, "year", info.year)
    _text(root, "plot", info.plot)
    for genre in info.genres:
        _text(root, "genre", genre)
    for studio in info.studios:
        _text(root, "studio", studio)
    _unique_ids(root, info)
    return _serialise(root)


def tvshow_xml(info: MediaInfo) -> str:
    """Fiche de serie, racine ``<tvshow>``, posee a la racine de la serie.

    Elle porte l'identite : c'est elle qui evite au serveur de redecouvrir la
    serie a chaque saison ajoutee, et donc de se tromper une fois de plus.
    """
    root = ElementTree.Element("tvshow")
    _text(root, "title", info.title)
    _text(root, "originaltitle", info.original_title)
    _text(root, "year", info.year)
    _text(root, "plot", info.plot)
    for genre in info.genres:
        _text(root, "genre", genre)
    for studio in info.studios:
        _text(root, "studio", studio)
    _unique_ids(root, info)
    return _serialise(root)


def episode_xml(info: MediaInfo) -> str:
    """Fiche d'un episode, racine ``<episodedetails>``.

    AUCUN ``uniqueid`` ici, et c'est delibere : le seul identifiant qu'on
    possede est celui de la SERIE. L'inscrire dans un episode ferait chercher
    au lecteur un episode sous un numero de serie — une identification fausse
    presentee comme certaine, exactement ce que ce module existe pour eviter.

    La numerotation, elle, est ce qu'on a de plus precieux a transmettre : elle
    resulte parfois d'une conversion depuis un numero absolu que le serveur ne
    saurait pas refaire seul.
    """
    root = ElementTree.Element("episodedetails")
    _text(root, "title", info.episode_title)
    _text(root, "showtitle", info.title)
    _text(root, "season", info.season)
    _text(root, "episode", info.episode)
    _text(root, "plot", info.plot)
    return _serialise(root)


# --- Ecriture ---------------------------------------------------------------


def _write_atomic(
    path: Path, payload: bytes, *, avant_publication: Callable[[], None] | None = None
) -> None:
    """Ecrit par fichier temporaire puis renommage.

    Le temporaire est cree DANS le dossier de destination : ``os.replace`` n'est
    atomique qu'a l'interieur d'un meme systeme de fichiers, et passer par
    /tmp — souvent un volume distinct dans un conteneur — retirerait
    precisement la garantie qu'on vient chercher.

    Le ``fsync`` suit le meme raisonnement que celui du journal : sans lui, le
    contenu peut n'exister qu'en cache pendant que le nom, lui, est deja
    publie. Une coupure a cet instant laisse un fichier de taille nulle qui
    sera relu comme faisant autorite.

    ``avant_publication`` s'execute une fois le contenu entierement sur le
    disque, juste avant le renommage : c'est la qu'on met l'ancien fichier de
    cote. Plus tot, un echec d'ecriture laisserait la bibliotheque sans aucune
    des deux versions.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".sortilege-", suffix=".tmp", delete=False
    )
    temporaire = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if avant_publication is not None:
            avant_publication()
        os.replace(temporaire, path)
    except BaseException:
        temporaire.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class Placement:
    """Ce qu'est devenu un fichier depose, et ou est parti celui qu'il remplace."""

    outcome: Outcome
    set_aside: Path | None = None
    """Ou l'ancien fichier a ete mis de cote (corbeille ou sauvegarde a cote),
    None s'il n'y en avait pas. C'est ce qui permet de le journaliser, donc de
    le rendre a l'annulation."""


def _nom_de_sauvegarde(path: Path, *, horodate: bool) -> Path:
    """Un nom libre a cote du fichier, qui n'en ecrase jamais un autre.

    « .bak » quand il est libre et qu'on l'a promis (BACKUP) : c'est le nom que
    tout le monde reconnait. Sinon un horodatage, puis un numero d'ordre si
    deux sauvegardes tombent dans la meme seconde.
    """
    if not horodate:
        simple = path.with_name(f"{path.name}.bak")
        if not simple.exists():
            return simple
    instant = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    candidat = path.with_name(f"{path.name}.{instant}.bak")
    rang = 1
    while candidat.exists():
        rang += 1
        candidat = path.with_name(f"{path.name}.{instant}-{rang}.bak")
    return candidat


def _meme_contenu(path: Path, payload: bytes) -> bool:
    try:
        return path.stat().st_size == len(payload) and path.read_bytes() == payload
    except OSError:
        return False


def _remettre(ecarte: Path, path: Path) -> None:
    """Rend sa place a un fichier mis de cote quand la publication a echoue."""
    try:
        try:
            os.rename(ecarte, path)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(str(ecarte), str(path))
    except OSError:
        logger.exception("ancien fichier non remis en place, il reste dans %s", ecarte)


def place_file(
    path: Path,
    payload: bytes,
    *,
    on_existing: OnExisting = OnExisting.SKIP,
    trash_root: Path | None = None,
    force: bool = False,
) -> Placement:
    """Pose un fichier sans jamais detruire ce qui etait la.

    La seule primitive d'ecriture des metadonnees locales : fiches, affiches,
    manifestes et couvertures de livres passent tous par ici, pour que la
    regle ne puisse pas diverger d'un format a l'autre.

    - SKIP laisse l'existant en place, sauf ``force``.
    - Un contenu IDENTIQUE n'est pas reecrit. Sans ce test, chaque episode
      d'une serie remettrait « tvshow.nfo » en corbeille pour le reposer a
      l'identique, et la corbeille se remplirait de copies.
    - Sinon l'existant est mis de cote juste avant la publication : en
      corbeille quand ``trash_root`` est connu, a cote sous un nom qui
      n'ecrase rien sinon (voir ``OnExisting``).
    - Si la publication echoue apres cela, l'ancien est remis en place.

    ``force`` remplace une fiche qui decrit une AUTRE oeuvre, quelle que soit
    la conduite choisie. Leve ``OSError`` en cas d'echec : l'appelant sait,
    lui, si un fichier manquant merite d'etre signale ou simplement note.
    """
    existe = path.exists()
    if existe:
        if on_existing is OnExisting.SKIP and not force:
            logger.debug("%s existe deja, laisse en place", path.name)
            return Placement(Outcome.SKIPPED)
        if _meme_contenu(path, payload):
            return Placement(Outcome.SKIPPED)

    mis_de_cote: list[Path] = []

    def mettre_de_cote() -> None:
        if not existe:
            return
        if trash_root is not None:
            mis_de_cote.append(send_to_trash(path, trash_root))
            return
        cible = _nom_de_sauvegarde(path, horodate=on_existing is not OnExisting.BACKUP)
        os.rename(path, cible)
        mis_de_cote.append(cible)

    try:
        _write_atomic(path, payload, avant_publication=mettre_de_cote)
    except BaseException:
        if mis_de_cote and not path.exists():
            _remettre(mis_de_cote[0], path)
        raise

    if not existe:
        return Placement(Outcome.WRITTEN)
    sauvegarde = on_existing is OnExisting.BACKUP and not force
    return Placement(Outcome.BACKED_UP if sauvegarde else Outcome.REPLACED, mis_de_cote[0])


def _a_des_identifiants(sonde: FileProbe) -> bool:
    return bool(sonde.tmdb_id or sonde.imdb_id or sonde.tvdb_id)


def _write_nfo(
    path: Path,
    xml: str,
    *,
    on_existing: OnExisting,
    trash_root: Path | None,
    force: bool = False,
) -> Placement:
    """``write_nfo``, en rendant aussi ou est parti l'ancien fichier.

    Regle dure, plus forte que la conduite choisie : une fiche qui porte un
    identifiant n'est JAMAIS remplacee par une fiche qui n'en porte pas. La
    premiere epingle l'oeuvre ; la seconde laisserait le serveur multimedia la
    reidentifier par son nom, c'est-a-dire refaire l'erreur qu'un arbitrage
    avait corrigee — avec l'autorite d'une fiche locale. Le cas est compte
    comme ignore.
    """
    if (
        (force or on_existing is not OnExisting.SKIP)
        and path.is_file()
        and _a_des_identifiants(read_nfo_file(path))
        and not _a_des_identifiants(parse_nfo(xml))
    ):
        logger.info("%s porte un identifiant que la nouvelle fiche n'a pas, conserve", path.name)
        return Placement(Outcome.SKIPPED)
    return place_file(
        path, xml.encode("utf-8"), on_existing=on_existing, trash_root=trash_root, force=force
    )


def write_nfo(
    path: Path,
    xml: str,
    *,
    on_existing: OnExisting = OnExisting.SKIP,
    trash_root: Path | None = None,
) -> Outcome:
    """Depose une fiche. Ne detruit jamais rien, meme quand on le lui demande.

    ``trash_root`` : ou part la fiche remplacee. Sans lui, elle est mise de
    cote a cote, sous un nom qui n'ecrase aucune sauvegarde precedente.

    Leve ``OSError`` en cas d'echec d'ecriture : l'appelant sait, lui, si un
    ``.nfo`` manquant merite d'etre signale ou simplement note. Ici on ne
    decide pas a sa place.
    """
    return _write_nfo(path, xml, on_existing=on_existing, trash_root=trash_root).outcome


# --- Affiches ---------------------------------------------------------------

_TMDB_THUMBNAIL = re.compile(r"(https://image\.tmdb\.org/t/p/)w\d+(/)")


def display_size(url: str) -> str:
    """Reclame une AFFICHE en taille d'affichage plutot qu'en vignette.

    Les URL portees par les plans sont calibrees pour une grille de navigateur
    — « w185 », assez pour reconnaitre une jaquette dans une liste. Deposer
    cent quatre-vingt-cinq pixels de large dans une bibliotheque produirait une
    affiche floue sur un televiseur, et personne ne ferait le lien avec un
    reglage de vignette.

    Une URL qui ne vient pas de TMDB est laissee telle quelle : on ne devine
    pas la convention de tailles d'un service qu'on ne connait pas.
    """
    return _TMDB_THUMBNAIL.sub(r"\1w780\2", url)


def _telecharger_image(url: str, http: httpx.Client) -> bytes | None:
    """Les octets d'une image, lus EN FLUX et jamais au-dela du plafond.

    Lire la reponse entiere avant de la mesurer revenait a laisser le serveur
    decider de la memoire consommee : le plafond n'aurait protege que le
    disque. Une taille annoncee trop grande est refusee sans rien lire ; une
    taille non annoncee, ou mensongere, interrompt la lecture des que le
    plafond est franchi. Leve ``httpx.HTTPError``.
    """
    with http.stream("GET", url) as reponse:
        reponse.raise_for_status()
        if not reponse.headers.get("content-type", "").startswith("image/"):
            logger.debug("%s ne renvoie pas une image, ignore", url)
            return None
        annonce = reponse.headers.get("content-length", "").strip()
        if annonce.isdigit() and int(annonce) > MAX_IMAGE_BYTES:
            logger.debug("affiche annoncee a %s octets, refusee sans etre lue", annonce)
            return None
        donnees = bytearray()
        for bloc in reponse.iter_bytes():
            donnees += bloc
            if len(donnees) > MAX_IMAGE_BYTES:
                logger.debug("affiche au-dela de %d octets, lecture interrompue", MAX_IMAGE_BYTES)
                return None
        return bytes(donnees)


def _fetch_image(
    url: str,
    destination: Path,
    *,
    client: httpx.Client | None,
    on_existing: OnExisting,
    trash_root: Path | None,
    force: bool = False,
) -> Placement | None:
    """``fetch_image``, en rendant ce qui a ete fait. None : rien n'a ete pose."""
    if not url:
        return None
    if destination.exists() and on_existing is OnExisting.SKIP and not force:
        # Une affiche deja presente a ete posee par quelqu'un — la meme regle
        # que pour les .nfo : on ne remplace pas ce qu'on n'a pas ecrit, et on
        # ne le telecharge meme pas.
        return None

    ferme = client is None
    http = client or httpx.Client(timeout=IMAGE_TIMEOUT, follow_redirects=False)
    try:
        donnees = _telecharger_image(url, http)
        if donnees is None:
            return None
        return place_file(
            destination, donnees, on_existing=on_existing, trash_root=trash_root, force=force
        )
    except httpx.HTTPError as exc:
        # Un hote d'images bloque ne se resout pas tout seul non plus : en
        # debug, il resterait invisible aux niveaux de log habituels.
        logger.warning("affiche non telechargee (%s) : %s", destination.name, exc)
        return None
    except OSError as exc:
        # Le reseau a repondu et c'est le DISQUE qui refuse : droits, place,
        # volume en lecture seule. Cela ne se resoudra pas tout seul, et
        # merite d'etre vu.
        logger.warning("affiche non ecrite (%s) : %s", destination, exc)
        return None
    finally:
        if ferme:
            http.close()


def fetch_image(
    url: str,
    destination: Path,
    *,
    client: httpx.Client | None = None,
    on_existing: OnExisting = OnExisting.SKIP,
    trash_root: Path | None = None,
) -> bool:
    """Telecharge une image et la depose. Ne leve jamais, ne retente jamais.

    Une affiche manquante n'est pas un echec de rangement : le film est a sa
    place, et le serveur multimedia ira chercher l'image lui-meme. Insister —
    retenter, allonger le delai — ferait payer a un lot entier ce qui n'est
    qu'un agrement.

    Les redirections ne sont pas suivies : l'URL vient d'un fournisseur, et une
    redirection est le moyen le plus simple de faire emettre au serveur une
    requete vers autre chose que ce qu'on croyait demander.

    Une affiche deja la suit la meme conduite qu'une fiche : laissee en place
    par defaut, mise de cote — jamais detruite — sinon.
    """
    placement = _fetch_image(
        url, destination, client=client, on_existing=on_existing, trash_root=trash_root
    )
    return placement is not None and placement.outcome is not Outcome.SKIPPED


# --- Emplacements -----------------------------------------------------------


def _slug(value: str) -> str:
    """Reduit un nom a ses lettres et chiffres, accents aplatis.

    Sert a comparer un titre a un nom de dossier sans dependre de la
    ponctuation que le gabarit a pu ecrire : « Amélie » et « Amelie (2001) »
    doivent se reconnaitre.
    """
    plat = unicodedata.normalize("NFKD", value)
    plat = "".join(c for c in plat if not unicodedata.combining(c))
    return _NON_ALNUM.sub("", plat.lower())


def work_folder(media: Path, title: str, *, series: bool) -> Path | None:
    """Le dossier qui appartient a CETTE oeuvre, ou None si aucun ne lui appartient.

    Question moins evidente qu'elle n'en a l'air, et sur laquelle repose tout
    le depot d'affiches : « poster.jpg » decrit son dossier entier. Le poser
    dans un dossier qui contient trois cents films ferait de l'affiche de Dune
    la vignette de toute la bibliotheque.

    On remonte donc un eventuel dossier de saison, puis on VERIFIE que le
    dossier COMMENCE par le titre de l'oeuvre — « Dune (2021) » pour « Dune ».
    La comparaison ne va que dans ce sens : accepter l'inverse ferait passer un
    rangement alphabetique, « Films/D/Dune.mkv », pour le dossier de Dune.

    Un gabarit qui ne cree pas de dossier par oeuvre — c'est un choix
    legitime — renvoie donc None, et rien n'est depose. Ne rien ecrire vaut
    mieux qu'etiqueter le dossier du voisin.
    """
    dossier = media.parent
    if series and _SEASON_FOLDER.fullmatch(dossier.name):
        dossier = dossier.parent

    reference = _slug(title)
    if not reference:
        return None
    return dossier if _slug(dossier.name).startswith(reference) else None


# --- Depot ------------------------------------------------------------------


@dataclass(slots=True)
class Deposit:
    """Ce qui a ete depose, pour le dire a l'utilisateur sans le deviner."""

    written: list[Path] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    images: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unnumbered: list[Path] = field(default_factory=list)
    """Medias restes sans la fiche qui les identifie : numerotation inconnue,
    ou aucun dossier propre a l'oeuvre ou poser « tvshow.nfo ». Ce n'est pas
    une erreur, mais le taire laisserait croire que tout a ete epingle."""

    corrected: list[Path] = field(default_factory=list)
    """Fiches qui decrivaient une AUTRE oeuvre (identifiant TMDB different)
    et ont ete remplacees, quelle que soit la conduite choisie."""

    set_aside: list[tuple[Path, Path]] = field(default_factory=list)
    """(emplacement, ou l'ancien fichier est parti) pour chaque fichier
    remplace : de quoi le journaliser, donc le rendre a l'annulation."""

    def summary(self) -> str:
        """Fragment de phrase a coller au compte rendu d'un rangement."""
        morceaux = []
        if self.written:
            morceaux.append(f"{len(self.written)} fiche(s)")
        if self.images:
            morceaux.append(f"{len(self.images)} affiche(s)")
        if self.corrected:
            morceaux.append(
                f"{len(self.corrected)} fiche(s) d'une autre œuvre remplacée(s), "
                "l'ancienne mise de côté"
            )
        if self.skipped:
            morceaux.append(f"{len(self.skipped)} fiche(s) existante(s) conservée(s)")
        if self.unnumbered:
            morceaux.append(
                f"{len(self.unnumbered)} fichier(s) sans fiche d'identité "
                "(numérotation ou dossier d'œuvre inconnus)"
            )
        return ", ".join(morceaux)


SERIES_KINDS = frozenset({"episode", "anime"})


def deposit(
    media: Path,
    kind: str,
    info: MediaInfo,
    settings: LocalMetadataSettings,
    *,
    client: httpx.Client | None = None,
    trash_root: Path | None = None,
) -> Deposit:
    """Depose fiches et affiches a cote d'un media DEJA range.

    ``media`` designe le fichier a son emplacement definitif : ce module
    n'intervient qu'apres coup, jamais sur une source qu'on s'apprete a
    deplacer.

    ``trash_root`` : ou partent les fichiers remplaces. Sans lui, ils sont mis
    de cote a cote du media, sous un nom qui n'ecrase rien.

    Les livres sont ecartes : Jellyfin ne lit pas de ``.nfo`` pour eux, il
    attend un dossier par livre avec un ``metadata.opf``. Ecrire du XML Kodi a
    cote d'un EPUB ne serait lu par personne.

    Chaque ecriture est isolee des autres : un dossier de saison en lecture
    seule ne doit pas priver l'episode de sa propre fiche.

    Deux regles l'emportent sur la conduite choisie :

    - une fiche qui porte un identifiant n'est jamais remplacee par une fiche
      qui n'en porte pas (voir ``_write_nfo``) ;
    - une fiche dont l'identifiant TMDB designe une AUTRE oeuvre que celle
      qu'on depose est remplacee, affiches comprises, meme en « skip ». Elle
      est le reste d'une identification corrigee depuis : la respecter
      rendrait la correction sans effet chez le serveur multimedia, qui
      continuerait d'afficher la mauvaise oeuvre avec l'autorite d'une fiche
      locale. Le compte rendu le dit.
    """
    depot = Deposit()
    if kind == "book" or (not settings.nfo and not settings.artwork):
        return depot

    try:
        politique = OnExisting(settings.on_existing)
    except ValueError:
        # Une valeur inconnue vient d'un fichier de preferences edite a la
        # main : on retombe sur la conduite la plus prudente plutot que de
        # faire echouer un rangement pour un mot mal orthographie.
        politique = OnExisting.SKIP

    serie = kind in SERIES_KINDS
    dossier = work_folder(media, info.title, series=serie)
    numerote = info.season is not None and info.episode is not None

    if serie:
        fiche_d_identite = dossier / "tvshow.nfo" if dossier is not None else None
    else:
        fiche_d_identite = media.with_suffix(".nfo")
    force = _decrit_une_autre_oeuvre(fiche_d_identite, info.tmdb_id)

    if settings.nfo:
        if serie:
            if dossier is not None:
                _tenter(
                    depot, dossier / "tvshow.nfo", tvshow_xml(info), politique, trash_root, force
                )
            if numerote:
                _tenter(
                    depot,
                    media.with_suffix(".nfo"),
                    episode_xml(info),
                    politique,
                    trash_root,
                    force,
                )
            if not numerote or dossier is None:
                # Sans numerotation resolue, une fiche d'episode ne dirait que
                # ce que le serveur sait deja lire dans le nom du fichier — et
                # prendrait autorite pour le repeter. Sans dossier propre a
                # l'oeuvre, l'identite n'a nulle part ou se poser. Dans les
                # deux cas le fichier est signale, pas tu.
                depot.unnumbered.append(media)
        else:
            _tenter(depot, media.with_suffix(".nfo"), movie_xml(info), politique, trash_root, force)

    if settings.artwork and dossier is not None:
        # Seule l'affiche est reclamee dans une autre taille : l'URL de fond
        # n'a jamais servi de vignette, le fournisseur la produit deja dans la
        # taille utile.
        images = ((display_size(info.poster_url), POSTER_NAME), (info.backdrop_url, FANART_NAME))
        for url, nom in images:
            _deposer_image(depot, url, dossier / nom, politique, trash_root, force, client)

    return depot


def _decrit_une_autre_oeuvre(fiche: Path | None, tmdb_id: str) -> bool:
    """La fiche en place porte-t-elle l'identifiant TMDB d'une AUTRE oeuvre ?

    Faux des qu'on ne peut pas comparer — pas de fiche, pas d'identifiant d'un
    cote ou de l'autre : sans deux identifiants, « different » serait un pari.
    """
    if fiche is None or not tmdb_id or not fiche.is_file():
        return False
    en_place = read_nfo_file(fiche).tmdb_id
    return bool(en_place) and en_place != tmdb_id


def _deposer_image(
    depot: Deposit,
    url: str,
    destination: Path,
    politique: OnExisting,
    trash_root: Path | None,
    force: bool,
    client: httpx.Client | None,
) -> None:
    """Une affiche, et le compte rendu de ce qu'elle a remplace."""
    if not url:
        if force and destination.is_file():
            # L'oeuvre corrigee n'a pas d'image de ce type : celle de l'AUTRE
            # oeuvre ne doit pas rester pour autant a la representer.
            try:
                ecarte = _ecarter(destination, trash_root)
            except OSError as exc:
                depot.errors.append(f"{destination.name} : {exc}")
                logger.warning("affiche d'une autre oeuvre non ecartee (%s) : %s", destination, exc)
                return
            depot.set_aside.append((destination, ecarte))
        return
    if not force and (politique is OnExisting.SKIP or not destination.exists()):
        # Cas ordinaire : rien ne sera mis de cote, un booleen suffit. Il passe
        # par ``fetch_image`` et sa signature d'origine, parce que c'est le
        # point d'entree que les autres modules — et leurs tests — observent
        # pour savoir si un telechargement a ete tente.
        if fetch_image(url, destination, client=client):
            depot.images.append(destination)
        return
    # Remplacement d'une affiche en place : il faut savoir ou l'ancienne est
    # partie, pour la journaliser et la rendre a l'annulation.
    placement = _fetch_image(
        url,
        destination,
        client=client,
        on_existing=politique,
        trash_root=trash_root,
        force=force,
    )
    if placement is None or placement.outcome is Outcome.SKIPPED:
        return
    depot.images.append(destination)
    if placement.set_aside is not None:
        depot.set_aside.append((destination, placement.set_aside))


def _ecarter(path: Path, trash_root: Path | None) -> Path:
    """Met un fichier de cote sans le remplacer : corbeille, ou nom horodate a cote."""
    if trash_root is not None:
        return send_to_trash(path, trash_root)
    cible = _nom_de_sauvegarde(path, horodate=True)
    os.rename(path, cible)
    return cible


def _tenter(
    depot: Deposit,
    path: Path,
    xml: str,
    politique: OnExisting,
    trash_root: Path | None,
    force: bool,
) -> None:
    """Ecrit une fiche en rangeant l'echec dans le compte rendu.

    Rien ne remonte a l'appelant : le media est deja a sa place, et c'est ce
    qui compte. Une fiche non ecrite se signale, elle n'annule pas un
    rangement reussi.
    """
    try:
        placement = _write_nfo(path, xml, on_existing=politique, trash_root=trash_root, force=force)
    except OSError as exc:
        depot.errors.append(f"{path.name} : {exc}")
        logger.warning("fiche non ecrite (%s) : %s", path, exc)
        return
    if placement.outcome is Outcome.SKIPPED:
        depot.skipped.append(path)
        return
    depot.written.append(path)
    if placement.set_aside is not None:
        depot.set_aside.append((path, placement.set_aside))
        if force:
            depot.corrected.append(path)


# --- Sources d'information --------------------------------------------------


def info_from_plan(plan: Plan) -> MediaInfo:
    """Fiche construite depuis un plan qu'on vient d'appliquer.

    C'est le cas qui justifie tout le module : ce plan porte l'identification
    validee — parfois par un humain qui a corrige la machine — et sans ce
    passage elle ne quitterait jamais Sortilege.

    Ni synopsis, ni genre, ni studio : regle 4 du module. Ce que le plan
    transporte de ces champs est un extrait calibre pour une grille de
    navigateur, pas une synopsis.
    """
    valeurs = plan.values or {}
    tmdb = plan.external_id if plan.provider == "tmdb" else ""
    return MediaInfo(
        title=plan.title or str(valeurs.get("title") or ""),
        original_title=str(valeurs.get("original_title") or ""),
        year=plan.year or valeurs.get("year"),
        tmdb_id=tmdb or str(valeurs.get("tmdb_id") or ""),
        imdb_id=str(valeurs.get("imdb_id") or ""),
        season=valeurs.get("season"),
        episode=valeurs.get("episode"),
        episode_title=str(valeurs.get("episode_title") or ""),
        poster_url=plan.poster_url,
        backdrop_url=plan.backdrop_url,
    )


def info_from_scan(scanned: ScannedFile) -> MediaInfo:
    """Fiche construite depuis un fichier DEJA range, sans rien redemander.

    Meme precaution que pour la remise en conformite des noms : on repart de ce
    que le fichier dit de lui-meme. Reinterroger un fournisseur ferait courir
    le risque qu'une mauvaise reponse ecrase, avec autorite, une bibliotheque
    correcte.

    Consequence a connaitre : la fiche produite est PAUVRE — un titre, une
    annee, une numerotation, et les identifiants si un ``.nfo`` en portait
    deja. C'est exactement ce qu'il faut : elle epingle l'identite, et laisse
    le serveur completer le reste.

    Les identifiants deja poses sont relus ICI, a cote du fichier, quand la
    sonde n'en a pas. Un scan rapide ne lit pas les fichiers et rend une sonde
    vide : la fiche regeneree perdait alors son ``uniqueid`` et, en
    « overwrite », effacait avec autorite l'arbitrage que portait l'ancienne.
    """
    parsed = scanned.parsed
    probe = scanned.probe
    posee = _identite_deja_posee(scanned.path, serie=str(parsed.kind) in SERIES_KINDS)
    return MediaInfo(
        title=probe.nfo_title or posee.nfo_title or parsed.title,
        year=parsed.year or probe.nfo_year or posee.nfo_year,
        tmdb_id=probe.tmdb_id or posee.tmdb_id or "",
        imdb_id=probe.imdb_id or posee.imdb_id or "",
        tvdb_id=probe.tvdb_id or posee.tvdb_id or "",
        season=parsed.season if parsed.season is not None else probe.container_season,
        episode=parsed.episode if parsed.episode is not None else probe.container_episode,
    )


def _identite_deja_posee(media: Path, *, serie: bool) -> FileProbe:
    """La fiche qui porte deja l'identite de ce media, ou une sonde vide.

    Pour un film : sa fiche, puis « movie.nfo ». Pour un episode : UNIQUEMENT
    la fiche de serie, a la racine de la serie — au-dessus d'un eventuel
    dossier de saison, la ou ``deposit`` la pose. La fiche d'un episode peut
    porter l'identifiant de l'EPISODE (tinyMediaManager l'y ecrit) : le
    reprendre pour la serie inscrirait un numero d'episode comme identite de
    toute la serie.
    """
    if serie:
        candidates = [media.parent / "tvshow.nfo"]
        if _SEASON_FOLDER.fullmatch(media.parent.name):
            candidates.append(media.parent.parent / "tvshow.nfo")
    else:
        candidates = [media.with_suffix(".nfo"), media.parent / "movie.nfo"]
    for fiche in candidates:
        if fiche.is_file() and _a_des_identifiants(sonde := read_nfo_file(fiche)):
            return sonde
    return FileProbe()
