"""Arbitrer entre deux encodages d'une meme oeuvre, par la taille.

Le cas reel : trois cents fichiers deja ranges, dont la copie en telechargement
n'a pas la meme taille — 6,06 Go contre 1,87 Go. Ce sont deux encodages
distincts, un remux et une version legere, et aucun signal automatique ne dit
lequel garder. La taille, elle, est un critere que l'utilisateur peut choisir :
la place, ou la qualite.

C'est l'operation la plus lourde de cette application : elle REMPLACE un
fichier de bibliotheque. Ces tests portent donc moins sur le cas nominal que
sur ce qui doit rester vrai quand elle tourne mal — car se tromper sur trois
cents fichiers d'un coup ne se rattrape pas a la main.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.journal import Journal, keep_by_size, undo_last
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision


@pytest.fixture
def espace(tmp_path: Path) -> Path:
    (tmp_path / "dl").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / ".corbeille").mkdir()
    return tmp_path


@pytest.fixture
def journal(espace: Path) -> Journal:
    return Journal(espace / "journal.jsonl")


def paire(espace: Path, *, copie: int, rangee: int) -> Plan:
    """Deux encodages du meme film : l'un en telechargement, l'autre range."""
    source = espace / "dl" / "Film.mkv"
    source.write_bytes(b"C" * copie)

    destination = espace / "lib" / "Films" / "Film (2024).mkv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"R" * rangee)

    return Plan(
        id="p1",
        source=source,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        title="Film",
        year=2024,
    )


def corbeille(espace: Path) -> Path:
    return espace / "lib" / ".corbeille"


# --- Garder le plus petit ---------------------------------------------------


def test_la_copie_plus_petite_remplace_le_fichier_range(espace, journal) -> None:
    plan = paire(espace, copie=1000, rangee=5000)

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is True
    assert plan.destination.read_bytes().startswith(b"C"), "la copie occupe la place"
    assert not plan.source.exists()


def test_la_copie_plus_grosse_est_simplement_evacuee(espace, journal) -> None:
    """Le fichier range est deja le plus petit : il ne bouge pas."""
    plan = paire(espace, copie=5000, rangee=1000)

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is True
    assert plan.destination.read_bytes().startswith(b"R"), "le fichier range est intact"
    assert not plan.source.exists()


# --- Garder le plus gros ----------------------------------------------------


def test_le_critere_inverse_fonctionne(espace, journal) -> None:
    """« Le plus petit » n'est pas toujours le bon choix — c'est meme le
    contraire pour qui tient a l'image."""
    plan = paire(espace, copie=5000, rangee=1000)

    keep_by_size(plan, journal, corbeille(espace), keep="larger")

    assert plan.destination.read_bytes().startswith(b"C")


# --- Ce qui doit rester vrai ------------------------------------------------


def test_le_fichier_ecarte_part_en_corbeille(espace, journal) -> None:
    """LA propriete a preserver. Se tromper de critere sur trois cents fichiers
    serait irrattrapable si l'ecarte etait supprime."""
    plan = paire(espace, copie=1000, rangee=5000)

    keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    en_corbeille = list(corbeille(espace).rglob("*.mkv"))
    assert len(en_corbeille) == 1
    assert en_corbeille[0].read_bytes().startswith(b"R"), "l'ancien fichier est recuperable"


def test_le_remplacement_est_annulable(espace, journal) -> None:
    """Remplacer un fichier de bibliotheque doit pouvoir se defaire : les deux
    mouvements sont journalises separement."""
    plan = paire(espace, copie=1000, rangee=5000)
    keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    resultats = undo_last(journal, 2)

    assert all(r.ok for r in resultats)
    assert plan.source.is_file(), "la copie est revenue en telechargement"
    assert plan.destination.read_bytes().startswith(b"R"), "le fichier range est revenu"


def test_deux_tailles_egales_ne_sont_pas_departagees(espace, journal) -> None:
    """Sans ecart, le critere ne dit rien : agir au hasard serait pire que de
    ne rien faire."""
    plan = paire(espace, copie=2000, rangee=2000)

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is False
    assert plan.source.is_file()
    assert plan.destination.is_file()


def test_un_fichier_disparu_arrete_tout(espace, journal) -> None:
    plan = paire(espace, copie=1000, rangee=5000)
    plan.source.unlink()

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is False
    assert plan.destination.is_file(), "la bibliotheque n'a pas ete touchee"


def test_sans_corbeille_le_fichier_range_n_est_pas_ecarte(espace, journal) -> None:
    """Mieux vaut ne rien faire que d'ecarter sans filet."""
    plan = paire(espace, copie=1000, rangee=5000)

    resultat = keep_by_size(plan, journal, None, keep="smaller")

    assert resultat.ok is False
    assert plan.destination.read_bytes().startswith(b"R")


def test_un_echec_a_mi_chemin_restaure_la_bibliotheque(espace, journal, monkeypatch) -> None:
    """Le pire scenario : le fichier range est deja ecarte quand le
    remplacement echoue. Laisser la bibliotheque SANS le fichier serait pire
    que de n'avoir rien tente."""
    from sortilege.core import journal as module

    plan = paire(espace, copie=1000, rangee=5000)
    vrai_move = module._move
    appels = {"n": 0}

    def move_capricieux(source, destination):
        appels["n"] += 1
        if appels["n"] == 2:  # le deplacement de la copie vers la destination
            raise PermissionError(13, "Permission denied")
        return vrai_move(source, destination)

    monkeypatch.setattr(module, "_move", move_capricieux)
    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is False
    assert plan.destination.is_file(), "le fichier range a ete remis en place"
    assert plan.destination.read_bytes().startswith(b"R")
