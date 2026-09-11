"""Le manifeste qu'un serveur multimedia lira a cote d'un livre.

Jellyfin ne lit pas de ``.nfo`` pour les livres : il cherche un
``metadata.opf`` ou un ``ComicInfo.xml`` a cote du fichier. Ce que ces tests
verifient n'est donc pas « le module produit du XML », mais « ce XML est celui
que le monde du livre numerique lit deja » — d'ou la relecture par le lecteur
d'EPUB du projet lui-meme, qui n'a rien de complaisant : il ignore tout de la
facon dont ces fichiers ont ete ecrits.

Comme dans ``test_ebook``, les EPUB sont de VRAIS EPUB : de vrais ZIP avec un
vrai manifeste. Simuler la lecture prouverait seulement que nos deux moities
sont d'accord entre elles.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from sortilege.core import ebook, opf
from sortilege.core.ebook import BookMeta
from sortilege.core.nfo import OnExisting, Outcome

CONTAINER = """<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

# Un vrai debut de JPEG : ce qu'on veut prouver est que les octets extraits
# sont EXACTEMENT ceux du livre, pas une image ressemblante.
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(64))
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(32))


def epub(path: Path, opf_xml: str, ressources: dict[str, bytes] | None = None) -> Path:
    """Un EPUB minimal mais VALIDE : un ZIP, un container, un manifeste."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf_xml)
        for nom, contenu in (ressources or {}).items():
            archive.writestr(nom, contenu)
    return path


def manifeste(metadata: str = "", manifest: str = "") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="x">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Un livre</dc:title>
    {metadata}
  </metadata>
  <manifest>
    {manifest}
  </manifest>
</package>"""


COMPLET = BookMeta(
    title="Le Nom du Vent",
    authors=["Patrick Rothfuss"],
    series="Chronique du Tueur de Roi",
    volume=1,
    year=2009,
    publisher="Bragelonne",
    language="fr",
    isbn="9782352944034",
)


# --- Ce qu'on ecrit doit se relire ------------------------------------------


def test_le_manifeste_se_relit_avec_le_lecteur_d_epub_du_projet(tmp_path: Path) -> None:
    """La preuve d'interoperabilite : notre manifeste, glisse dans un EPUB,
    est lu par ``core/ebook`` comme n'importe quel manifeste d'editeur. Si le
    lecteur du projet s'y retrouve, Calibre et Jellyfin aussi — ils parlent le
    meme Dublin Core."""
    livre = epub(tmp_path / "x.epub", opf.metadata_xml(COMPLET))

    relu = ebook.read(livre)

    assert relu.title == COMPLET.title
    assert relu.authors == COMPLET.authors
    assert relu.series == COMPLET.series
    assert relu.volume == COMPLET.volume
    assert relu.year == COMPLET.year
    assert relu.publisher == COMPLET.publisher
    assert relu.language == COMPLET.language
    assert relu.isbn == COMPLET.isbn


def test_le_role_d_auteur_est_declare() -> None:
    """Sans « aut », un traducteur et un auteur sont le meme dc:creator."""
    xml = opf.metadata_xml(COMPLET)

    assert '<dc:creator opf:role="aut">Patrick Rothfuss</dc:creator>' in xml


def test_les_champs_inconnus_ne_sont_pas_ecrits() -> None:
    """Un element vide n'informe pas : il INTERDIT au serveur de completer le
    champ depuis un fournisseur. Absent, il le laisse chercher."""
    xml = opf.metadata_xml(BookMeta(title="Nu", authors=["Anon"]))

    assert "dc:publisher" not in xml
    assert "dc:identifier" not in xml
    assert "dc:date" not in xml
    # Pas d'identifiant, donc pas de unique-identifier : l'attribut designerait
    # un element qui n'existe pas.
    assert "unique-identifier" not in xml


def test_un_tome_sans_serie_n_est_pas_ecrit() -> None:
    """Un « tome 3 » sans serie ne designe rien du tout."""
    xml = opf.metadata_xml(BookMeta(title="Orphelin", volume=3))

    assert "calibre:series" not in xml


def test_l_annee_seule_est_ecrite_sans_jour_invente() -> None:
    xml = opf.metadata_xml(BookMeta(title="T", year=2009))

    assert "<dc:date>2009</dc:date>" in xml


def test_le_manifeste_est_stable_d_une_ecriture_a_l_autre() -> None:
    """Deux passages doivent produire le meme fichier, sinon chaque rangement
    modifierait la bibliotheque sans rien y changer."""
    assert opf.metadata_xml(COMPLET) == opf.metadata_xml(COMPLET)


