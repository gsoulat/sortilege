"""Strategies de qualite : quel exemplaire garder quand il y en a plusieurs.

La regle etait codee en dur — la plus haute resolution gagne, puis le plus gros
fichier. C'etait une preference deguisee en regle, et elle ne convient pas a
tout le monde : un remux 4K de soixante gigaoctets n'a pas la meme valeur pour
qui archive un film et pour qui garde deux cents episodes sur un NAS.

Ce qui est teste ici tient en une phrase : **une strategie doit faire ce que
son libelle annonce**. « Économie de place » qui garderait la 4K serait pire
qu'une absence de reglage, parce qu'on lui ferait confiance.
"""

from __future__ import annotations

import pytest

from sortilege.core.quality import (
    BY_KEY,
    STRATEGIES,
    QualitySettings,
    compare,
    normalise,
    strategy,
)

GB = 1024**3


# --- Reconnaitre une resolution ecrite n'importe comment --------------------


@pytest.mark.parametrize(
    ("brut", "attendu"),
    [
        ("4K", "2160p"),
        ("UHD", "2160p"),
        ("2160p", "2160p"),
        ("1080i", "1080p"),
        ("1080p", "1080p"),
        ("720P", "720p"),
    ],
)
def test_les_ecritures_equivalentes_se_rejoignent(brut, attendu) -> None:
    """« 4K » et « 2160p » designent la meme chose. Les traiter comme deux
    valeurs ferait echouer toute comparaison, et l'utilisateur ne comprendrait
    pas pourquoi sa 4K n'est pas reconnue."""
    assert normalise(brut) == attendu


def test_une_hauteur_mesuree_est_rabattue_sur_un_palier() -> None:
    """ffprobe rend une hauteur reelle, pas un palier : 1088 doit valoir
    1080p, sans quoi un fichier mesure ne vaudrait rien."""
    assert normalise("1088") == "1080p"


def test_une_resolution_inconnue_ne_vaut_rien() -> None:
    assert normalise("") == ""
    assert normalise("n'importe quoi") == ""


# --- Chaque strategie fait ce qu'elle annonce -------------------------------


def test_qualite_maximale_prefere_la_4k() -> None:
    verdict = compare(
        strategy("quality"),
        candidate_resolution="2160p",
        candidate_size=60 * GB,
        incumbent_resolution="1080p",
        incumbent_size=8 * GB,
    )

    assert verdict.keep_candidate is True


def test_economie_de_place_ecarte_la_4k() -> None:
    """C'est le test qui compte : une strategie qui garderait la 4K sous ce
    libelle serait pire qu'un reglage absent, parce qu'on lui ferait
    confiance."""
    verdict = compare(
        strategy("compact"),
        candidate_resolution="2160p",
        candidate_size=60 * GB,
        incumbent_resolution="720p",
        incumbent_size=2 * GB,
    )

    assert verdict.keep_candidate is False


def test_equilibre_prefere_le_1080p_a_la_4k() -> None:
    """La 4K triple le poids pour un gain que peu d'ecrans restituent."""
    verdict = compare(
        strategy("balanced"),
        candidate_resolution="2160p",
        candidate_size=60 * GB,
        incumbent_resolution="1080p",
        incumbent_size=8 * GB,
    )

    assert verdict.keep_candidate is False


def test_equilibre_prefere_le_1080p_au_720p() -> None:
    verdict = compare(
        strategy("balanced"),
        candidate_resolution="1080p",
        candidate_size=8 * GB,
        incumbent_resolution="720p",
        incumbent_size=2 * GB,
    )

    assert verdict.keep_candidate is True


# --- A resolution egale -----------------------------------------------------


def test_a_resolution_egale_le_plus_gros_gagne_en_qualite() -> None:
    verdict = compare(
        strategy("quality"),
        candidate_resolution="1080p",
        candidate_size=12 * GB,
        incumbent_resolution="1080p",
        incumbent_size=4 * GB,
    )

    assert verdict.keep_candidate is True


def test_a_resolution_egale_le_plus_petit_gagne_en_economie() -> None:
    verdict = compare(
        strategy("compact"),
        candidate_resolution="1080p",
        candidate_size=2 * GB,
        incumbent_resolution="1080p",
        incumbent_size=12 * GB,
    )

    assert verdict.keep_candidate is True


def test_deux_exemplaires_identiques_ne_sont_pas_departages() -> None:
    verdict = compare(
        strategy("quality"),
        candidate_resolution="1080p",
        candidate_size=8 * GB,
        incumbent_resolution="1080p",
        incumbent_size=8 * GB,
    )

    assert verdict.keep_candidate is False
    assert "valent" in verdict.reason


# --- Ce qu'on refuse de faire -----------------------------------------------


def test_une_resolution_inconnue_ne_remplace_pas_une_connue() -> None:
    """On ne remplace pas une certitude par une inconnue, meme modeste."""
    verdict = compare(
        strategy("quality"),
        candidate_resolution=None,
        candidate_size=50 * GB,
        incumbent_resolution="480p",
        incumbent_size=1 * GB,
    )

    assert verdict.keep_candidate is False


def test_le_motif_nomme_la_strategie() -> None:
    """Remplacer un fichier sans dire ce qui l'a emporte laisse l'utilisateur
    devant un resultat qu'il ne peut ni verifier ni contester."""
    verdict = compare(
        strategy("compact"),
        candidate_resolution="720p",
        candidate_size=2 * GB,
        incumbent_resolution="2160p",
        incumbent_size=60 * GB,
    )

    assert "Économie de place" in verdict.reason
    assert "720p" in verdict.reason


# --- Reglage par type -------------------------------------------------------


def test_chaque_type_a_sa_strategie() -> None:
    """C'est le besoin reel : la meilleure image pour les films, la plus petite
    empreinte pour les series."""
    reglage = QualitySettings(movie="quality", episode="compact", anime="balanced")

    assert reglage.for_kind("movie").key == "quality"
    assert reglage.for_kind("episode").key == "compact"
    assert reglage.for_kind("anime").key == "balanced"


def test_un_type_inconnu_retombe_sur_le_defaut() -> None:
    assert QualitySettings().for_kind("musique").key in BY_KEY


def test_une_strategie_inconnue_ne_leve_pas() -> None:
    """Un reglage devenu invalide ne doit pas empecher un rangement."""
    assert strategy("inexistante").key in BY_KEY
    assert strategy(None).key in BY_KEY


def test_chaque_strategie_couvre_toute_l_echelle() -> None:
    """Une resolution oubliee dans un ordre arriverait derniere sans raison,
    et le comportement serait inexplicable."""
    for strat in STRATEGIES:
        assert set(strat.order) == {"2160p", "1080p", "720p", "576p", "480p"}, strat.key
