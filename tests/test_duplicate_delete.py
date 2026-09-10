"""Supprimer un doublon sans passer par la corbeille.

La corbeille reste le geste par defaut, et c'est le bon quand on ne fait que
soupconner un doublon. Mais quand on cherche de la place, deplacer six cents
gigaoctets vers une corbeille qu'il faudra vider ensuite double le travail sans
rien proteger de plus.

C'est donc la deuxieme operation de l'application qui detruit sans filet. Ce
qui est teste ici n'est pas le cas nominal — supprimer un fichier est trivial —
mais la CONDITION qui rend l'operation acceptable : on ne supprime un
exemplaire que si celui qu'on garde est reellement present sur le disque.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from sortilege.api.collection import (
    DeleteDuplicatesRequest,
    DuplicateGroupIn,
    delete_duplicates,
)
from sortilege.config import get_settings


@pytest.fixture
def bibliotheque(tmp_path: Path, monkeypatch) -> Path:
    racine = tmp_path / "media"
    (racine / "Films").mkdir(parents=True)
    conf = get_settings()
    monkeypatch.setattr(conf, "library_root", racine)
    monkeypatch.setattr("sortilege.api.collection.get_settings", lambda: conf)
    return racine


def poser(racine: Path, nom: str, octets: int = 1000) -> str:
    chemin = racine / "Films" / nom
    chemin.write_bytes(b"x" * octets)
    return f"Films/{nom}"


def demande(keep: str, paths: list[str], *, confirm: bool = True) -> DeleteDuplicatesRequest:
    return DeleteDuplicatesRequest(
        groups=[DuplicateGroupIn(keep=keep, paths=paths)], confirm=confirm
    )


# --- Le cas nominal ---------------------------------------------------------


def test_l_exemplaire_en_trop_est_supprime(bibliotheque: Path) -> None:
    garde = poser(bibliotheque, "Film [2160p].mkv", 5000)
    trop = poser(bibliotheque, "Film [1080p].mkv", 1000)

    sortie = delete_duplicates(demande(garde, [trop]))

    assert sortie["deleted"] == 1
    assert sortie["freed_bytes"] == 1000
    assert not (bibliotheque / trop).exists()
    assert (bibliotheque / garde).is_file(), "l'exemplaire garde ne bouge pas"


# --- Ce qui doit rester vrai ------------------------------------------------


def test_rien_n_est_supprime_si_l_exemplaire_garde_est_absent(bibliotheque: Path) -> None:
    """LA propriete a preserver. Sans cette verification, un bogue d'affichage
    ou un appel malforme effacerait le dernier exemplaire d'une oeuvre."""
    trop = poser(bibliotheque, "Film [1080p].mkv")

    sortie = delete_duplicates(demande("Films/Absent.mkv", [trop]))

    assert sortie["deleted"] == 0
    assert (bibliotheque / trop).is_file(), "le fichier est toujours la"


def test_l_exemplaire_garde_ne_peut_pas_se_supprimer_lui_meme(bibliotheque: Path) -> None:
    garde = poser(bibliotheque, "Film [2160p].mkv")

    sortie = delete_duplicates(demande(garde, [garde]))

    assert sortie["deleted"] == 0
    assert (bibliotheque / garde).is_file()


def test_la_suppression_doit_etre_confirmee(bibliotheque: Path) -> None:
    """Elle ne se rattrape pas : elle ne doit pas pouvoir arriver par un champ
    oublie."""
    garde = poser(bibliotheque, "Film [2160p].mkv")
    trop = poser(bibliotheque, "Film [1080p].mkv")

    with pytest.raises(HTTPException):
        delete_duplicates(demande(garde, [trop], confirm=False))

    assert (bibliotheque / trop).is_file()


@pytest.mark.parametrize(
    "hors",
    ["/etc/passwd", "../../etc/passwd", "Films/../../dehors.mkv"],
)
def test_un_chemin_hors_bibliotheque_est_refuse(bibliotheque: Path, hors: str) -> None:
    """Le chemin vient du client. Une suppression pilotee par un chemin non
    verifie serait une porte ouverte sur tout le systeme de fichiers."""
    garde = poser(bibliotheque, "Film [2160p].mkv")

    sortie = delete_duplicates(demande(garde, [hors]))

    assert sortie["deleted"] == 0
    assert sortie["failed"] == 1


def test_un_fichier_deja_disparu_ne_fait_pas_echouer_le_reste(bibliotheque: Path) -> None:
    garde = poser(bibliotheque, "Film [2160p].mkv")
    present = poser(bibliotheque, "Film [1080p].mkv")

    sortie = delete_duplicates(demande(garde, ["Films/Parti.mkv", present]))

    assert sortie["deleted"] == 1
    assert sortie["failed"] == 1
    assert not (bibliotheque / present).exists()
