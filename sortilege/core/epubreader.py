"""Lecture d'un EPUB chapitre par chapitre, pour un lecteur dans le navigateur.

Un EPUB est un ZIP contenant du XHTML. Aucun navigateur ne sait l'ouvrir tel
quel : il faut le desarchiver quelque part. Deux facons de faire, et le choix
n'est pas anodin.

**Cote client**, avec une bibliotheque JavaScript qui dezippe dans le
navigateur. C'est la solution courante — et elle imposerait de charger du code
depuis un CDN, alors que l'application doit tourner sur un NAS sans acces
internet. Elle obligerait aussi a transferer le livre entier avant la premiere
page.

**Cote serveur**, en servant un chapitre a la fois. C'est ce qui est fait ici :
zipfile est dans la bibliotheque standard, la premiere page arrive
immediatement, et surtout le contenu passe par un filtre avant d'atteindre le
navigateur.

Ce dernier point est le vrai sujet. **Le contenu d'un EPUB est du code
etranger** : n'importe qui peut fabriquer un livre contenant un script qui
lira la session de l'utilisateur. Le fichier vient d'internet, il n'est pas
plus digne de confiance qu'une page web quelconque. D'ou deux protections
independantes :

1. Ici, le HTML est NETTOYE — scripts, cadres, gestionnaires d'evenements et
   liens « javascript: » sont retires.
2. Dans l'interface, il est affiche dans un cadre bac-a-sable sans autorisation
   d'execution.

Une seule de ces deux protections suffirait probablement. Les deux tiennent
parce que la premiere est du filtrage — donc faillible par nature — et que la
seconde est structurelle.
"""

from __future__ import annotations

import logging
import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

_CONTAINER = "META-INF/container.xml"
_OPF = "{http://www.idpf.org/2007/opf}"

