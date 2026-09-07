"""Tests de la POLITIQUE de decision.

Separes de ceux du calcul (test_scoring_compute.py) comme le code l'est :
``decide`` traduit un score en action, ``compute_score`` produit ce score. On
peut durcir les seuils sans toucher au calcul, et rejouer d'anciennes mesures
sous une nouvelle politique — ces tests-la ne bougeront pas.
"""

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


# --- Signaux issus du fichier (probe.py) -------------------------------------


def test_identifiant_declare_concordant_court_circuite_le_score() -> None:
    """Un tmdbid de .nfo est une reponse, pas une ressemblance a evaluer."""
    assert decide(0.10, signals(external_id_match=True), POLICY) is Decision.AUTO


def test_identifiant_declare_divergent_disqualifie() -> None:
    """Un identifiant qui designe une AUTRE oeuvre l'emporte sur tout score."""
    assert decide(0.99, signals(external_id_match=False), POLICY) is Decision.REJECT


def test_identifiants_ignores_si_la_politique_le_demande() -> None:
    prudente = Policy(auto_apply_threshold=0.92, reject_threshold=0.40, trust_external_ids=False)
    assert decide(0.10, signals(external_id_match=True), prudente) is Decision.REJECT
    assert decide(0.99, signals(external_id_match=False), prudente) is Decision.AUTO


def test_duree_incompatible_bloque_l_application_automatique() -> None:
    """Un « film » de 22 minutes merite un regard, meme bien score."""
    assert decide(0.99, signals(runtime_plausible=False), POLICY) is Decision.REVIEW


def test_duree_incompatible_ne_sauve_pas_un_mauvais_score() -> None:
    assert decide(0.10, signals(runtime_plausible=False), POLICY) is Decision.REJECT


def test_duree_inconnue_sans_effet() -> None:
    assert decide(0.99, signals(runtime_plausible=None), POLICY) is Decision.AUTO


def test_identifiant_prime_sur_la_duree() -> None:
    """Une identite declaree l'emporte sur une duree atypique (director's cut,
    episode double, film de 20 minutes qui existe vraiment)."""
    decision = decide(0.99, signals(external_id_match=True, runtime_plausible=False), POLICY)
    assert decision is Decision.AUTO


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


def test_explication_signale_l_identifiant_declare() -> None:
    lignes = explain(signals(external_id_match=True), 0.99, Decision.AUTO)
    assert any("certitude" in ligne for ligne in lignes)


def test_explication_signale_la_duree_incompatible() -> None:
    lignes = explain(signals(runtime_plausible=False), 0.80, Decision.REVIEW)
    assert any("duree" in ligne for ligne in lignes)


def test_explication_commence_par_le_verdict() -> None:
    lignes = explain(signals(), 0.83, Decision.REVIEW)
    assert lignes[0].startswith("score 0.83")


# --- Contrat de compute_score -----------------------------------------------


def test_compute_score_respecte_ses_bornes() -> None:
    """Le contrat minimal, quel que soit le reglage des poids."""
    for s in (
        signals(),
        signals(title_similarity=0.0, parse_quality=0.0, year_match=False, provider_agreement=0),
    ):
        score = compute_score(s)
        assert 0.0 <= score <= 1.0


def test_un_meilleur_candidat_score_plus_haut() -> None:
    fort = compute_score(signals(title_similarity=0.98, year_match=True, provider_agreement=3))
    faible = compute_score(signals(title_similarity=0.35, year_match=False, provider_agreement=0))
    assert fort > faible
