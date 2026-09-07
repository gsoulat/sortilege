"""Tests de la politique de decision.

``compute_score`` n'est pas encore implemente : ces tests couvrent ``decide``
et ``explain``, qui sont deja le contrat sur lequel repose le comportement de
l'application. Ils passeront tels quels quand le calcul arrivera.
"""

import pytest

from sortilege.core.scoring import Decision, Policy, Signals, compute_score, decide, explain


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


POLICY = Policy(auto_apply_threshold=0.92, reject_threshold=0.40)


def test_score_eleve_applique() -> None:
    assert decide(0.97, signals(), POLICY) is Decision.AUTO


def test_score_moyen_part_en_revue() -> None:
    assert decide(0.70, signals(), POLICY) is Decision.REVIEW


def test_score_faible_rejete() -> None:
    assert decide(0.20, signals(), POLICY) is Decision.REJECT


def test_bornes_de_seuil_inclusives() -> None:
    assert decide(0.92, signals(), POLICY) is Decision.AUTO
    assert decide(0.40, signals(), POLICY) is Decision.REVIEW


def test_ia_non_fiable_plafonne_a_revue() -> None:
    """Avec trust_ai=False, une identification IA ne passe jamais en AUTO."""
    politique = Policy(auto_apply_threshold=0.92, reject_threshold=0.40, trust_ai=False)
    assert decide(0.99, signals(ai_confidence=0.95), politique) is Decision.REVIEW


def test_ia_non_fiable_rejette_quand_meme_sous_le_seuil() -> None:
    politique = Policy(auto_apply_threshold=0.92, reject_threshold=0.40, trust_ai=False)
    assert decide(0.10, signals(ai_confidence=0.95), politique) is Decision.REJECT


def test_ia_fiable_peut_passer_en_auto() -> None:
    assert decide(0.99, signals(ai_confidence=0.95), POLICY) is Decision.AUTO


def test_sans_ia_la_politique_trust_ai_est_sans_effet() -> None:
    politique = Policy(auto_apply_threshold=0.92, reject_threshold=0.40, trust_ai=False)
    assert decide(0.99, signals(ai_confidence=None), politique) is Decision.AUTO


# --- Explication ------------------------------------------------------------


def test_explication_signale_l_annee_manquante() -> None:
    lignes = explain(signals(year_match=None), 0.71, Decision.REVIEW)
    assert any("aucune annee" in ligne for ligne in lignes)


def test_explication_signale_l_homonymie() -> None:
    lignes = explain(signals(ambiguous_candidates=3), 0.60, Decision.REVIEW)
    assert any("homonymie" in ligne for ligne in lignes)


def test_explication_signale_l_annee_divergente() -> None:
    lignes = explain(signals(year_match=False), 0.50, Decision.REVIEW)
    assert any("DIVERGENTE" in ligne for ligne in lignes)


def test_explication_mentionne_l_assistance_ia() -> None:
    lignes = explain(signals(ai_confidence=0.8), 0.80, Decision.REVIEW)
    assert any("IA" in ligne for ligne in lignes)


def test_explication_commence_par_le_verdict() -> None:
    lignes = explain(signals(), 0.83, Decision.REVIEW)
    assert lignes[0].startswith("score 0.83")


# --- Contrat de compute_score -----------------------------------------------


@pytest.mark.xfail(raises=NotImplementedError, reason="compute_score reste a implementer")
def test_compute_score_respecte_ses_bornes() -> None:
    """Passera automatiquement des que la fonction sera ecrite."""
    for s in (
        signals(),
        signals(title_similarity=0.0, parse_quality=0.0, year_match=False, provider_agreement=0),
    ):
        score = compute_score(s)
        assert 0.0 <= score <= 1.0


@pytest.mark.xfail(raises=NotImplementedError, reason="compute_score reste a implementer")
def test_un_meilleur_candidat_score_plus_haut() -> None:
    fort = compute_score(signals(title_similarity=0.98, year_match=True, provider_agreement=3))
    faible = compute_score(signals(title_similarity=0.35, year_match=False, provider_agreement=0))
    assert fort > faible
