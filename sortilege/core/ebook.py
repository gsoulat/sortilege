"""Lecture des metadonnees d'un livre numerique.

Renversement complet par rapport a la video, et c'est ce qui rend les livres
plus faciles a ranger qu'un film.

Un fichier video ne dit presque rien de lui-meme : le nom de release ment, les
tags de conteneur sont rarement remplis, et il faut interroger un fournisseur
pour savoir ce qu'on tient. Un EPUB, lui, **porte ses metadonnees**. Le format
impose un manifeste XML — titre, auteur, editeur, langue, ISBN, parfois la
serie et le numero de tome — renseigne par celui qui a fabrique le fichier.

Conclusion pratique : ici, le fichier fait autorite et le fournisseur ne sert
qu'a completer. C'est l'inverse exact de la video, et le pipeline doit en tenir
compte, sans quoi on irait chercher au loin ce qu'on a deja sous la main.

Le PDF est le parent pauvre. Ses metadonnees sont facultatives et souvent
remplies par l'outil de generation plutot que par un editeur — « Microsoft Word
- document1 » comme titre est courant. On les lit quand meme, en les traitant
comme un indice faible.

Aucune dependance ajoutee : un EPUB est un ZIP et son manifeste du XML, tous
deux dans la bibliotheque standard. Ajouter une dependance pour lire un ZIP
serait payer cher une commodite.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

BOOK_EXTENSIONS = {".epub", ".pdf", ".mobi", ".azw3", ".cbz", ".cbr", ".djvu", ".fb2"}

# Formats dont on sait lire les metadonnees. Les autres sont ranges d'apres
# leur nom seul — c'est moins bon, mais les ecarter serait pire.
READABLE = {".epub", ".pdf"}

# Espaces de noms du manifeste EPUB. Ils sont fixes par le format ; les coder
# en dur est plus sur que de les deviner, car certains fabricants omettent les
# declarations.
_DC = "{http://purl.org/dc/elements/1.1/}"
_OPF = "{http://www.idpf.org/2007/opf}"

_CONTAINER = "META-INF/container.xml"

# « Tome 3 », « T3 », « Volume 2 », « #4 » : le numero de tome se trouve plus
# souvent dans le titre que dans un champ dedie.
_TOME = re.compile(
    r"(?:\b(?:tome|volume|vol|livre|book|t)\s*\.?\s*|#)(?P<numero>\d{1,3})\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class BookMeta:
    """Ce que le fichier dit de lui-meme. Vide si illisible."""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    series: str = ""
    volume: int | None = None
    year: int | None = None
    publisher: str = ""
    language: str = ""
    isbn: str = ""
    read: bool = False
    """Le manifeste a bien ete lu. Distinct d'un manifeste lu mais vide : dans
    le premier cas on ne sait rien, dans le second on sait qu'il n'y a rien."""

    @property
    def author(self) -> str:
        return ", ".join(self.authors)

    @property
    def trustworthy(self) -> bool:
        """Assez renseigne pour se passer d'un fournisseur ?

        Un titre ET un auteur suffisent a ranger un livre correctement. Aller
        interroger une base pour confirmer ce que l'editeur a lui-meme inscrit
        serait depenser un appel pour rien.
        """
        return bool(self.title and self.authors)


def read(path: Path) -> BookMeta:
    """Metadonnees du livre. Ne leve jamais : un fichier illisible est un
    fichier sans metadonnees, pas une erreur."""
    extension = path.suffix.lower()
    try:
        if extension == ".epub":
            return _read_epub(path)
        if extension == ".pdf":
            return _read_pdf(path)
    except Exception as exc:
        logger.debug("metadonnees illisibles pour %s : %s", path.name, exc)
    return BookMeta()


# --- EPUB -------------------------------------------------------------------


def _read_epub(path: Path) -> BookMeta:
    with zipfile.ZipFile(path) as archive:
        chemin_opf = _locate_opf(archive)
        if chemin_opf is None:
            return BookMeta()
        racine = ElementTree.fromstring(archive.read(chemin_opf))  # noqa: S314

    meta = BookMeta(read=True)
    meta.title = _first_text(racine, f".//{_DC}title")
    meta.authors = [
        texte for element in racine.iter(f"{_DC}creator") if (texte := (element.text or "").strip())
    ]
    meta.publisher = _first_text(racine, f".//{_DC}publisher")
    meta.language = _first_text(racine, f".//{_DC}language")
    meta.year = _year(_first_text(racine, f".//{_DC}date"))
    meta.isbn = _isbn(racine)

    # La serie n'a pas de champ standard avant EPUB 3 : Calibre a impose une
    # convention par balises <meta name="calibre:series">, que tout le monde
    # suit desormais. On la lit, faute de mieux normalise.
    for element in racine.iter(f"{_OPF}meta"):
        nom = element.get("name", "")
        contenu = element.get("content", "")
        if nom == "calibre:series" and contenu:
            meta.series = contenu.strip()
        elif nom == "calibre:series_index" and contenu:
            meta.volume = _int(contenu)

    if meta.volume is None and meta.title:
        if trouve := _TOME.search(meta.title):
            meta.volume = _int(trouve.group("numero"))

    return meta


def _locate_opf(archive: zipfile.ZipFile) -> str | None:
    """Chemin du manifeste, declare dans META-INF/container.xml.

    On le SUIT au lieu de chercher un « *.opf » au hasard : un EPUB peut en
    contenir plusieurs, et seul celui-ci fait foi.
    """
    try:
        container = ElementTree.fromstring(archive.read(_CONTAINER))  # noqa: S314
    except (KeyError, ElementTree.ParseError):
        return None
    for element in container.iter():
        if element.tag.endswith("rootfile") and (chemin := element.get("full-path")):
            return chemin
    return None


def _isbn(racine: ElementTree.Element) -> str:
    """ISBN parmi les identifiants. Les autres — UUID, URL — n'identifient que
    le FICHIER, pas l'oeuvre, et ne servent a rien pour la retrouver."""
    for element in racine.iter(f"{_DC}identifier"):
        texte = (element.text or "").strip()
        nettoye = re.sub(r"[^0-9Xx]", "", texte)
        if len(nettoye) in (10, 13) and "isbn" in (texte + element.get("scheme", "")).lower():
            return nettoye.upper()
        if len(nettoye) in (10, 13) and texte.lower().startswith(("978", "979", "isbn")):
            return nettoye.upper()
    return ""


