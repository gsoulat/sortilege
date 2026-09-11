"""Rendre un livre lisible par un serveur multimedia : un dossier, un manifeste, une couverture.

``core/nfo`` a resolu la question pour la video. Elle se repose entierement
pour le livre, parce que Jellyfin n'y lit PAS de ``.nfo`` : il cherche, a cote
du fichier, un ``metadata.opf``, un ``content.opf`` ou un ``ComicInfo.xml``.
Deposer une fiche Kodi dans un dossier de livres revient donc a n'en deposer
aucune — le travail d'identification reste dans Sortilege et le serveur refait
le sien, avec les erreurs que ce module existe pour eviter.

**Pourquoi l'OPF plutot qu'un format a nous.** ``metadata.opf`` est ce que
Calibre ecrit pour chaque livre de sa bibliotheque, accompagne d'une
``cover.jpg``. C'est le format d'echange deja etabli du livre numerique : le
choisir fait qu'une bibliotheque rangee par Sortilege s'importe dans Calibre
sans conversion, et l'inverse. Il est de surcroit a portee de main, puisque
c'est le meme vocabulaire Dublin Core que les manifestes EPUB que
``core/ebook`` sait deja lire — on ECRIT ici ce qu'on LIT la-bas.

**Un dossier par livre.** La documentation de Jellyfin est explicite sur ce
point, et la raison est mecanique : ``metadata.opf`` et ``cover.jpg`` portent
des noms fixes. Deux livres dans le meme dossier ne peuvent pas avoir chacun
leur manifeste — le second ecraserait le premier, ou pire, le premier
decrirait les deux. Le prereglage de gabarit « book » de ``core/template`` en
decoule ; ce n'est qu'une SUGGESTION, car le chemin des livres reste un
reglage de l'utilisateur.

Les regles d'ecriture sont celles de ``core/nfo``, et passent par sa
primitive ``place_file`` pour ne pas diverger : un champ vide n'est jamais
ecrit (il effacerait ce que le serveur aurait trouve), l'ecriture est
atomique (un manifeste tronque serait relu comme faisant autorite, donc pire
qu'absent), rien n'ecrase un fichier existant sans qu'on l'ait demande —
celui de Calibre est plus riche que le notre — et rien ne le detruit quand on
l'a demande : il part en corbeille, ou a cote sous un nom qui n'ecrase rien.

Aucune dependance nouvelle : ``xml.etree`` produit ce XML et ``zipfile`` ouvre
l'EPUB, comme deja dans ``core/ebook`` et ``core/epubreader``.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree

from .ebook import BookMeta
from .nfo import OnExisting, Outcome, Placement, place_file

logger = logging.getLogger(__name__)

XML_HEADER = '<?xml version="1.0" encoding="UTF-8"?>'

OPF_NAME = "metadata.opf"
"""Le nom que Calibre ecrit. Jellyfin lit aussi « content.opf », mais celui-la
est le nom du manifeste INTERNE d'un EPUB : le reutiliser a cote du fichier
melangerait deux roles qui n'ont pas la meme duree de vie."""

COVER_STEM = "cover"
"""Sans extension : elle depend du format reel de l'image trouvee (voir
``_extension``)."""

_DC_NS = "http://purl.org/dc/elements/1.1/"
_OPF_NS = "http://www.idpf.org/2007/opf"
_OPF = f"{{{_OPF_NS}}}"

_CONTAINER = "META-INF/container.xml"

_ISBN_ID = "isbn"
"""Identifiant XML de l'element ISBN, reference par ``unique-identifier``. Une
constante parce que les deux endroits DOIVENT dire la meme chose : un
``unique-identifier`` qui ne designe aucun element rend le manifeste invalide."""

MAX_COVER_BYTES = 12 * 1024 * 1024
"""Plafond de ce qu'on accepte d'extraire, verifie sur la taille DECLAREE de
l'entree avant toute lecture. Un EPUB vient d'internet et rien n'empeche d'y
declarer une image de plusieurs gigaoctets ; la decompresser en memoire pour
s'en apercevoir ensuite serait s'en remettre a la bonne volonte du fichier."""

# Extensions par type declare. Renommer un PNG en « .jpg » passe chez la
# plupart des lecteurs et casse chez les stricts ; Jellyfin accepte de toute
# facon « cover.png » aussi bien que « cover.jpg », donc autant ne pas mentir.
_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


# --- Production du manifeste -------------------------------------------------