# --- La couverture, dans les deux formes qui existent -----------------------


def test_la_couverture_epub2_est_extraite(tmp_path: Path) -> None:
    """EPUB 2 n'a pas d'attribut pour cela : la pratique est une balise
    <meta name="cover"> pointant vers un item du manifeste."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            metadata='<meta name="cover" content="couv"/>',
            manifest='<item id="couv" href="images/couv.jpg" media-type="image/jpeg"/>',
        ),
        {"OEBPS/images/couv.jpg": JPEG},
    )

    assert opf.extract_cover(livre) is Outcome.WRITTEN
    assert (tmp_path / "cover.jpg").read_bytes() == JPEG


def test_la_couverture_epub3_est_extraite(tmp_path: Path) -> None:
    """EPUB 3 la marque par properties="cover-image", parmi d'autres jetons."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            manifest=(
                '<item id="p" href="images/p.png" media-type="image/png" properties="cover-image"/>'
            )
        ),
        {"OEBPS/images/p.png": PNG},
    )

    assert opf.extract_cover(livre) is Outcome.WRITTEN
    # PNG garde son extension : le renommer « .jpg » serait un mensonge que
    # les lecteurs stricts refusent, et Jellyfin lit cover.png aussi bien.
    assert (tmp_path / "cover.png").read_bytes() == PNG


def test_la_forme_epub3_l_emporte_sur_la_convention_heritee(tmp_path: Path) -> None:
    """Beaucoup de livres portent les deux ; seule la premiere est normative."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            metadata='<meta name="cover" content="vieux"/>',
            manifest=(
                '<item id="vieux" href="a.jpg" media-type="image/jpeg"/>'
                '<item id="neuf" href="b.png" media-type="image/png" properties="cover-image"/>'
            ),
        ),
        {"OEBPS/a.jpg": JPEG, "OEBPS/b.png": PNG},
    )

    opf.extract_cover(livre)

    assert (tmp_path / "cover.png").read_bytes() == PNG


def test_un_href_percent_encode_retrouve_son_entree(tmp_path: Path) -> None:
    """Les href d'un manifeste sont des IRI ; le ZIP, lui, stocke le nom en clair."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            manifest=(
                '<item id="c" href="images/cover%20art.jpg" media-type="image/jpeg" '
                'properties="cover-image"/>'
            )
        ),
        {"OEBPS/images/cover art.jpg": JPEG},
    )

    assert opf.extract_cover(livre) is Outcome.WRITTEN
    assert (tmp_path / "cover.jpg").read_bytes() == JPEG


def test_une_couverture_declaree_mais_absente_ne_leve_pas(tmp_path: Path) -> None:
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            manifest=(
                '<item id="c" href="fantome.jpg" media-type="image/jpeg" properties="cover-image"/>'
            )
        ),
    )

    assert opf.extract_cover(livre) is None


def test_aucune_image_n_est_choisie_au_hasard(tmp_path: Path) -> None:
    """Sans declaration, on ne devine pas : une illustration de chapitre
    promue couverture serait une erreur presentee comme une donnee."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(manifest='<item id="i" href="images/chap1.jpg" media-type="image/jpeg"/>'),
        {"OEBPS/images/chap1.jpg": JPEG},
    )

    assert opf.cover_data(livre) is None


def test_une_couverture_demesuree_est_refusee_sans_etre_lue(tmp_path: Path) -> None:
    """La taille declaree suffit a decider : decompresser d'abord reviendrait
    a s'en remettre a la bonne volonte d'un fichier venu d'internet."""
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            manifest=(
                '<item id="c" href="gros.jpg" media-type="image/jpeg" properties="cover-image"/>'
            )
        ),
        {"OEBPS/gros.jpg": b"\x00" * (opf.MAX_COVER_BYTES + 1)},
    )

    assert opf.cover_data(livre) is None


# --- Rien n'est ecrase sans qu'on l'ait demande -----------------------------


def test_un_manifeste_existant_est_laisse_en_place(tmp_path: Path) -> None:
    """Calibre a pu l'ecrire, et le sien est plus riche que le notre."""
    livre = tmp_path / "x.epub"
    livre.write_bytes(b"")
    existant = opf.manifest_path(livre)
    existant.write_text("<package>a moi</package>", encoding="utf-8")

    assert opf.write_metadata(livre, COMPLET) is Outcome.SKIPPED
    assert existant.read_text(encoding="utf-8") == "<package>a moi</package>"