# --- PDF --------------------------------------------------------------------

_PDF_TITLE = re.compile(rb"/Title\s*\((?P<valeur>(?:[^()\\]|\\.)*)\)")
_PDF_AUTHOR = re.compile(rb"/Author\s*\((?P<valeur>(?:[^()\\]|\\.)*)\)")

# Titres poses par un outil de generation, qui ne designent aucune oeuvre. Les
# retenir ferait ranger des livres sous « Document1 ».
_PDF_BIDON = re.compile(
    r"^(?:document\d*|untitled|microsoft word.*|sans titre|print|pdfsam.*|scan.*)$",
    re.IGNORECASE,
)


def _read_pdf(path: Path) -> BookMeta:
    """Metadonnees d'un PDF, sans dependance.

    On lit la fin du fichier, ou le dictionnaire d'informations se trouve dans
    l'immense majorite des cas. C'est approximatif — un PDF compresse ou chiffre
    ne livrera rien — mais c'est gratuit, et ces metadonnees ne valent de toute
    facon qu'un indice faible : bien souvent l'outil de generation y a inscrit
    son propre nom.
    """
    with path.open("rb") as handle:
        handle.seek(0, 2)
        taille = handle.tell()
        handle.seek(max(0, taille - 32 * 1024))
        queue = handle.read()

    meta = BookMeta(read=True)
    if trouve := _PDF_TITLE.search(queue):
        titre = _pdf_text(trouve.group("valeur"))
        if titre and not _PDF_BIDON.match(titre):
            meta.title = titre
    if trouve := _PDF_AUTHOR.search(queue):
        if auteur := _pdf_text(trouve.group("valeur")):
            meta.authors = [auteur]
    return meta


def _pdf_text(brut: bytes) -> str:
    texte = brut.replace(b"\\(", b"(").replace(b"\\)", b")").replace(b"\\\\", b"\\")
    return texte.decode("utf-8", errors="ignore").strip()


# --- Communs ----------------------------------------------------------------


def _first_text(racine: ElementTree.Element, chemin: str) -> str:
    element = racine.find(chemin)
    return (element.text or "").strip() if element is not None else ""


def _year(texte: str) -> int | None:
    if trouve := re.search(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)", texte):
        return int(trouve.group(1))
    return None


def _int(texte: str) -> int | None:
    try:
        return int(float(texte))
    except (TypeError, ValueError):
        return None
