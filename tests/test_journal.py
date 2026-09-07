"""Tests de l'application et de l'annulation.

C'est le seul module qui ecrit sur le disque : les tests portent d'abord sur ce
qu'il refuse de faire.
"""

from pathlib import Path

import pytest

from sortilege.core.journal import Journal, MoveRecord, apply_plan, undo_last
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision


@pytest.fixture
def env(tmp_path: Path):
    src = tmp_path / "dl"
    dst = tmp_path / "media"
    src.mkdir()
    dst.mkdir()
    journal = Journal(tmp_path / "journal.jsonl")
    return src, dst, journal


def make_plan(source: Path, destination: Path | None) -> Plan:
    return Plan(
        id="abc123",
        source=source,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
    )


def test_simulation_ne_deplace_rien(env) -> None:
    src, dst, journal = env
    f = src / "film.mkv"
    f.touch()
    target = dst / "Films" / "Dune.mkv"

    result = apply_plan(make_plan(f, target), journal, dry_run=True)

    assert result.ok is True
    assert result.simulated is True
    assert f.is_file()
    assert not target.exists()
    assert journal.read_all() == []


def test_deplacement_reel(env) -> None:
    src, dst, journal = env
    f = src / "film.mkv"
    f.write_text("contenu")
    target = dst / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    result = apply_plan(make_plan(f, target), journal, dry_run=False)

    assert result.ok is True
    assert not f.exists()
    assert target.read_text() == "contenu"
    assert len(journal.read_all()) == 1


def test_destination_existante_jamais_ecrasee(env) -> None:
    """Le pire defaut possible pour un rangeur serait de perdre un fichier."""
    src, dst, journal = env
    f = src / "film.mkv"
    f.write_text("nouveau")
    target = dst / "Dune.mkv"
    target.write_text("existant")

    result = apply_plan(make_plan(f, target), journal, dry_run=False)

    assert result.ok is False
    assert "existe deja" in result.message
    assert target.read_text() == "existant"
    assert f.is_file()
    assert journal.read_all() == []


def test_source_disparue(env) -> None:
    src, dst, journal = env
    result = apply_plan(make_plan(src / "absent.mkv", dst / "x.mkv"), journal, dry_run=False)
    assert result.ok is False
    assert "introuvable" in result.message


def test_plan_sans_destination(env) -> None:
    src, _, journal = env
    result = apply_plan(make_plan(src / "x.mkv", None), journal, dry_run=False)
    assert result.ok is False


def test_fichier_deja_a_sa_place(env) -> None:
    """Rescanner sa bibliotheque ne doit pas produire d'operations vides."""
    _, dst, journal = env
    f = dst / "Dune.mkv"
    f.touch()
    result = apply_plan(make_plan(f, f), journal, dry_run=False)
    assert result.ok is True
    assert "deja a sa place" in result.message
    assert journal.read_all() == []


# --- Journal ----------------------------------------------------------------


def test_journal_persiste(env) -> None:
    *_, journal = env
    journal.append(MoveRecord("2026-01-01T00:00:00Z", "p1", "/a", "/b", "rename"))
    journal.append(MoveRecord("2026-01-01T00:01:00Z", "p2", "/c", "/d", "copy"))
    records = journal.read_all()
    assert [r.plan_id for r in records] == ["p1", "p2"]


def test_ligne_tronquee_ignoree(env) -> None:
    """Une coupure en pleine ecriture ne doit pas invalider tout le journal."""
    *_, journal = env
    journal.append(MoveRecord("2026-01-01T00:00:00Z", "p1", "/a", "/b", "rename"))
    with journal._path.open("a", encoding="utf-8") as h:
        h.write('{"timestamp": "incomp')
    assert len(journal.read_all()) == 1


def test_journal_absent_donne_liste_vide(tmp_path: Path) -> None:
    assert Journal(tmp_path / "rien.jsonl").read_all() == []


# --- Annulation -------------------------------------------------------------


def test_annulation_ramene_le_fichier(env) -> None:
    src, dst, journal = env
    f = src / "film.mkv"
    f.write_text("contenu")
    target = dst / "Films" / "Dune.mkv"
    apply_plan(make_plan(f, target), journal, dry_run=False)

    results = undo_last(journal, 1)

    assert results[0].ok is True
    assert f.read_text() == "contenu"
    assert not target.exists()
    assert journal.read_all() == []


def test_annulation_refuse_d_ecraser_l_origine(env) -> None:
    src, dst, journal = env
    f = src / "film.mkv"
    f.write_text("original")
    target = dst / "Dune.mkv"
    apply_plan(make_plan(f, target), journal, dry_run=False)

    # Quelque chose a repris la place entre-temps.
    f.write_text("autre chose")

    results = undo_last(journal, 1)

    assert results[0].ok is False
    assert f.read_text() == "autre chose"
    # L'operation reste au journal : elle est toujours a annuler.
    assert len(journal.read_all()) == 1


def test_annulation_dans_l_ordre_inverse(env) -> None:
    src, dst, journal = env
    for i in (1, 2, 3):
        f = src / f"f{i}.mkv"
        f.write_text(str(i))
        apply_plan(make_plan(f, dst / f"d{i}.mkv"), journal, dry_run=False)

    results = undo_last(journal, 2)

    assert [r.ok for r in results] == [True, True]
    # Les deux dernieres sont revenues, la premiere est restee deplacee.
    assert (src / "f3.mkv").is_file()
    assert (src / "f2.mkv").is_file()
    assert (dst / "d1.mkv").is_file()
    assert len(journal.read_all()) == 1


def test_annulation_sans_journal(env) -> None:
    *_, journal = env
    assert undo_last(journal, 5) == []