def _text(parent: ElementTree.Element, tag: str, value: object, **attrs: str) -> None:
    """Ajoute un element, SAUF si sa valeur est vide.

    Meme regle que dans ``core/nfo``, appliquee en un seul endroit pour
    qu'aucun appelant ne puisse l'oublier : un ``<dc:publisher/>`` vide
    n'informe pas le serveur, il lui interdit d'aller chercher l'editeur
    ailleurs.
    """
    if value is None or value == "":
        return
    element = ElementTree.SubElement(parent, tag, attrs)
    element.text = str(value)


def metadata_xml(meta: BookMeta) -> str:
    """Le manifeste OPF 2.0 decrivant ce livre.

    Les prefixes (``dc:``, ``opf:``) sont ecrits LITTERALEMENT plutot que par
    ``ElementTree.register_namespace`` : cette derniere modifie une table
    globale au processus, et ce module changerait alors la facon dont tout le
    reste du programme serialise son XML — ``core/nfo`` compris. Le prix a
    payer est de declarer les espaces de noms a la main sur ``<metadata>`` ;
    le resultat se relit correctement avec un analyseur qui les respecte,
    ``core/ebook`` en premier.
    """
    package = ElementTree.Element("package", {"xmlns": _OPF_NS, "version": "2.0"})
    # OPF 2.0 veut un « unique-identifier », mais nous n'avons d'identifiant
    # que si le livre porte un ISBN. En fabriquer un UUID donnerait un fichier
    # different a chaque passage — donc une modification permanente de la
    # bibliotheque a chaque rangement — pour un identifiant que personne ne
    # peut resoudre. Ni Calibre ni Jellyfin ne l'exigent : on s'en passe.
    if meta.isbn:
        package.set("unique-identifier", _ISBN_ID)

    metadata = ElementTree.SubElement(
        package, "metadata", {"xmlns:dc": _DC_NS, "xmlns:opf": _OPF_NS}
    )
    _text(metadata, "dc:title", meta.title)
    for auteur in meta.authors:
        # « aut » distingue l'auteur du traducteur ou de l'illustrateur, qui
        # partagent le meme element dc:creator. Sans ce role, un livre range
        # par Sortilege puis relu par Calibre pourrait s'attribuer a son
        # traducteur.
        _text(metadata, "dc:creator", auteur, **{"opf:role": "aut"})
    _text(metadata, "dc:language", meta.language)
    _text(metadata, "dc:publisher", meta.publisher)
    # L'annee seule, jamais « 2010-01-01 » : nous ne connaissons pas le jour,
    # et l'inventer inscrirait une precision fausse dans un fichier qui fait
    # autorite. W3CDTF, que le format impose, autorise l'annee nue.
    _text(metadata, "dc:date", meta.year)
    if meta.isbn:
        _text(metadata, "dc:identifier", meta.isbn, id=_ISBN_ID, **{"opf:scheme": "ISBN"})

    # La serie n'a pas de champ standard avant EPUB 3 : Calibre a impose une
    # convention par balises <meta>, que tout le monde suit. On l'ecrit sous la
    # forme exacte que core/ebook sait relire.
    if meta.series:
        _meta(metadata, "calibre:series", meta.series)
        if meta.volume is not None:
            # Un numero de tome sans serie ne designe rien : il ne serait pas
            # faux, il serait orphelin. D'ou l'imbrication.
            _meta(metadata, "calibre:series_index", str(meta.volume))

    ElementTree.indent(package, space="  ")
    return f"{XML_HEADER}\n{ElementTree.tostring(package, encoding='unicode')}\n"


def _meta(parent: ElementTree.Element, nom: str, contenu: str) -> None:
    ElementTree.SubElement(parent, "meta", {"name": nom, "content": contenu})


# --- Ecriture ----------------------------------------------------------------


def manifest_path(book: Path) -> Path:
    """Ou se pose le manifeste de ce livre : a cote de lui, jamais ailleurs."""
    return book.parent / OPF_NAME


def _write_metadata(
    book: Path, meta: BookMeta, *, on_existing: OnExisting, trash_root: Path | None
) -> Placement:
    """``write_metadata``, en rendant aussi ou est parti l'ancien manifeste."""
    if on_existing is OnExisting.SKIP and (book.parent / "content.opf").exists():
        logger.debug("un content.opf decrit deja %s, rien n'est ecrit", book.name)
        return Placement(Outcome.SKIPPED)
    return place_file(
        manifest_path(book),
        metadata_xml(meta).encode("utf-8"),
        on_existing=on_existing,
        trash_root=trash_root,
    )