def test_l_ecrasement_doit_etre_demande_explicitement(tmp_path: Path) -> None:
    livre = tmp_path / "x.epub"
    livre.write_bytes(b"")
    cible = opf.manifest_path(livre)
    cible.write_text("ancien", encoding="utf-8")

    assert opf.write_metadata(livre, COMPLET, on_existing=OnExisting.OVERWRITE) is Outcome.REPLACED
    assert "Le Nom du Vent" in cible.read_text(encoding="utf-8")


def test_la_sauvegarde_conserve_le_contenu_d_avant_nous(tmp_path: Path) -> None:
    livre = tmp_path / "x.epub"
    livre.write_bytes(b"")
    cible = opf.manifest_path(livre)
    cible.write_text("ancien", encoding="utf-8")

    assert opf.write_metadata(livre, COMPLET, on_existing=OnExisting.BACKUP) is Outcome.BACKED_UP
    assert (tmp_path / "metadata.opf.bak").read_text(encoding="utf-8") == "ancien"
    assert "Le Nom du Vent" in cible.read_text(encoding="utf-8")


def test_un_content_opf_deja_present_fait_renoncer(tmp_path: Path) -> None:
    """Jellyfin lit les deux noms sans ordre documente : en poser un second
    laisserait le hasard decider lequel decrit le livre."""
    livre = tmp_path / "x.epub"
    livre.write_bytes(b"")
    (tmp_path / "content.opf").write_text("<package/>", encoding="utf-8")

    assert opf.write_metadata(livre, COMPLET) is Outcome.SKIPPED
    assert not opf.manifest_path(livre).exists()


def test_une_couverture_existante_est_respectee(tmp_path: Path) -> None:
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            manifest='<item id="c" href="c.jpg" media-type="image/jpeg" properties="cover-image"/>'
        ),
        {"OEBPS/c.jpg": JPEG},
    )
    (tmp_path / "cover.jpg").write_bytes(b"la mienne")

    assert opf.extract_cover(livre) is Outcome.SKIPPED
    assert (tmp_path / "cover.jpg").read_bytes() == b"la mienne"


# --- Ecriture atomique ------------------------------------------------------


def test_une_ecriture_interrompue_laisse_l_existant_intact(tmp_path: Path, monkeypatch) -> None:
    """Un manifeste tronque serait relu comme faisant autorite : il est PIRE
    qu'un manifeste absent. Le renommage final est ce qui rend l'etat
    intermediaire invisible."""
    livre = tmp_path / "x.epub"
    livre.write_bytes(b"")
    cible = opf.manifest_path(livre)
    cible.write_text("ancien", encoding="utf-8")

    def coupure(*_args: object, **_kwargs: object) -> None:
        raise OSError("coupure de courant")

    monkeypatch.setattr(os, "replace", coupure)

    with pytest.raises(OSError, match="coupure"):
        opf.write_metadata(livre, COMPLET, on_existing=OnExisting.OVERWRITE)

    assert cible.read_text(encoding="utf-8") == "ancien"
    assert not list(tmp_path.glob(".sortilege-*")), "un temporaire est reste dans la bibliotheque"


def test_aucun_temporaire_ne_subsiste_apres_une_ecriture_reussie(tmp_path: Path) -> None:
    livre = tmp_path / "x.epub"
    livre.write_bytes(b"")

    assert opf.write_metadata(livre, COMPLET) is Outcome.WRITTEN
    assert not list(tmp_path.glob(".sortilege-*"))
    assert os.path.exists(opf.manifest_path(livre))


# --- Depot complet ----------------------------------------------------------


def test_un_livre_sans_couverture_recoit_quand_meme_son_manifeste(tmp_path: Path) -> None:
    """L'identite du livre est dans le manifeste ; l'image n'est qu'un
    agrement. Lier le sort du premier a celle-ci priverait de metadonnees tous
    les livres sans illustration."""
    livre = epub(tmp_path / "x.epub", manifeste())

    depot = opf.deposit(livre, COMPLET)

    assert depot.manifest is Outcome.WRITTEN
    assert depot.cover is None, "None dit « rien a extraire », SKIPPED dirait « deja la »"
    assert "Le Nom du Vent" in opf.manifest_path(livre).read_text(encoding="utf-8")


def test_le_depot_pose_les_deux_fichiers_a_cote_du_livre(tmp_path: Path) -> None:
    livre = epub(
        tmp_path / "x.epub",
        manifeste(
            manifest='<item id="c" href="c.jpg" media-type="image/jpeg" properties="cover-image"/>'
        ),
        {"OEBPS/c.jpg": JPEG},
    )

    depot = opf.deposit(livre, COMPLET)

    assert (depot.manifest, depot.cover) == (Outcome.WRITTEN, Outcome.WRITTEN)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cover.jpg", "metadata.opf", "x.epub"]
