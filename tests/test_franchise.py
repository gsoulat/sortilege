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


# --- Le disque comme voisinage ----------------------------------------------
#
# Cas reel, et le plus couteux qu'on ait vu : « Star Trek: Discovery »
# telecharge SEUL partait dans « Series/Star Trek Discovery (2017) », et
# telecharge le meme jour que « Star Trek: Picard » dans « Series/Star Trek/
# Star Trek Discovery (2017) ». Deux destinations pour une meme serie selon ce
# qui l'accompagnait : la bibliotheque finissait coupee en deux, et un serveur
# multimedia y voyait deux series aux saisons incompletes.
#
# Les voisins lus sur le disque suppriment cette dependance au lot. Ils ont
# perdu leur deux-points en devenant des noms de dossier, d'ou les cas
# ci-dessous.


def test_un_dossier_de_franchise_existant_suffit() -> None:
    """Une fois « Star Trek » sur le disque, toute serie de la franchise l'y
    rejoint — quel que soit le contenu du lot en cours."""
    sur_le_disque = ["Star Trek", "Star Trek Discovery", "Severance"]

    assert franchise_for("Star Trek: Picard", sur_le_disque) == "Star Trek"


def test_un_voisin_sans_deux_points_compte_quand_meme() -> None:
    """« Star Trek: Discovery » figure sur le disque sous « Star Trek
    Discovery » : plus rien ne s'en derive, mais le prefixe reste lisible."""
    assert franchise_for("Star Trek", ["Star Trek", "Star Trek Voyager"]) == "Star Trek"


def test_une_serie_seule_ne_cree_pas_de_dossier() -> None:
    """Un dossier de franchise a un seul element n'est pas un regroupement,
    c'est un niveau de plus a traverser."""
    assert franchise_for("Star Trek", ["Star Trek"]) is None
    assert franchise_for("Severance", ["Severance", "Star Trek"]) is None


def test_le_prefixe_exige_une_suite() -> None:
    """Sans cette borne, « Star Trek » se declarerait sa propre franchise en se
    voyant lui-meme dans la liste."""
    assert franchise_for("Star Trek", ["Star Trek", "Star Trek "]) is None