def write_metadata(
    book: Path,
    meta: BookMeta,
    *,
    on_existing: OnExisting = OnExisting.SKIP,
    trash_root: Path | None = None,
) -> Outcome:
    """Depose le manifeste du livre. Leve ``OSError`` si l'ecriture echoue.

    Un ``content.opf`` deja present arrete le depot par defaut, et pas
    seulement un ``metadata.opf`` : Jellyfin lit les deux noms sans ordre de
    preference documente. En ecrire un second reviendrait a laisser le hasard
    decider lequel des deux decrit le livre — une ambiguite plus couteuse que
    l'absence de notre fiche.

    ``trash_root`` : ou part le manifeste remplace (voir ``nfo.place_file``).
    """
    return _write_metadata(book, meta, on_existing=on_existing, trash_root=trash_root).outcome


# --- Couverture --------------------------------------------------------------


def _iter(racine: ElementTree.Element, nom: str) -> Iterator[ElementTree.Element]:
    """Elements d'un manifeste EPUB, avec ou sans espace de noms declare.

    Certains fabricants omettent la declaration ; exiger l'espace de noms
    ferait rendre « pas de couverture » pour des livres qui en ont une.
    """
    for element in racine.iter():
        if element.tag in (f"{_OPF}{nom}", nom):
            yield element


def _cover_item(racine: ElementTree.Element) -> ElementTree.Element | None:
    """L'entree de manifeste designant la couverture, dans les deux formes.

    EPUB 3 la marque par ``properties="cover-image"`` ; EPUB 2 n'ayant pas cet
    attribut, la pratique y est une balise ``<meta name="cover" content="id">``
    pointant vers l'item. Les deux se rencontrent, et beaucoup de livres
    recents portent les deux par compatibilite — d'ou l'ordre : la forme
    EPUB 3 est normative, la seconde n'est qu'une convention heritee.

    On ne devine JAMAIS au-dela : prendre la premiere image du livre faute de
    declaration deposerait une illustration de chapitre comme couverture, avec
    l'aplomb d'une donnee verifiee.
    """
    for item in _iter(racine, "item"):
        if "cover-image" in (item.get("properties") or "").split():
            return item

    identifiant = ""
    for element in _iter(racine, "meta"):
        if element.get("name") == "cover" and (contenu := element.get("content", "").strip()):
            identifiant = contenu
            break
    if identifiant:
        for item in _iter(racine, "item"):
            if item.get("id") == identifiant:
                return item
    return None


def _extension(item: ElementTree.Element, href: str) -> str:
    if extension := _EXTENSIONS.get((item.get("media-type") or "").strip().lower()):
        return extension
    # Type absent ou exotique : le suffixe du fichier reste un indice, et
    # « .jpg » est le defaut le moins surprenant pour une couverture.
    suffixe = posixpath.splitext(href)[1].lower()
    return suffixe if suffixe in {".jpg", ".jpeg", ".png", ".gif", ".webp"} else ".jpg"


def _entree(archive: zipfile.ZipFile, chemin: str) -> str | None:
    """Le nom REEL de l'entree dans le ZIP, ou None.

    Les href d'un manifeste sont des IRI : ils peuvent porter des sequences
    percent-encodees que le ZIP, lui, stocke en clair. Et un EPUB fabrique sur
    un systeme insensible a la casse declare parfois « Cover.jpg » pour une
    entree « cover.jpg ». Les deux cas se rattrapent ici plutot que de rendre
    une couverture absente qui, en realite, est bien la.
    """
    noms = set(archive.namelist())
    for candidat in (chemin, unquote(chemin)):
        if candidat in noms:
            return candidat
    return {nom.lower(): nom for nom in noms}.get(unquote(chemin).lower())


def _locate_opf(archive: zipfile.ZipFile) -> str | None:
    """Chemin du manifeste, declare dans META-INF/container.xml.

    On le SUIT plutot que de chercher un « *.opf » au hasard : un EPUB peut en
    contenir plusieurs et seul celui-ci fait foi.
    """
    try:
        container = ElementTree.fromstring(archive.read(_CONTAINER))  # noqa: S314
    except (KeyError, ElementTree.ParseError):
        return None
    for element in container.iter():
        if element.tag.endswith("rootfile") and (chemin := element.get("full-path")):
            return chemin
    return None


