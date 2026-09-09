"""Evacuation des copies dont le fichier est deja range.

Le cas concret : 340 fichiers echouent avec « la destination existe deja ». Ils
ont bien ete ranges lors d'un passage precedent, mais une copie subsiste dans
les telechargements. Elle occupe la place et revient a chaque scan.

La demande naturelle est « supprime l'ancien ». Ces tests verifient qu'on ne le
fait pas tout a fait : trois conditions doivent tenir, et la moindre incertitude
sur l'identite des deux fichiers vaut un refus. Une suppression n'est jamais
rattrapable ; c'est precisement ce qu'un outil de rangement ne doit pas se
permettre.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.journal import Journal, evacuate_ranged_source, undo_last
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision


@pytest.fixture
def space(tmp_path: Path) -> Path:
    (tmp_path / "dl").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / ".corbeille").mkdir()
    return tmp_path


@pytest.fixture
def journal(space: Path) -> Journal:
    return Journal(space / "journal.jsonl")


def plan_for(space: Path, *, source_bytes: bytes, dest_bytes: bytes | None) -> Plan:
    """Un fichier telecharge, et sa version rangee — ou son absence."""
    source = space / "dl" / "Severance.S01E01.mkv"
    source.write_bytes(source_bytes)

    destination = space / "lib" / "Severance" / "S01E01.mkv"
    if dest_bytes is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(dest_bytes)

    return Plan(
        id="p1",
        source=source,
        destination=destination,
        kind="episode",
        score=0.95,
        decision=Decision.AUTO,
        title="Severance",
        year=2022,
    )


def trash(space: Path) -> Path:
    return space / "lib" / ".corbeille"


# --- Le cas nominal ---------------------------------------------------------


def test_une_copie_identique_part_en_corbeille(space: Path, journal: Journal) -> None:
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")

    result = evacuate_ranged_source(plan, journal, trash(space))

    assert result.ok is True
    assert not plan.source.exists(), "la copie a quitte les telechargements"
    assert plan.destination.is_file(), "le fichier range n'a pas bouge"


def test_rien_n_est_supprime(space: Path, journal: Journal) -> None:
    """Le point qui separe cette fonction d'un « rm ». La copie doit rester
    retrouvable : un doublon mal identifie se rattrape, une suppression non."""
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")
    evacuate_ranged_source(plan, journal, trash(space))

    dans_corbeille = list(trash(space).rglob("*.mkv"))
    assert len(dans_corbeille) == 1
    assert dans_corbeille[0].read_bytes() == b"video"


def test_l_evacuation_est_annulable(space: Path, journal: Journal) -> None:
    """Journalisee comme n'importe quel deplacement : se tromper de lot ne doit
    pas etre definitif."""
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")
    evacuate_ranged_source(plan, journal, trash(space))

    results = undo_last(journal, 1)

    assert results[0].ok is True
    assert plan.source.is_file(), "la copie est revenue dans les telechargements"


# --- Les refus, qui sont l'essentiel ---------------------------------------


def test_une_taille_differente_est_refusee(space: Path, journal: Journal) -> None:
    """Deux encodages d'un meme episode visent le meme nom sans etre le meme
    fichier. Les confondre ferait perdre un exemplaire distinct."""
    plan = plan_for(space, source_bytes=b"un encodage plus long", dest_bytes=b"court")

    result = evacuate_ranged_source(plan, journal, trash(space))

    assert result.ok is False
    assert result.reason == "size_mismatch"
    assert plan.source.is_file(), "rien n'a ete touche"


def test_un_fichier_jamais_range_est_refuse(space: Path, journal: Journal) -> None:
    """Sans ce controle, la fonction supprimerait une source qui n'est nulle
    part ailleurs — la perte seche."""
    plan = plan_for(space, source_bytes=b"video", dest_bytes=None)

    result = evacuate_ranged_source(plan, journal, trash(space))

    assert result.ok is False
    assert result.reason == "not_ranged"
    assert plan.source.is_file()


def test_une_source_deja_absente_ne_leve_pas(space: Path, journal: Journal) -> None:
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")
    plan.source.unlink()

    result = evacuate_ranged_source(plan, journal, trash(space))

    assert result.ok is False
    assert result.reason == "source_missing"


def test_sans_corbeille_rien_ne_bouge(space: Path, journal: Journal) -> None:
    """Mieux vaut laisser du desordre que d'inventer une destination."""
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")

    result = evacuate_ranged_source(plan, journal, None)

    assert result.ok is False
    assert result.reason == "no_trash"
    assert plan.source.is_file()


