"""Tests des fichiers compagnons et du nettoyage.

Le test le plus important est celui des sous-titres : les oublier est une perte
de donnees silencieuse, que personne ne remarque avant de lancer la lecture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.companions import (
    CompanionKind,
    directory_now_empty,
    find_companions,
    find_leftovers,
    trash_destination,
)
from sortilege.core.journal import Journal, apply_plan, undo_last
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """Un dossier de release realiste."""
    folder = tmp_path / "dl" / "Dune.2024.1080p-GROUPE"
    folder.mkdir(parents=True)
    (folder / "Dune.2024.1080p-GROUPE.mkv").write_text("video")
    (folder / "Dune.2024.1080p-GROUPE.fr.srt").write_text("sous-titres fr")
    (folder / "Dune.2024.1080p-GROUPE.en.srt").write_text("sous-titres en")
    (folder / "RARBG.txt").write_text("pub")
    (folder / "poster.jpg").write_text("image")
    return folder


def video_of(folder: Path) -> Path:
    return folder / "Dune.2024.1080p-GROUPE.mkv"


# --- Detection --------------------------------------------------------------


def test_les_sous_titres_sont_trouves(release: Path) -> None:
    companions = find_companions(video_of(release))
    subs = [c for c in companions if c.kind is CompanionKind.SUBTITLE]
    assert len(subs) == 2
    assert {c.suffix for c in subs} == {".fr.srt", ".en.srt"}


def test_le_suffixe_de_langue_est_preserve(release: Path) -> None:
    """Sans lui, les deux pistes se recouvriraient sous le meme nom."""
    companions = find_companions(video_of(release))
    destination = Path("/media/Films/Dune (2024)/Dune (2024).mkv")
    noms = {c.destination_for(destination).name for c in companions}
    assert "Dune (2024).fr.srt" in noms
    assert "Dune (2024).en.srt" in noms


def test_la_jaquette_canonique_est_emportee(release: Path) -> None:
    companions = find_companions(video_of(release))
    assert any(c.kind is CompanionKind.ARTWORK for c in companions)


def test_jaquette_ignoree_si_plusieurs_videos(tmp_path: Path) -> None:
    """« poster.jpg » n'appartient a personne quand le dossier a deux videos."""
    folder = tmp_path / "saison"
    folder.mkdir()
    (folder / "S01E01.mkv").write_text("a")
    (folder / "S01E02.mkv").write_text("b")
    (folder / "poster.jpg").write_text("image")

    companions = find_companions(folder / "S01E01.mkv")
    assert companions == []


def test_les_jaquettes_peuvent_etre_refusees(release: Path) -> None:
    companions = find_companions(video_of(release), artwork=False)
    assert all(c.kind is CompanionKind.SUBTITLE for c in companions)


def test_un_sous_titre_d_une_autre_video_n_est_pas_pris(tmp_path: Path) -> None:
    folder = tmp_path / "melange"
    folder.mkdir()
    (folder / "Film.A.mkv").write_text("a")
    (folder / "Film.B.fr.srt").write_text("appartient a B")

    assert find_companions(folder / "Film.A.mkv") == []


# --- Restes -----------------------------------------------------------------


def test_les_dechets_connus_sont_reperes(release: Path) -> None:
    video = video_of(release)
    leftovers = find_leftovers(video, find_companions(video))
    assert [p.name for p in leftovers] == ["RARBG.txt"]


def test_un_fichier_inconnu_est_laisse_en_place(tmp_path: Path) -> None:
    """La liste des dechets est conservatrice a dessein."""
    folder = tmp_path / "x"
    folder.mkdir()
    video = folder / "film.mkv"
    video.write_text("v")
    (folder / "notes-importantes.odt").write_text("precieux")

    assert find_leftovers(video, []) == []


def test_un_compagnon_n_est_jamais_un_reste(release: Path) -> None:
    video = video_of(release)
    companions = find_companions(video)
    leftovers = find_leftovers(video, companions)
    assert not {p for p in leftovers} & {c.path for c in companions}


def test_chemin_de_corbeille_aplati() -> None:
    target = trash_destination(Path("/media/.corbeille"), "2026-09-08", Path("/dl/rel/RARBG.txt"))
    assert target.parent.name == "2026-09-08"
    assert "dl_rel_RARBG.txt" in target.name


def test_dossier_vide_detecte(tmp_path: Path) -> None:
    vide = tmp_path / "vide"
    vide.mkdir()
    (vide / ".DS_Store").write_text("x")
    assert directory_now_empty(vide) is True

    plein = tmp_path / "plein"
    plein.mkdir()
    (plein / "film.mkv").write_text("x")
    assert directory_now_empty(plein) is False