def cover_data(book: Path) -> tuple[bytes, str] | None:
    """Les octets de la couverture et l'extension qui lui convient, ou None.

    None n'est pas une erreur : c'est le cas courant d'un livre qui ne declare
    pas de couverture, et de tous les formats autres que l'EPUB. Un PDF en
    porte une — sa premiere page — mais l'en extraire demanderait un moteur de
    rendu, c'est-a-dire une dependance lourde pour une image d'agrement.
    """
    if book.suffix.lower() != ".epub":
        return None
    try:
        with zipfile.ZipFile(book) as archive:
            chemin_opf = _locate_opf(archive)
            if chemin_opf is None:
                return None
            racine = ElementTree.fromstring(archive.read(chemin_opf))  # noqa: S314
            item = _cover_item(racine)
            if item is None or not (href := (item.get("href") or "").strip()):
                return None

            # Les href sont relatifs au manifeste, pas a la racine du ZIP.
            base = posixpath.dirname(chemin_opf)
            chemin = posixpath.normpath(posixpath.join(base, href)) if base else href
            nom = _entree(archive, chemin)
            if nom is None:
                logger.debug("couverture declaree mais absente de %s : %s", book.name, chemin)
                return None
            if archive.getinfo(nom).file_size > MAX_COVER_BYTES:
                logger.debug("couverture de %s trop volumineuse, ignoree", book.name)
                return None
            return archive.read(nom), _extension(item, href)
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError, KeyError) as exc:
        logger.debug("couverture illisible pour %s : %s", book.name, exc)
        return None


def extract_cover(
    book: Path,
    destination: Path | None = None,
    *,
    on_existing: OnExisting = OnExisting.SKIP,
    trash_root: Path | None = None,
) -> Outcome | None:
    """Extrait la couverture a cote du livre. Rend None si le livre n'en a pas.

    La distinction compte pour l'appelant : ``Outcome.SKIPPED`` dit qu'une
    couverture existait deja et qu'on l'a respectee, ``None`` qu'il n'y avait
    rien a extraire. Confondre les deux ferait signaler comme « conserve » un
    fichier qui n'a jamais existe.
    """
    resultat = _extract_cover(book, destination, on_existing=on_existing, trash_root=trash_root)
    return None if resultat is None else resultat[1].outcome


def _extract_cover(
    book: Path,
    destination: Path | None,
    *,
    on_existing: OnExisting,
    trash_root: Path | None,
) -> tuple[Path, Placement] | None:
    """``extract_cover``, en rendant l'emplacement et ce qu'il a remplace."""
    donnees = cover_data(book)
    if donnees is None:
        return None
    payload, extension = donnees
    cible = destination or book.parent / f"{COVER_STEM}{extension}"
    return cible, place_file(cible, payload, on_existing=on_existing, trash_root=trash_root)


# --- Depot complet -----------------------------------------------------------


@dataclass(slots=True)
class BookDeposit:
    """Ce qui a reellement ete depose a cote d'un livre."""

    manifest: Outcome
    cover: Outcome | None = None
    """None quand le livre ne portait aucune couverture."""

    written: list[Path] = field(default_factory=list)
    """Fichiers reellement poses : ce qu'une annulation devra retirer."""

    set_aside: list[tuple[Path, Path]] = field(default_factory=list)
    """(emplacement, ou l'ancien fichier est parti) pour chaque fichier
    remplace : ce qu'une annulation devra rendre."""


def deposit(
    book: Path,
    meta: BookMeta,
    *,
    on_existing: OnExisting = OnExisting.SKIP,
    trash_root: Path | None = None,
) -> BookDeposit:
    """Manifeste et couverture, en un seul geste.

    L'absence de couverture n'empeche pas le manifeste : c'est lui qui porte
    l'identite du livre, l'image n'est qu'un agrement. Faire dependre le
    premier de la seconde priverait de metadonnees tous les livres sans
    illustration.

    ``trash_root`` : ou partent le manifeste et la couverture remplaces. Sans
    lui, ils sont mis de cote a cote du livre, sous un nom qui n'ecrase rien.
    """
    manifeste = _write_metadata(book, meta, on_existing=on_existing, trash_root=trash_root)
    depot = BookDeposit(manifest=manifeste.outcome)
    _noter(depot, manifest_path(book), manifeste)
    try:
        couverture = _extract_cover(book, None, on_existing=on_existing, trash_root=trash_root)
    except OSError as exc:
        # Le manifeste, lui, est deja ecrit : le signaler perdu parce que
        # l'image a echoue ferait recommencer un travail deja fait. Mais le
        # disque qui refuse ne se reparera pas seul : cela merite d'etre vu.
        logger.warning("couverture non deposee pour %s : %s", book.name, exc)
        couverture = None
    if couverture is not None:
        cible, placement = couverture
        depot.cover = placement.outcome
        _noter(depot, cible, placement)
    return depot


def _noter(depot: BookDeposit, chemin: Path, placement: Placement) -> None:
    if placement.outcome is Outcome.SKIPPED:
        return
    depot.written.append(chemin)
    if placement.set_aside is not None:
        depot.set_aside.append((chemin, placement.set_aside))
