"""Tests du calcul de confiance.

Les valeurs exactes des poids sont destinees a etre ajustees : ces tests
verifient donc des PROPRIETES (ordres, plafonds, asymetries), pas des nombres.
Un reglage des poids ne doit pas casser la suite ; un changement de logique,
si.
"""

import pytest

from sortilege.core.scoring import Signals, compute_score


def signals(**kw) -> Signals:
    base = dict(
        title_similarity=0.9,
        parse_quality=0.8,
        year_match=True,
        episode_match=None,
        provider_agreement=2,
        provider_popularity=0.7,
        ai_confidence=None,
        ambiguous_candidates=1,
    )
    base.update(kw)
    return Signals(**base)


def test_toujours_borne() -> None:
    for s in (
        signals(),
        signals(title_similarity=0.0, parse_quality=0.0, year_match=False, provider_agreement=0),
        signals(title_similarity=1.0, parse_quality=1.0, provider_agreement=3, episode_match=True),
    ):
        assert 0.0 <= compute_score(s) <= 1.0


def test_meilleur_candidat_score_plus_haut() -> None:
    fort = compute_score(signals(title_similarity=0.98, year_match=True, provider_agreement=3))
    faible = compute_score(signals(title_similarity=0.35, year_match=False, provider_agreement=0))
    assert fort > faible


# --- Asymetrie confirmation / contradiction ---------------------------------


def test_une_annee_fausse_coute_plus_qu_une_annee_juste_ne_rapporte() -> None:
    """Trouver l'annee juste est ordinaire ; en trouver une fausse est alarmant."""
    neutre = compute_score(signals(year_match=None))
    juste = compute_score(signals(year_match=True))
    faux = compute_score(signals(year_match=False))
    assert (juste - neutre) < (neutre - faux)


def test_annee_absente_ne_penalise_pas() -> None:
    """« Je ne sais pas » n'est pas « c'est faux »."""
    assert compute_score(signals(year_match=None)) > compute_score(signals(year_match=False))


def test_episode_introuvable_penalise_lourdement() -> None:
    assert compute_score(signals(episode_match=False)) < compute_score(signals(episode_match=None))


# --- Accord entre fournisseurs ----------------------------------------------


def test_accord_progresse_de_facon_non_lineaire() -> None:
    """Le gain de 1 -> 2 doit depasser celui de 2 -> 3 : l'essentiel de
    l'information est dans le fait qu'une SECONDE source confirme."""
    un = compute_score(signals(provider_agreement=1))
    deux = compute_score(signals(provider_agreement=2))
    trois = compute_score(signals(provider_agreement=3))
    assert (deux - un) > (trois - deux)


def test_aucun_fournisseur_penalise() -> None:
    assert compute_score(signals(provider_agreement=0)) < compute_score(
        signals(provider_agreement=1)
    )


# --- Plafonds ---------------------------------------------------------------


def test_homonymie_plafonne_sous_l_application_automatique() -> None:
    """Deux candidats a egalite : aucune accumulation de bonus ne doit
    permettre de passer en automatique."""
    parfait = signals(
        title_similarity=1.0,
        parse_quality=1.0,
        provider_agreement=3,
        episode_match=True,
        ambiguous_candidates=2,
    )
    assert compute_score(parfait) < 0.92


def test_confiance_ia_agit_en_plafond() -> None:
    """Un modele hesitant borne le resultat, il ne s'y ajoute pas."""
    sans = compute_score(signals(title_similarity=1.0, provider_agreement=3))
    avec = compute_score(signals(title_similarity=1.0, provider_agreement=3, ai_confidence=0.4))
    assert avec <= 0.4
    assert avec < sans


def test_ia_confiante_ne_gonfle_pas_un_mauvais_score() -> None:
    """Un plafond haut n'ajoute rien : il ne fait que ne pas brider."""
    faible = dict(title_similarity=0.2, parse_quality=0.1, provider_agreement=0)
    sans = compute_score(signals(**faible))
    avec = compute_score(signals(**faible, ai_confidence=0.99))
    assert sans == pytest.approx(avec, abs=1e-9)


# --- Cas de bout en bout ----------------------------------------------------


def test_identification_franche_passe_en_automatique() -> None:
    net = signals(
        title_similarity=1.0,
        parse_quality=0.9,
        year_match=True,
        provider_agreement=2,
        provider_popularity=0.9,
        container_title_similarity=0.95,
    )
    assert compute_score(net) >= 0.92


def test_fichier_mal_nomme_reste_sous_le_seuil() -> None:
    flou = signals(
        title_similarity=0.55,
        parse_quality=0.35,
        year_match=None,
        provider_agreement=1,
        provider_popularity=0.2,
    )
    assert 0.40 <= compute_score(flou) < 0.92
