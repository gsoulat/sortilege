"""Tests du garde-fou de chemin avant mise en corbeille.

Ce controle decide si un chemin envoye par le NAVIGATEUR peut designer un
fichier a deplacer. C'etait le seul controle de securite du projet sans test —
et le plus expose, puisque son entree vient entierement du client.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.api.collection import _resolve_in_library


@pytest.fixture
def library(tmp_path: Path) -> Path:
    root = tmp_path / "media"
    (root / "Films").mkdir(parents=True)
    (root / "Films" / "Dune.mkv").touch()
    (tmp_path / "secret.txt").write_text("hors bibliotheque")
    return root


def test_un_chemin_relatif_normal_est_accepte(library: Path) -> None:
    assert _resolve_in_library("Films/Dune.mkv", library) == library / "Films" / "Dune.mkv"


@pytest.mark.parametrize(
    "hostile",
    [
        "../secret.txt",
        "Films/../../secret.txt",
        "./../secret.txt",
        "Films/./../../secret.txt",
    ],
)
def test_toute_remontee_est_refusee(library: Path, hostile: str) -> None:
    """Refus explicite et non neutralisation : ici l'entree vient du client,
    corriger silencieusement masquerait une tentative."""
    with pytest.raises(ValueError):
        _resolve_in_library(hostile, library)


@pytest.mark.parametrize("absolu", ["/etc/passwd", "/media/Films/Dune.mkv", "//tmp/x"])
def test_un_chemin_absolu_est_refuse(library: Path, absolu: str) -> None:
    with pytest.raises(ValueError):
        _resolve_in_library(absolu, library)


def test_les_separateurs_windows_sont_pris_en_compte(library: Path) -> None:
    """« ..\\.. » doit etre vu comme une remontee, pas comme un nom de fichier."""
    with pytest.raises(ValueError):
        _resolve_in_library("Films\\..\\..\\secret.txt", library)


def test_la_racine_elle_meme_est_refusee(library: Path) -> None:
    """Elle n'est pas SOUS la racine : accepter reviendrait a autoriser le
    deplacement du dossier de bibliotheque."""
    with pytest.raises(ValueError):
        _resolve_in_library("", library)


def test_un_fichier_inexistant_passe_le_controle(library: Path) -> None:
    """Le garde-fou juge le CHEMIN, pas l'existence — l'appelant verifie
    ensuite. Les confondre ferait dependre la securite d'un etat du disque."""
    resolved = _resolve_in_library("Films/absent.mkv", library)
    assert library in resolved.parents


def test_un_sous_dossier_profond_reste_accepte(library: Path) -> None:
    deep = "Series/Severance (2022)/Season 02/ep.mkv"
    assert library in _resolve_in_library(deep, library).parents
