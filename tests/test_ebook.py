"""Lecture des metadonnees d'un livre numerique.

Renversement complet par rapport a la video, et c'est ce qui rend les livres
plus faciles a ranger qu'un film.

Un fichier video ne dit presque rien de lui-meme. Un EPUB, lui, PORTE ses
metadonnees : le format impose un manifeste renseigne par celui qui a fabrique
le fichier. Ici le fichier fait autorite, et le fournisseur ne sert qu'a
completer — l'inverse exact de la video.

Ces tests construisent de vrais EPUB, c'est-a-dire de vrais ZIP avec un vrai
manifeste. Simuler la lecture ne prouverait rien : ce qu'on veut savoir, c'est
qu'on lit correctement des fichiers tels qu'ils existent.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from sortilege.core.ebook import BOOK_EXTENSIONS, read

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""


def epub(path: Path, opf: str) -> Path:
    """Un EPUB minimal mais VALIDE : un ZIP, un container, un manifeste."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
    return path


def manifeste(interieur: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    {interieur}
  </metadata>
</package>"""


# --- Ce que le fichier dit de lui-meme --------------------------------------


def test_le_titre_et_l_auteur_sont_lus(tmp_path: Path) -> None:
    livre = epub(
        tmp_path / "x.epub",
        manifeste("<dc:title>Le Nom du Vent</dc:title><dc:creator>Patrick Rothfuss</dc:creator>"),
    )

    meta = read(livre)

    assert meta.title == "Le Nom du Vent"
    assert meta.authors == ["Patrick Rothfuss"]
    assert meta.trustworthy, "titre + auteur suffisent a ranger"


def test_plusieurs_auteurs_sont_conserves(tmp_path: Path) -> None:
    """Les recueils et les ouvrages a quatre mains sont courants ; n'en garder
    qu'un rangerait le livre sous une moitie de sa paternite."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            "<dc:title>La Bonne Apocalypse</dc:title>"
            "<dc:creator>Terry Pratchett</dc:creator>"
            "<dc:creator>Neil Gaiman</dc:creator>"
        ),
    )

    assert read(livre).authors == ["Terry Pratchett", "Neil Gaiman"]
    assert read(livre).author == "Terry Pratchett, Neil Gaiman"


def test_l_annee_est_extraite_d_une_date_complete(tmp_path: Path) -> None:
    livre = epub(
        tmp_path / "x.epub",
        manifeste("<dc:title>T</dc:title><dc:date>2007-03-27T00:00:00+00:00</dc:date>"),
    )

    assert read(livre).year == 2007


def test_la_serie_calibre_est_lue(tmp_path: Path) -> None:
    """La serie n'a pas de champ standard avant EPUB 3. Calibre a impose une
    convention que tout le monde suit desormais ; l'ignorer priverait du seul
    moyen de regrouper une saga."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            "<dc:title>Le Nom du Vent</dc:title>"
            '<meta name="calibre:series" content="Chronique du Tueur de Roi"/>'
            '<meta name="calibre:series_index" content="1"/>'
        ),
    )

    meta = read(livre)

    assert meta.series == "Chronique du Tueur de Roi"
    assert meta.volume == 1


def test_le_tome_est_devine_depuis_le_titre(tmp_path: Path) -> None:
    """Faute de champ dedie, le numero se trouve presque toujours dans le
    titre."""
    livre = epub(tmp_path / "x.epub", manifeste("<dc:title>Fondation - Tome 3</dc:title>"))

    assert read(livre).volume == 3


def test_l_isbn_est_reconnu_parmi_les_identifiants(tmp_path: Path) -> None:
    """Un UUID identifie le FICHIER, pas l'oeuvre : le retenir ne permettrait
    pas de la retrouver."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            "<dc:title>T</dc:title>"
            '<dc:identifier id="uuid">urn:uuid:1234-5678-abcd</dc:identifier>'
            '<dc:identifier opf:scheme="ISBN" '
            'xmlns:opf="http://www.idpf.org/2007/opf">978-2-2660-2119-6</dc:identifier>'
        ),
    )

    assert read(livre).isbn == "9782266021196"


# --- Ce qu'on refuse de croire ----------------------------------------------


def test_un_titre_pose_par_un_outil_est_ignore(tmp_path: Path) -> None:
    """« Microsoft Word - document1 » ne designe aucune oeuvre. Le retenir
    ferait ranger des livres sous ce nom."""
    pdf = tmp_path / "livre.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 100 + b"/Title (Microsoft Word - document1)\n%%EOF")

    assert read(pdf).title == ""


def test_un_titre_pdf_reel_est_conserve(tmp_path: Path) -> None:
    pdf = tmp_path / "livre.pdf"
    pdf.write_bytes(
        b"%PDF-1.4\n" + b"x" * 100 + b"/Title (Les Miserables)\n/Author (Victor Hugo)\n%%EOF"
    )

    meta = read(pdf)

    assert meta.title == "Les Miserables"
    assert meta.authors == ["Victor Hugo"]


def test_un_fichier_illisible_ne_leve_pas(tmp_path: Path) -> None:
    """Un livre sans metadonnees se range d'apres son nom : c'est moins bon,
    mais l'ecarter serait pire."""
    faux = tmp_path / "casse.epub"
    faux.write_bytes(b"ceci n'est pas un zip")

    meta = read(faux)

    assert meta.title == ""
    assert meta.read is False


def test_un_format_sans_lecteur_ne_leve_pas(tmp_path: Path) -> None:
    """MOBI, CBZ, DJVU : ranges d'apres leur nom, faute de savoir les ouvrir."""
    mobi = tmp_path / "livre.mobi"
    mobi.write_bytes(b"x" * 100)

    assert read(mobi).read is False


def test_les_formats_reconnus_couvrent_l_essentiel() -> None:
    for extension in (".epub", ".pdf", ".mobi", ".azw3", ".cbz"):
        assert extension in BOOK_EXTENSIONS
