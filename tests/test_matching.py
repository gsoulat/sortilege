"""Tests de la mesure des signaux.

Aucun appel reseau : les candidats sont construits a la main, ce qui permet de
tester exactement les situations qui posent probleme en production.
"""

from pathlib import Path

from sortilege.core.matching import (
    best_match,
    build_signals,
    normalize_title,
    title_similarity,
)
from sortilege.core.parser import parse
from sortilege.core.probe import FileProbe
from sortilege.providers.base import Candidate


def cand(**kw) -> Candidate:
    base = dict(provider="tmdb", external_id="1", title="Dune", year=2024, popularity=0.5)
    base.update(kw)
    return Candidate(**base)


# --- Normalisation ----------------------------------------------------------


def test_accents_et_casse_ignores() -> None:
    assert normalize_title("Amélie Poulain") == normalize_title("AMELIE POULAIN")


def test_ponctuation_ignoree() -> None:
    assert normalize_title("Dune: Part Two") == normalize_title("Dune - Part Two")


# --- Similarite -------------------------------------------------------------


def test_titre_identique() -> None:
    assert title_similarity("Dune", cand(title="Dune")) == 1.0


def test_titre_francais_trouve_via_alias() -> None:
    """Le cas qui fait echouer la moitie d'une bibliotheque francaise."""
    c = cand(title="The Hangover", aliases=["Very Bad Trip"])
    assert title_similarity("Very Bad Trip", c) == 1.0


def test_article_de_tete_absent() -> None:
    c = cand(title="Le Fabuleux Destin d'Amélie Poulain")
    assert title_similarity("Fabuleux Destin d Amelie Poulain", c) > 0.9


def test_ordre_des_mots_tolere() -> None:
    """Le recouvrement de mots rattrape ce que l'ordre fait perdre."""
    assert title_similarity("Part Two Dune", cand(title="Dune Part Two")) > 0.9


def test_titre_sans_rapport() -> None:
    assert title_similarity("Severance", cand(title="Dune")) < 0.4


# --- Identifiant declare ----------------------------------------------------


def test_identifiant_concordant() -> None:
    probe = FileProbe(tmdb_id="693134")
    s = build_signals(parse(Path("Dune.2024.mkv")), probe, cand(external_id="693134"), [])
    assert s.external_id_match is True


def test_identifiant_divergent() -> None:
    probe = FileProbe(tmdb_id="1")
    s = build_signals(parse(Path("Dune.2024.mkv")), probe, cand(external_id="999"), [])
    assert s.external_id_match is False


def test_identifiant_dans_un_autre_referentiel_ne_conclut_pas() -> None:
    """Un tvdbid ne peut ni confirmer ni infirmer un candidat TMDB."""
    probe = FileProbe(tvdb_id="123")
    s = build_signals(parse(Path("Dune.2024.mkv")), probe, cand(provider="tmdb"), [])
    assert s.external_id_match is None


def test_absence_d_identifiant_ne_conclut_pas() -> None:
    s = build_signals(parse(Path("Dune.2024.mkv")), FileProbe(), cand(), [])
    assert s.external_id_match is None


# --- Annee ------------------------------------------------------------------


def test_annee_concordante() -> None:
    s = build_signals(parse(Path("Dune.2024.mkv")), FileProbe(), cand(year=2024), [])
    assert s.year_match is True


def test_tolerance_d_un_an() -> None:
    """Sortie salle en decembre, support en janvier : un an d'ecart legitime."""
    s = build_signals(parse(Path("Dune.2024.mkv")), FileProbe(), cand(year=2025), [])
    assert s.year_match is True


def test_annee_divergente() -> None:
    s = build_signals(parse(Path("Dune.2024.mkv")), FileProbe(), cand(year=1984), [])
    assert s.year_match is False


def test_annee_absente_du_fichier() -> None:
    s = build_signals(parse(Path("Dune.mkv")), FileProbe(), cand(year=2024), [])
    assert s.year_match is None


# --- Accord entre fournisseurs ----------------------------------------------


def test_deux_fournisseurs_concordants() -> None:
    a = cand(provider="tmdb", title="Frieren", year=2023)
    b = cand(provider="anilist", title="Frieren", year=2023)
    s = build_signals(parse(Path("Frieren.2023.mkv")), FileProbe(), a, [a, b])
    assert s.provider_agreement == 2


def test_un_seul_fournisseur() -> None:
    a = cand(provider="tmdb", title="Dune", year=2024)
    s = build_signals(parse(Path("Dune.2024.mkv")), FileProbe(), a, [a])
    assert s.provider_agreement == 1


# --- Homonymie --------------------------------------------------------------


def test_homonymes_detectes() -> None:
    """Deux « Dune » a egalite : incertitude structurelle."""
    a = cand(external_id="1", title="Dune", year=1984)
    b = cand(external_id="2", title="Dune", year=2021)
    s = build_signals(parse(Path("Dune.mkv")), FileProbe(), a, [a, b])
    assert s.ambiguous_candidates == 2


# --- Duree ------------------------------------------------------------------


def test_duree_incoherente_pour_un_film() -> None:
    probe = FileProbe(duration_seconds=22 * 60)
    s = build_signals(parse(Path("Dune.2024.mkv")), probe, cand(), [])
    assert s.runtime_plausible is False


def test_duree_inconnue() -> None:
    s = build_signals(parse(Path("Dune.2024.mkv")), FileProbe(), cand(), [])
    assert s.runtime_plausible is None


# --- Choix du meilleur candidat ---------------------------------------------


def test_aucun_candidat() -> None:
    assert best_match(parse(Path("Dune.2024.mkv")), FileProbe(), []) is None


def test_candidat_trop_eloigne_rejete() -> None:
    assert best_match(parse(Path("Severance.mkv")), FileProbe(), [cand(title="Dune")]) is None


def test_identifiant_declare_impose_le_candidat() -> None:
    """Meme avec un titre qui ne ressemble a rien, l'identifiant tranche."""
    probe = FileProbe(tmdb_id="693134")
    loin = cand(external_id="693134", title="Titre Totalement Different")
    proche = cand(external_id="42", title="Dune")
    result = best_match(parse(Path("Dune.2024.mkv")), probe, [loin, proche])
    assert result is not None
    assert result.candidate.external_id == "693134"


def test_meilleure_similarite_gagne_sans_identifiant() -> None:
    a = cand(external_id="1", title="Dune Part Two")
    b = cand(external_id="2", title="Dunkerque")
    result = best_match(parse(Path("Dune.Part.Two.2024.mkv")), FileProbe(), [a, b])
    assert result is not None
    assert result.candidate.external_id == "1"
    assert result.runner_up is not None
