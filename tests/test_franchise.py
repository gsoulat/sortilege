"""Tests du regroupement par franchise."""

import pytest

from sortilege.core.franchise import derive_franchise, franchise_for


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Star Trek: Discovery", "Star Trek"),
        ("Star Trek: Strange New Worlds", "Star Trek"),
        ("Star Trek : Picard", "Star Trek"),
        ("The Witcher: Blood Origin", "The Witcher"),
        ("American Horror Story: Coven", "American Horror Story"),
        ("Doctor Who - Confidential", "Doctor Who"),
    ],
)
def test_franchise_deduite(title: str, expected: str) -> None:
    assert derive_franchise(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Severance",
        "Breaking Bad",
        "Kaamelott",
        "",
        "Star Trek",  # la serie d'origine ne porte pas de sous-titre
    ],
)
def test_pas_de_franchise_sans_sous_titre(title: str) -> None:
    assert derive_franchise(title) is None


@pytest.mark.parametrize("title", ["The: Thing", "Le : Truc", "New: Girl"])
def test_prefixe_generique_refuse(title: str) -> None:
    """« The » ou « New » regrouperaient des series sans aucun rapport."""
    assert derive_franchise(title) is None


def test_sous_titre_vide_ignore() -> None:
    assert derive_franchise("Quelque Chose: ") is None


# --- Corroboration par la bibliotheque --------------------------------------


def test_franchise_confirmee_par_une_soeur() -> None:
    siblings = ["Star Trek: Discovery", "Star Trek: Picard"]
    assert franchise_for("Star Trek: Discovery", siblings) == "Star Trek"


def test_franchise_confirmee_par_la_serie_d_origine() -> None:
    """« Star Trek » tout court confirme la franchise de ses derives."""
    siblings = ["Star Trek: Discovery", "Star Trek"]
    assert franchise_for("Star Trek: Discovery", siblings) == "Star Trek"


def test_franchise_seule_rejetee() -> None:
    """Un dossier de franchise a un seul element n'apporte rien."""
    siblings = ["The Witcher: Blood Origin", "Severance", "Kaamelott"]
    assert franchise_for("The Witcher: Blood Origin", siblings) is None


def test_sans_bibliotheque_on_garde_la_deduction() -> None:
    assert franchise_for("Star Trek: Discovery", None) == "Star Trek"
