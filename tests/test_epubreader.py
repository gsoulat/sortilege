"""Lecture d'un EPUB dans le navigateur.

Le contenu d'un EPUB est du **code etranger**. Le format autorise le
JavaScript, et un livre telecharge n'est pas plus digne de confiance qu'une
page web quelconque : n'importe qui peut fabriquer un fichier contenant un
script qui lira la session de l'utilisateur.

Ces tests portent donc d'abord sur ce qui est RETIRE. Le rendu correct d'un
chapitre est secondaire — un livre mal mis en page reste lisible, un livre qui
execute du code ne l'est pas.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from sortilege.core.epubreader import asset, chapter_html, sanitize, spine

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
 <rootfiles><rootfile full-path="OEBPS/content.opf"
  media-type="application/oebps-package+xml"/></rootfiles></container>"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
 <manifest>
  <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
  <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>"""

NAV = """<html xmlns="http://www.w3.org/1999/xhtml"><body><nav>
 <ol><li><a href="ch1.xhtml">Prologue</a></li>
 <li><a href="ch2.xhtml">Un endroit pour les démons</a></li></ol></nav></body></html>"""


def livre(tmp_path: Path, chapitre1: str = "<html><body><p>Texte.</p></body></html>") -> Path:
    chemin = tmp_path / "livre.epub"
    with zipfile.ZipFile(chemin, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", OPF)
        z.writestr("OEBPS/nav.xhtml", NAV)
        z.writestr("OEBPS/ch1.xhtml", chapitre1)
        z.writestr("OEBPS/ch2.xhtml", "<html><body><p>Deux.</p></body></html>")
        z.writestr("OEBPS/images/logo.png", b"\x89PNG")
    return chemin


# --- Ce qui est retire ------------------------------------------------------


def test_un_script_est_retire() -> None:
    """LE test qui compte. Un livre telecharge n'est pas plus digne de confiance
    qu'une page web quelconque."""
    propre = sanitize("<p>Bonjour</p><script>vol()</script>")

    assert "<script" not in propre
    assert "vol()" not in propre
    assert "Bonjour" in propre, "le texte du livre, lui, reste"


def test_un_gestionnaire_d_evenement_est_retire() -> None:
    """Un script n'a pas besoin de balise pour s'executer."""
    propre = sanitize("<p onclick=\"vol()\" onmouseover='vol()'>Texte</p>")

    assert "onclick" not in propre
    assert "onmouseover" not in propre
    assert "Texte" in propre


def test_un_lien_javascript_est_retire() -> None:
    assert "javascript:" not in sanitize('<a href="javascript:vol()">Suite</a>')


def test_un_cadre_est_retire() -> None:
    """Un cadre chargerait une page distante depuis l'interieur du livre."""
    assert "<iframe" not in sanitize('<iframe src="http://ailleurs"></iframe>')


# --- Le fil de lecture ------------------------------------------------------


def test_les_chapitres_suivent_l_ordre_de_l_editeur(tmp_path: Path) -> None:
    """L'ordre du ZIP n'a aucun sens de lecture : un livre dont on aurait
    melange les pages n'est pas un livre."""
    chapitres = spine(livre(tmp_path))

    assert [c.index for c in chapitres] == [0, 1]
    assert chapitres[0].href == "OEBPS/ch1.xhtml"


def test_les_titres_viennent_de_la_table_des_matieres(tmp_path: Path) -> None:
    titres = [c.title for c in spine(livre(tmp_path))]

    assert titres == ["Prologue", "Un endroit pour les démons"]


def test_un_fichier_illisible_ne_leve_pas(tmp_path: Path) -> None:
    casse = tmp_path / "casse.epub"
    casse.write_bytes(b"pas un zip")

    assert spine(casse) == []


# --- Le contenu servi -------------------------------------------------------


def test_le_corps_seul_est_rendu(tmp_path: Path) -> None:
    """Renvoyer le document entier imbriquerait un <html> dans un autre."""
    html = chapter_html(livre(tmp_path), 0, "/base?i=")

    assert "<body" not in html
    assert "Texte." in html


def test_les_images_pointent_vers_l_api(tmp_path: Path) -> None:
    """Sans reecriture, le navigateur chercherait l'image a la racine du site et
    n'afficherait rien."""
    source = '<html><body><img src="images/logo.png"/></body></html>'

    html = chapter_html(livre(tmp_path, source), 0, "/base?i=")

    assert 'src="/base?i=OEBPS/images/logo.png"' in html


def test_un_lien_externe_reste_intact(tmp_path: Path) -> None:
    source = '<html><body><a href="https://exemple.fr">Suite</a></body></html>'

    assert 'href="https://exemple.fr"' in chapter_html(livre(tmp_path, source), 0, "/base?i=")


def test_un_chapitre_inexistant_ne_leve_pas(tmp_path: Path) -> None:
    assert chapter_html(livre(tmp_path), 99, "/base?i=") == ""


# --- Les ressources internes ------------------------------------------------


def test_une_ressource_du_livre_est_servie(tmp_path: Path) -> None:
    trouve = asset(livre(tmp_path), "OEBPS/images/logo.png")

    assert trouve is not None
    contenu, type_mime = trouve
    assert contenu.startswith(b"\x89PNG")
    assert type_mime == "image/png"


def test_une_ressource_hors_du_livre_n_existe_pas(tmp_path: Path) -> None:
    """La ressource est cherchee dans la LISTE des entrees de l'archive : un
    « ../ » ne designe aucune entree reelle, et rien ne sort du fichier."""
    assert asset(livre(tmp_path), "../../etc/passwd") is None
    assert asset(livre(tmp_path), "/etc/passwd") is None