# --- Application de bout en bout --------------------------------------------


def make_plan(video: Path, destination: Path, companions, leftovers) -> Plan:
    return Plan(
        id="p1",
        source=video,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        companions=companions,
        leftovers=leftovers,
    )


def test_les_sous_titres_suivent_la_video(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    journal = Journal(tmp_path / "j.jsonl")
    result = apply_plan(plan, journal, dry_run=False, trash_root=lib / ".corbeille")

    assert result.ok
    assert (lib / "Films" / "Dune (2024)" / "Dune (2024).fr.srt").read_text() == "sous-titres fr"
    assert (lib / "Films" / "Dune (2024)" / "Dune (2024).en.srt").read_text() == "sous-titres en"


def test_les_restes_vont_en_corbeille_sans_etre_supprimes(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    trash = lib / ".corbeille"
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    apply_plan(plan, Journal(tmp_path / "j.jsonl"), dry_run=False, trash_root=trash)

    survivants = list(trash.rglob("*RARBG.txt"))
    assert len(survivants) == 1
    assert survivants[0].read_text() == "pub"


def test_le_dossier_vide_disparait(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    apply_plan(
        plan,
        Journal(tmp_path / "j.jsonl"),
        dry_run=False,
        trash_root=lib / ".corbeille",
        source_roots=[tmp_path],
    )

    assert not release.exists()


def test_une_racine_source_n_est_jamais_supprimee(tmp_path: Path) -> None:
    """Un fichier pose directement dans une source : la vider ne doit pas la
    faire disparaitre. Sans cette borne, le scan suivant echouerait sur un
    dossier absent — et l'ancien code n'en avait aucune."""
    from sortilege.core.journal import prune_empty_dirs

    source = tmp_path / "Download"
    source.mkdir()

    assert prune_empty_dirs(source, [source]) == 0
    assert source.is_dir()


def test_le_nettoyage_remonte_plusieurs_niveaux(tmp_path: Path) -> None:
    """Une release occupe souvent deux niveaux ; ne remonter que d'un cran
    laissait une carcasse par episode."""
    from sortilege.core.journal import prune_empty_dirs

    source = tmp_path / "Download"
    profond = source / "Serie" / "Serie S01E01 GROUPE"
    profond.mkdir(parents=True)

    assert prune_empty_dirs(profond, [source]) == 2
    assert not (source / "Serie").exists()
    assert source.is_dir(), "la source elle-meme est intacte"


def test_un_dossier_non_vide_arrete_la_remontee(tmp_path: Path) -> None:
    from sortilege.core.journal import prune_empty_dirs

    source = tmp_path / "Download"
    profond = source / "Serie" / "Serie S01E01"
    profond.mkdir(parents=True)
    (source / "Serie" / "S01E02.mkv").write_text("autre episode", encoding="utf-8")

    assert prune_empty_dirs(profond, [source]) == 1
    assert (source / "Serie").is_dir()


def test_l_annulation_ramene_aussi_les_compagnons(tmp_path: Path, release: Path) -> None:
    """Une annulation partielle serait pire que pas d'annulation du tout."""
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    journal = Journal(tmp_path / "j.jsonl")
    apply_plan(plan, journal, dry_run=False, trash_root=lib / ".corbeille")

    total = len(journal.read_all())
    undo_last(journal, total)

    assert video.is_file()
    assert (release / "Dune.2024.1080p-GROUPE.fr.srt").read_text() == "sous-titres fr"
    assert (release / "RARBG.txt").read_text() == "pub"


def test_la_simulation_ne_touche_a_aucun_compagnon(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    apply_plan(plan, Journal(tmp_path / "j.jsonl"), dry_run=True, trash_root=lib / ".corbeille")

    assert (release / "Dune.2024.1080p-GROUPE.fr.srt").is_file()
    assert (release / "RARBG.txt").is_file()
    assert release.is_dir()


def test_sans_corbeille_configuree_rien_n_est_evacue(tmp_path: Path, release: Path) -> None:
    """Mieux vaut laisser du desordre que d'inventer une destination."""
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Dune.mkv"

    plan = make_plan(video, destination, [], [release / "RARBG.txt"])
    apply_plan(plan, Journal(tmp_path / "j.jsonl"), dry_run=False, trash_root=None)

    assert (release / "RARBG.txt").is_file()
