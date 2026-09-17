"""Les refus du nettoyage des annexes, regroupes par cause.

Le cas reel : un seul dossier de vignettes Jellyfin appartenant a root bloquait
3 887 fichiers, et l'ecran repetait 3 887 fois le meme conseil de quatre
lignes. La cause etait unique ; elle doit s'afficher une fois, avec le nombre
de fichiers qu'elle concerne et quelques exemples pour aller verifier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.api import library


@pytest.fixture
def media(tmp_path: Path) -> Path:
    racine = tmp_path / "media"
    (racine / "Films").mkdir(parents=True)
    return racine


def affiche(racine: Path, nom: str) -> Path:
    chemin = racine / "Films" / nom
    chemin.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 64)
    return chemin


def test_un_seul_dossier_refuse_produit_une_seule_cause(
    media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for numero in range(5):
        affiche(media, f"vignette-{numero}.jpg")

    def refus(self: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", refus)
    sortie = library._prune_extras(media, {"images"}, "delete")

    causes = sortie["causes"]
    assert isinstance(causes, list) and len(causes) == 1, "un dossier, une cause"
    assert causes[0]["count"] == 5
    assert "Permission denied" in causes[0]["message"]
    assert "DOSSIER" in causes[0]["message"], "le conseil est donne une fois"
    assert sortie["failed_count"] == 5


def test_les_exemples_sont_bornes_a_trois(media: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le nombre dit l'ampleur, les exemples disent ou regarder. Vingt chemins
    ne diraient pas plus que trois, et rallongeraient le mur qu'on vient de
    supprimer."""
    for numero in range(9):
        affiche(media, f"vignette-{numero}.jpg")

    def refus(self: Path) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "unlink", refus)
    causes = library._prune_extras(media, {"images"}, "delete")["causes"]

    assert causes[0]["count"] == 9
    exemples = causes[0]["examples"]
    assert len(exemples) == 3
    assert all(e.startswith("Films/") for e in exemples)


def test_deux_causes_differentes_ne_se_confondent_pas(
    media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    refusee = affiche(media, "refusee.jpg")
    for numero in range(2):
        affiche(media, f"pleine-{numero}.jpg")

    vrai_unlink = Path.unlink

    def selon_le_fichier(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "refusee.jpg":
            raise PermissionError(13, "Permission denied")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "unlink", selon_le_fichier)
    causes = library._prune_extras(media, {"images"}, "delete")["causes"]

    assert [c["count"] for c in causes] == [2, 1], "la cause la plus repandue d'abord"
    assert {c["count"] for c in causes} == {1, 2}
    assert any("No space left" in str(c["message"]) for c in causes)
    assert any("Permission denied" in str(c["message"]) for c in causes)
    assert refusee.is_file()
    assert vrai_unlink is not None


def test_sans_echec_la_liste_des_causes_est_vide(media: Path) -> None:
    affiche(media, "vignette.jpg")

    sortie = library._prune_extras(media, {"images"}, "delete")

    assert sortie["removed"] == 1
    assert sortie["causes"] == []
    assert sortie["failed_count"] == 0