# Ce qu'on retire du XHTML d'un chapitre. Un EPUB peut contenir n'importe quoi :
# le format autorise le JavaScript, et rien n'empeche un livre telecharge de
# porter un script qui lirait la session de l'utilisateur.
_SCRIPT = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_CADRE = re.compile(r"</?(?:iframe|object|embed|form)\b[^>]*>", re.IGNORECASE)
_EVENEMENT = re.compile(r"\son[a-z]+\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"(href|src)\s*=\s*(?:\"|')\s*javascript:[^\"']*(?:\"|')", re.IGNORECASE)

# Attributs pointant vers une ressource interne au livre, a reecrire vers l'API.
_RESSOURCE = re.compile(r"(?P<attr>src|href|xlink:href)\s*=\s*\"(?P<url>[^\"]+)\"", re.IGNORECASE)

_CORPS = re.compile(r"<body\b[^>]*>(?P<corps>.*)</body\s*>", re.IGNORECASE | re.DOTALL)


@dataclass(slots=True)
class Chapter:
    """Une entree du fil de lecture."""

    index: int
    href: str
    title: str = ""


def spine(path: Path) -> list[Chapter]:
    """Le fil de lecture, dans l'ordre voulu par l'editeur.

    On suit le ``spine`` du manifeste plutot que l'ordre des fichiers dans le
    ZIP : ce dernier n'a aucun sens de lecture, et une table des matieres tiree
    de la est aussi utile qu'un livre dont on aurait melange les pages.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            chemin_opf = _locate_opf(archive)
            if chemin_opf is None:
                return []
            racine = ElementTree.fromstring(archive.read(chemin_opf))  # noqa: S314
            base = posixpath.dirname(chemin_opf)

            manifeste = {
                item.get("id"): item.get("href", "")
                for item in racine.iter(f"{_OPF}item")
                if item.get("id")
            }
            titres = _titles(archive, racine, base)

            chapitres: list[Chapter] = []
            for element in racine.iter(f"{_OPF}itemref"):
                href = manifeste.get(element.get("idref", ""), "")
                if not href:
                    continue
                complet = posixpath.normpath(posixpath.join(base, href)) if base else href
                chapitres.append(Chapter(len(chapitres), complet, titres.get(complet, "")))
            return chapitres
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        logger.debug("fil de lecture illisible pour %s : %s", path.name, exc)
        return []


def _titles(archive: zipfile.ZipFile, racine, base: str) -> dict[str, str]:
    """Titres de chapitres, lus dans la table des matieres du livre.

    Deux formats coexistent et il faut les deux : EPUB 3 declare un document de
    navigation, EPUB 2 un fichier NCX. Beaucoup de livres recents portent
    encore les deux, beaucoup d'anciens n'ont que le second.

    Facultatif par construction : un livre sans table exploitable reste
    parfaitement lisible, chapitre par chapitre. On rend donc ce qu'on trouve,
    sans jamais echouer.
    """
    for item in racine.iter(f"{_OPF}item"):
        href = item.get("href", "")
        est_nav = "nav" in (item.get("properties") or "")
        est_ncx = item.get("media-type") == "application/x-dtbncx+xml"
        if not (est_nav or est_ncx) or not href:
            continue
        chemin = posixpath.normpath(posixpath.join(base, href)) if base else href
        try:
            arbre = ElementTree.fromstring(archive.read(chemin))  # noqa: S314
        except (KeyError, ElementTree.ParseError):
            continue

        dossier = posixpath.dirname(chemin)
        trouves = _from_nav(arbre, dossier) if est_nav else _from_ncx(arbre, dossier)
        if trouves:
            return trouves
    return {}


def _from_nav(arbre, dossier: str) -> dict[str, str]:
    """Table des matieres EPUB 3 : de simples liens dans du XHTML."""
    titres: dict[str, str] = {}
    for lien in arbre.iter():
        if not lien.tag.endswith("a"):
            continue
        href = (lien.get("href") or "").split("#")[0]
        texte = "".join(lien.itertext()).strip()
        if href and texte:
            titres.setdefault(_join(dossier, href), texte)
    return titres


def _from_ncx(arbre, dossier: str) -> dict[str, str]:
    """Table des matieres EPUB 2 : navPoint / navLabel / content."""
    titres: dict[str, str] = {}
    for point in arbre.iter():
        if not point.tag.endswith("navPoint"):
            continue
        libelle = next(
            ("".join(n.itertext()).strip() for n in point.iter() if n.tag.endswith("text")),
            "",
        )
        cible = next(
            (n.get("src", "") for n in point.iter() if n.tag.endswith("content")),
            "",
        )
        href = cible.split("#")[0]
        if href and libelle:
            titres.setdefault(_join(dossier, href), libelle)
    return titres


def _join(dossier: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(dossier, href)) if dossier else href


def chapter_html(path: Path, index: int, asset_base: str) -> str:
    """Le contenu d'un chapitre, nettoye et pret a etre affiche.

    Les liens vers les ressources internes — images, feuilles de style — sont
    reecrits vers l'API : sans cela, le navigateur les chercherait a la racine
    du site et n'afficherait aucune illustration.
    """
    chapitres = spine(path)
    if not 0 <= index < len(chapitres):
        return ""

    href = chapitres[index].href
    with zipfile.ZipFile(path) as archive:
        try:
            brut = archive.read(href).decode("utf-8", errors="replace")
        except KeyError:
            return ""

    corps = trouve.group("corps") if (trouve := _CORPS.search(brut)) else brut
    return _rewrite_assets(sanitize(corps), posixpath.dirname(href), asset_base)


def sanitize(html: str) -> str:
    """Retire ce qui peut s'executer.

    Filtrage, donc faillible par nature — c'est pourquoi l'affichage se fait en
    plus dans un cadre bac-a-sable sans autorisation d'execution. Les deux
    protections sont independantes : celle-ci echoue sur un cas exotique, la
    seconde tient quand meme.
    """
    html = _SCRIPT.sub("", html)
    html = _CADRE.sub("", html)
    html = _EVENEMENT.sub("", html)
    return _JS_URL.sub("", html)


def _rewrite_assets(html: str, dossier: str, asset_base: str) -> str:
    def remplace(trouve: re.Match) -> str:
        attr, url = trouve.group("attr"), trouve.group("url")
        # Les liens externes et les ancres restent tels quels : reecrire une
        # ancre casserait la navigation interne au chapitre.
        if url.startswith(("http://", "https://", "#", "data:", "mailto:")):
            return trouve.group(0)
        interne = posixpath.normpath(posixpath.join(dossier, url)) if dossier else url
        return f'{attr}="{asset_base}{interne}"'

    return _RESSOURCE.sub(remplace, html)


def asset(path: Path, interne: str) -> tuple[bytes, str] | None:
    """Une ressource interne au livre : image, feuille de style, police.

    La ressource est cherchee dans la LISTE des entrees de l'archive. C'est ce
    qui rend l'operation sure sans verification de chemin : une entree
    inexistante ne rend rien, et un « ../ » dans l'URL ne designe aucune entree
    reelle.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            if interne not in archive.namelist():
                return None
            contenu = archive.read(interne)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    type_mime = mimetypes.guess_type(interne)[0] or "application/octet-stream"
    return contenu, type_mime


def _locate_opf(archive: zipfile.ZipFile) -> str | None:
    try:
        container = ElementTree.fromstring(archive.read(_CONTAINER))  # noqa: S314
    except (KeyError, ElementTree.ParseError):
        return None
    for element in container.iter():
        if element.tag.endswith("rootfile") and (chemin := element.get("full-path")):
            return chemin
    return None
