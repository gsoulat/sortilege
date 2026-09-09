"""Annulation ciblee : une oeuvre, pas une session entiere.

« Tout annuler (703) » etait la seule option. Or le besoin reel est l'inverse :
une serie mal identifiee au milieu de sept cents deplacements corrects. Defaire
les sept cents pour corriger douze fichiers n'est pas une annulation, c'est une
punition.

Deux proprietes decident si c'est utilisable :

1. **Ce qui n'etait pas vise ne bouge pas.** Une annulation qui deborde sur les
   voisins est pire que pas d'annulation du tout : elle deplace des fichiers
   dont on avait valide le rangement.
2. **Le journal reste coherent apres coup.** Les operations annulees en
   quittent, les autres y restent dans l'ordre — sinon la prochaine annulation
   defait n'importe quoi.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.journal import (
    Journal,
    MoveRecord,
    group_by_work,
    undo_last,
    undo_plans,
    undo_work,
)


@pytest.fixture
def library(tmp_path: Path) -> Path:
    (tmp_path / "dl").mkdir()
    (tmp_path / "lib").mkdir()
    return tmp_path


def ranged(library: Path, record: MoveRecord) -> MoveRecord:
    """Cree sur le disque l'etat que decrit une entree de journal."""
    destination = Path(record.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("video", encoding="utf-8")
    return record


def entry(
    library: Path,
    *,
    plan_id: str,
    title: str,
    relative: str,
    kind: str = "video",
    at: str = "2026-09-01T10:00:00",
    work_kind: str = "episode",
) -> MoveRecord:
    return MoveRecord(
        timestamp=at,
        plan_id=plan_id,
        source=str(library / "dl" / Path(relative).name),
        destination=str(library / "lib" / relative),
        method="rename",
        kind=kind,
        title=title,
        work_kind=work_kind,
    )


@pytest.fixture
def journal(library: Path) -> Journal:
    """Deux series et un film ranges, tous presents sur le disque."""
    j = Journal(library / "journal.jsonl")
    for record in [
        entry(library, plan_id="s1", title="Severance", relative="Severance/S01E01.mkv"),
        entry(library, plan_id="s2", title="Severance", relative="Severance/S01E02.mkv"),
        entry(
            library,
            plan_id="s2",
            title="Severance",
            relative="Severance/S01E02.srt",
            kind="companion",
        ),
        entry(library, plan_id="d1", title="Dark Matter", relative="Dark Matter/S01E01.mkv"),
        entry(
            library,
            plan_id="m1",
            title="Dune",
            relative="Dune/Dune.mkv",
            work_kind="movie",
            at="2026-09-02T10:00:00",
        ),
    ]:
        j.append(ranged(library, record))
    return j


# --- Regroupement -----------------------------------------------------------


def test_les_operations_sont_groupees_par_oeuvre(journal: Journal) -> None:
    groups = {g["title"]: g for g in group_by_work(journal.read_all())}
    assert set(groups) == {"Severance", "Dark Matter", "Dune"}
    assert groups["Severance"]["files"] == 2
    assert groups["Severance"]["companions"] == 1


def test_l_oeuvre_la_plus_recente_vient_en_tete(journal: Journal) -> None:
    """On annule presque toujours ce qu'on vient de faire."""
    assert group_by_work(journal.read_all())[0]["title"] == "Dune"


def test_le_type_d_oeuvre_est_conserve(journal: Journal) -> None:
    groups = {g["title"]: g for g in group_by_work(journal.read_all())}
    assert groups["Dune"]["work_kind"] == "movie"


def test_une_entree_sans_titre_retombe_sur_le_dossier(library: Path) -> None:
    """Les deplacements anterieurs a l'ajout du titre doivent rester
    annulables : sinon la nouveaute rendrait l'ancien journal inutilisable."""
    j = Journal(library / "j.jsonl")
    j.append(
        MoveRecord(
            timestamp="2026-01-01T00:00:00",
            plan_id="vieux",
            source=str(library / "dl" / "x.mkv"),
            destination=str(library / "lib" / "Severance (2022)" / "Season 01" / "e1.mkv"),
            method="rename",
            kind="video",
        )
    )
    assert group_by_work(j.read_all())[0]["title"] == "Severance (2022)"


# --- Annulation d'une oeuvre ------------------------------------------------


def test_annuler_une_serie_la_ramene_entierement(journal: Journal, library: Path) -> None:
    results = undo_work(journal, "Severance")

    assert len(results) == 3
    assert all(r.ok for r in results)
    assert (library / "dl" / "S01E01.mkv").is_file()
    assert (library / "dl" / "S01E02.srt").is_file(), "le sous-titre doit revenir aussi"


def test_ce_qui_n_etait_pas_vise_ne_bouge_pas(journal: Journal, library: Path) -> None:
    """La propriete qui rend l'annulation ciblee utilisable."""
    undo_work(journal, "Severance")

    assert (library / "lib" / "Dark Matter" / "S01E01.mkv").is_file()
    assert (library / "lib" / "Dune" / "Dune.mkv").is_file()
    # Et rien de ces deux oeuvres n'est reparti vers la source.
    assert not (library / "dl" / "Dune.mkv").exists()


def test_le_journal_ne_garde_que_le_reste(journal: Journal) -> None:
    undo_work(journal, "Severance")
    titres = {r.title for r in journal.read_all()}
    assert titres == {"Dark Matter", "Dune"}


def test_le_journal_reste_dans_l_ordre(journal: Journal) -> None:
    """Une annulation au MILIEU du journal ne doit pas desordonner le reste :
    « annuler les N dernieres » deferait sinon autre chose que les dernieres."""
    undo_work(journal, "Dark Matter")
    stamps = [r.timestamp for r in journal.read_all()]
    assert stamps == sorted(stamps)


def test_annuler_une_oeuvre_inconnue_ne_fait_rien(journal: Journal) -> None:
    assert undo_work(journal, "Jamais Rangé") == []
    assert len(journal.read_all()) == 5


# --- Annulation de fichiers precis ------------------------------------------


def test_annuler_un_seul_episode(journal: Journal, library: Path) -> None:
    results = undo_plans(journal, ["s1"])

    assert len(results) == 1
    assert (library / "dl" / "S01E01.mkv").is_file()
    assert (library / "lib" / "Severance" / "S01E02.mkv").is_file(), "l'autre episode reste"


def test_annuler_un_episode_ramene_son_sous_titre(journal: Journal, library: Path) -> None:
    """Un retour arriere a moitie fait laisserait la video sans ses
    compagnons — exactement la perte qu'on cherche a eviter."""
    undo_plans(journal, ["s2"])
    assert (library / "dl" / "S01E02.mkv").is_file()
    assert (library / "dl" / "S01E02.srt").is_file()


def test_une_liste_vide_ne_fait_rien(journal: Journal) -> None:
    assert undo_plans(journal, []) == []
    assert len(journal.read_all()) == 5


# --- Non-regression de l'annulation globale ---------------------------------


def test_annuler_les_dernieres_operations_marche_toujours(journal: Journal, library: Path) -> None:
    results = undo_last(journal, 1)
    assert len(results) == 1
    assert (library / "dl" / "Dune.mkv").is_file()
    assert len(journal.read_all()) == 4


def test_une_annulation_qui_echoue_reste_rejouable(journal: Journal, library: Path) -> None:
    """L'origine est reoccupee : le retour doit echouer sans rien ecraser, et
    l'operation doit rester au journal pour un nouvel essai."""
    (library / "dl" / "S01E01.mkv").write_text("un autre fichier", encoding="utf-8")

    results = undo_plans(journal, ["s1"])

    assert results[0].ok is False
    assert (library / "dl" / "S01E01.mkv").read_text(encoding="utf-8") == "un autre fichier"
    assert any(r.plan_id == "s1" for r in journal.read_all())