def test_un_plan_sans_destination_est_refuse(space: Path, journal: Journal) -> None:
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")
    plan.destination = None

    assert evacuate_ranged_source(plan, journal, trash(space)).ok is False


def test_un_exemplaire_surnumeraire_est_supprime(space: Path, journal: Journal) -> None:
    """Troisieme exemplaire : un en bibliotheque, un en corbeille, celui-ci en
    trop. Le refuser le ferait revenir a chaque scan sans jamais se resoudre.

    C'est le seul cas ou l'evacuation supprime au lieu de deplacer, et il est
    encadre : le fichier est deja a destination ET deja en corbeille, tous deux
    verifies a la taille."""
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")
    evacuate_ranged_source(plan, journal, trash(space))

    # Meme chemin d'origine, donc meme nom aplati en corbeille.
    plan.source.write_bytes(b"video")
    result = evacuate_ranged_source(plan, journal, trash(space))

    assert result.ok is True
    assert not plan.source.exists()
    assert len(list(trash(space).rglob("*.mkv"))) == 1, "la corbeille n'a pas ete doublee"
    assert plan.destination.is_file(), "la bibliotheque est intacte"


def test_un_homonyme_de_taille_differente_est_refuse(space: Path, journal: Journal) -> None:
    """L'homonymie ne suffit pas a conclure a l'identite : si la copie en
    corbeille differe, on ne supprime rien."""
    plan = plan_for(space, source_bytes=b"video", dest_bytes=b"video")
    evacuate_ranged_source(plan, journal, trash(space))

    # Meme nom, meme taille a destination, mais la corbeille contient autre
    # chose — on l'y remplace pour simuler une collision reelle.
    en_corbeille = next(trash(space).rglob("*.mkv"))
    en_corbeille.write_bytes(b"un tout autre contenu, plus long")
    plan.source.write_bytes(b"video")

    result = evacuate_ranged_source(plan, journal, trash(space))

    assert result.ok is False
    assert result.reason == "destination_exists"
    assert plan.source.is_file(), "rien n'a ete supprime"


# --- Ou placer la corbeille -------------------------------------------------
#
# Ce n'est pas un detail de rangement, c'est un facteur mille sur le temps. Un
# deplacement a l'interieur d'un volume est un renommage, instantane quelle que
# soit la taille. D'un volume a l'autre, il faut recopier puis supprimer : sur
# un NAS ou telechargements et bibliotheque sont deux partages distincts,
# evacuer trois cents fichiers revenait a recopier des centaines de gigaoctets
# pour ne rien produire.


def test_la_corbeille_suit_la_racine_qui_contient_le_fichier(tmp_path) -> None:
    from sortilege.core.companions import TRASH_DIRNAME, trash_root_for

    downloads = tmp_path / "downloads"
    library = tmp_path / "media"
    downloads.mkdir()
    library.mkdir()
    fichier = downloads / "film.mkv"
    fichier.write_bytes(b"x")

    assert trash_root_for(fichier, library, [downloads]) == downloads / TRASH_DIRNAME


def test_sans_racine_source_connue_on_retombe_sur_la_bibliotheque(tmp_path) -> None:
    """Mieux vaut une copie lente qu'un refus."""
    from sortilege.core.companions import TRASH_DIRNAME, trash_root_for

    library = tmp_path / "media"
    library.mkdir()
    fichier = tmp_path / "ailleurs.mkv"
    fichier.write_bytes(b"x")

    assert trash_root_for(fichier, library, []) == library / TRASH_DIRNAME


def test_un_fichier_disparu_ne_leve_pas(tmp_path) -> None:
    from sortilege.core.companions import TRASH_DIRNAME, trash_root_for

    library = tmp_path / "media"
    library.mkdir()
    assert trash_root_for(tmp_path / "absent.mkv", library, []) == library / TRASH_DIRNAME
