"""Reclasser la file quand les seuils bougent.

Cas reel : seuil "pret" regle a 60 %, et la file gardait des plans a 64, 67,
70 ou 73 % "a verifier". La decision etait figee au calcul du plan, et un plan
ne gardait pas les signaux que la decision lit : rien ne pouvait le reclasser
sans repasser chez les fournisseurs.

Deux promesses, verifiees ici :

- **un verdict venu des seuils suit les seuils**, sans appel reseau ;
- **un verdict impose ne bouge jamais** : confirmation ou choix a la main,
  identification memorisee, identifiant declare, regle. Un reglage de seuil
  n'a pas a defaire ce qu'une personne a tranche.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps, review
from sortilege.core.matching import MatchResult
from sortilege.core.parser import parse
from sortilege.core.pipeline import Pipeline
from sortilege.core.planner import Plan, build_plan, redecide_plans
from sortilege.core.preferences import IdentificationSettings
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile
from sortilege.core.scoring import Decision, Policy, Signals, VerdictSource, verdict
from sortilege.core.snapshot import PLANS_KEY, plans_in, plans_out
from sortilege.core.store import Decision as RememberedDecision
from sortilege.core.template import PRESETS
from sortilege.main import app
from sortilege.providers.base import Candidate

GO = 1024**3

STRICT = Policy(auto_apply_threshold=0.92, reject_threshold=0.40)
SOUPLE = Policy(auto_apply_threshold=0.60, reject_threshold=0.40)


def _scanne(nom: str, *, duree: float | None = None, tmdb_id: str | None = None) -> ScannedFile:
    chemin = Path("/dl") / nom
    return ScannedFile(
        path=chemin,
        size_bytes=GO,
        parsed=parse(chemin, ["dl"]),
        probe=FileProbe(duration_seconds=duree, tmdb_id=tmdb_id),
    )


def _signaux(**champs) -> Signals:
    base = dict(
        title_similarity=1.0,
        parse_quality=1.0,
        year_match=None,
        episode_match=None,
        provider_agreement=1,
        provider_popularity=0.5,
        ai_confidence=None,
        ambiguous_candidates=1,
    )
    base.update(champs)
    return Signals(**base)


def _plan_seuils(score: float = 0.67, *, pid: str = "p67", **signaux) -> Plan:
    """Un plan tel que ``build_plan`` le produit sous ``STRICT``, ramene a un
    score fixe : le score calcule depend de poids qui ne sont pas le sujet."""
    plan = build_plan(
        _scanne(f"{pid}.mkv"),
        MatchResult(
            candidate=Candidate(provider="tmdb", external_id="1", title=pid),
            signals=_signaux(**signaux),
            similarity=1.0,
        ),
        template=PRESETS["jellyfin"]["movie"],
        destination_root=Path("/media"),
        policy=STRICT,
    )
    plan.id = pid
    plan.score = score
    plan.decision, _ = verdict(
        score,
        external_id_match=plan.external_id_match,
        ai_confidence=plan.ai_confidence,
        runtime_plausible=plan.runtime_plausible,
        policy=STRICT,
    )
    plan.reasons = [f"score {score:.2f} -> {plan.decision.value}", "autre motif"]
    return plan


# --- Le coeur : un verdict des seuils suit les seuils -----------------------


def test_un_plan_a_67_est_a_verifier_sous_92_et_pret_sous_60() -> None:
    plan = _plan_seuils(0.67)
    assert (plan.decision, plan.verdict_source) == (Decision.REVIEW, VerdictSource.THRESHOLDS)

    rapport = redecide_plans([plan], SOUPLE)

    assert plan.decision is Decision.AUTO
    assert rapport.changed[Decision.AUTO] == 1
    assert plan.reasons == ["score 0.67 -> auto", "autre motif"], "seule la 1re ligne change"
    assert plan.score == 0.67, "le score mesure les signaux, pas la politique"

    rapport = redecide_plans([plan], STRICT)

    assert plan.decision is Decision.REVIEW
    assert rapport.changed[Decision.REVIEW] == 1
    assert plan.reasons[0] == "score 0.67 -> review"


def test_rejouer_sous_la_meme_politique_ne_change_rien() -> None:
    plan = _plan_seuils(0.67)

    rapport = redecide_plans([plan], STRICT)

    assert (rapport.unchanged, rapport.changed_total, rapport.modified) == (1, 0, False)


def test_une_duree_incompatible_reste_a_verifier_au_dessus_du_seuil() -> None:
    plan = _plan_seuils(0.95, runtime_plausible=False)
    assert plan.runtime_plausible is False

    redecide_plans([plan], SOUPLE)

    assert plan.decision is Decision.REVIEW


def test_un_plan_passe_par_l_ia_suit_trust_ai() -> None:
    plan = _plan_seuils(0.95, ai_confidence=0.95)
    assert plan.ai_confidence == 0.95

    redecide_plans([plan], Policy(auto_apply_threshold=0.60, trust_ai=False))
    assert plan.decision is Decision.REVIEW, "sans confiance dans l'IA : jamais pret seul"

    redecide_plans([plan], Policy(auto_apply_threshold=0.60, trust_ai=True))
    assert plan.decision is Decision.AUTO


def test_build_plan_retient_les_trois_signaux_et_l_origine() -> None:
    plan = _plan_seuils(0.67, runtime_plausible=True, ai_confidence=0.7)

    assert plan.verdict_source is VerdictSource.THRESHOLDS
    assert (plan.external_id_match, plan.ai_confidence, plan.runtime_plausible) == (None, 0.7, True)


# --- Ce qui ne bouge jamais -------------------------------------------------


def test_un_identifiant_concordant_ne_bouge_jamais() -> None:
    scanne = _scanne("Dune.mkv", tmdb_id="438631")
    plan = build_plan(
        scanne,
        MatchResult(
            candidate=Candidate(provider="tmdb", external_id="438631", title="Dune"),
            signals=_signaux(external_id_match=True),
            similarity=1.0,
        ),
        template=PRESETS["jellyfin"]["movie"],
        destination_root=Path("/media"),
        policy=STRICT,
    )
    assert (plan.decision, plan.verdict_source) == (Decision.AUTO, VerdictSource.EXTERNAL_ID)
    plan.score = 0.50
    avant = list(plan.reasons)

    # Rejoue, ce plan tomberait "a verifier" : l'identifiant ne serait plus cru.
    rapport = redecide_plans([plan], Policy(trust_external_ids=False))

    assert plan.decision is Decision.AUTO
    assert plan.reasons == avant
    assert rapport.imposed == 1


async def test_une_identification_memorisee_ne_bouge_jamais() -> None:
    class Memoire:
        def recall(self, kind, title):
            return RememberedDecision(
                kind="movie", title_key="dune", provider="tmdb", external_id="1", title="Dune"
            )

        def note_hit(self, kind, key):
            pass

    pipeline = Pipeline(
        tmdb=None,
        anilist=None,
        library_root=Path("/media"),
        templates={"movie": PRESETS["jellyfin"]["movie"]},
        policy=STRICT,
        memory=Memoire(),
    )
    # 22 minutes pour un film : rejoue, la duree le ramenerait "a verifier".
    plan = await pipeline._recall(_scanne("Dune.mkv", duree=22 * 60))
    assert (plan.decision, plan.verdict_source) == (Decision.AUTO, VerdictSource.MEMORY)
    assert plan.runtime_plausible is False

    rapport = redecide_plans([plan], SOUPLE)

    assert plan.decision is Decision.AUTO
    assert rapport.imposed == 1


def test_un_plan_confirme_a_la_main_ne_bouge_jamais(client: TestClient) -> None:
    plan = _plan_seuils(0.67, pid="confirme", runtime_plausible=False)
    review._plans[plan.id] = plan
    assert client.post(f"/api/review/{plan.id}/confirm", json={}).status_code == 200
    assert plan.verdict_source is VerdictSource.HUMAN

    rapport = redecide_plans([plan], SOUPLE)

    assert (plan.decision, plan.reasons) == (Decision.AUTO, ["identification confirmee a la main"])
    assert rapport.imposed == 1


async def test_un_candidat_choisi_a_la_main_est_humain_et_la_regle_d_episode_tient() -> None:
    pipeline = Pipeline(
        tmdb=None,
        anilist=None,
        library_root=Path("/media"),
        templates=dict(PRESETS["jellyfin"]),
        policy=STRICT,
    )
    precedent = Plan(
        id="x", source=Path("/dl/x.mkv"), destination=None, kind="movie", score=0.5,
        decision=Decision.REVIEW,
    )  # fmt: skip
    serie = Candidate(provider="tmdb", external_id="2", title="The Vampire Diaries", kind="episode")

    avec_numero = await pipeline.replan_with(
        _scanne("The.Vampire.Diaries.S01E02.mkv"), serie, precedent
    )
    assert (avec_numero.decision, avec_numero.verdict_source) == (
        Decision.AUTO,
        VerdictSource.HUMAN,
    )

    # Sans numero d'episode, la regle etait ecrasee par le verdict humain et le
    # fichier partait "pret" vers "Season/Titre - SE".
    sans_numero = await pipeline.replan_with(_scanne("The Vampire Diaries.mkv"), serie, precedent)
    assert (sans_numero.decision, sans_numero.verdict_source) == (
        Decision.REVIEW,
        VerdictSource.EPISODE_RULE,
    )

    rapport = redecide_plans([avec_numero, sans_numero], SOUPLE)
    assert rapport.imposed == 2
    assert (avec_numero.decision, sans_numero.decision) == (Decision.AUTO, Decision.REVIEW)


def test_un_plan_en_echec_n_est_pas_rejoue() -> None:
    plan = _plan_seuils(0.67)
    plan.error = "chemin impossible"

    rapport = redecide_plans([plan], SOUPLE)

    assert (plan.decision, rapport.imposed) == (Decision.REVIEW, 1)


def test_un_plan_ancien_est_compte_non_reclassable_et_reste_intact() -> None:
    plan = Plan(
        id="ancien",
        source=Path("/dl/ancien.mkv"),
        destination=Path("/media/ancien.mkv"),
        kind="movie",
        score=0.67,
        decision=Decision.REVIEW,
        reasons=["score 0.67 -> review"],
    )
    assert plan.verdict_source is VerdictSource.UNKNOWN, "defaut : non reclassable"

    rapport = redecide_plans([plan], SOUPLE)

    assert (plan.decision, plan.reasons) == (Decision.REVIEW, ["score 0.67 -> review"])
    assert (rapport.legacy, rapport.changed_total, rapport.modified) == (1, 0, False)


def test_chaque_plan_est_compte_une_fois() -> None:
    confirme = _plan_seuils(0.67, pid="c")
    confirme.manual = True
    confirme.verdict_source = VerdictSource.HUMAN
    ancien = _plan_seuils(0.67, pid="a")
    ancien.verdict_source = VerdictSource.UNKNOWN
    file = [_plan_seuils(0.67, pid="r"), _plan_seuils(0.95, pid="p"), confirme, ancien]

    rapport = redecide_plans(file, SOUPLE)

    assert rapport.examined == 4
    assert (rapport.changed_total, rapport.unchanged, rapport.imposed, rapport.legacy) == (
        1,
        1,
        1,
        1,
    )
    assert rapport.examined == (
        rapport.changed_total + rapport.unchanged + rapport.imposed + rapport.legacy
    )


# --- Instantane --------------------------------------------------------------


def test_l_origine_et_les_signaux_traversent_l_instantane() -> None:
    plan = _plan_seuils(0.67, ai_confidence=0.8, runtime_plausible=False)

    relu = plans_in(plans_out([plan]))[0]

    assert relu.verdict_source is VerdictSource.THRESHOLDS
    assert (relu.external_id_match, relu.ai_confidence, relu.runtime_plausible) == (
        None,
        0.8,
        False,
    )
    redecide_plans([relu], SOUPLE)
    assert relu.decision is Decision.REVIEW, "la duree relue joue encore"


def test_un_instantane_ancien_se_relit_et_ses_plans_sont_non_reclassables() -> None:
    payload = plans_out([_plan_seuils(0.67)])
    for champ in ("verdict_source", "external_id_match", "ai_confidence", "runtime_plausible"):
        del payload["plans"][0][champ]

    relu = plans_in(payload)[0]

    assert relu.verdict_source is VerdictSource.UNKNOWN
    rapport = redecide_plans([relu], SOUPLE)
    assert (relu.decision, rapport.legacy) == (Decision.REVIEW, 1)


@pytest.mark.parametrize(
    ("champ", "valeur"),
    [
        ("verdict_source", "une-origine-future"),
        ("runtime_plausible", "oui"),
        ("ai_confidence", True),
    ],
)
def test_une_valeur_douteuse_rend_le_plan_non_reclassable_sans_jeter_la_file(
    champ: str, valeur: object
) -> None:
    payload = plans_out([_plan_seuils(0.67)])
    payload["plans"][0][champ] = valeur

    relu = plans_in(payload)[0]

    assert relu.verdict_source is VerdictSource.UNKNOWN


def test_un_signal_manquant_rend_le_plan_non_reclassable() -> None:
    payload = plans_out([_plan_seuils(0.67)])
    del payload["plans"][0]["runtime_plausible"]

    assert plans_in(payload)[0].verdict_source is VerdictSource.UNKNOWN


# --- La route ----------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        review._plans.clear()
        yield c
        review._plans.clear()
        review._persist_plans()


@pytest.fixture
def seuils_a_60():
    store = deps.get_store()
    avant = copy.deepcopy(store.load())
    prefs = store.load()
    prefs.identification = IdentificationSettings(auto_apply_percent=60, reject_percent=40)
    store.save(prefs)
    yield
    store._cache = None
    store.save(avant)


def test_la_route_reclasse_et_rend_son_compte_rendu(client: TestClient, seuils_a_60) -> None:
    ancien = _plan_seuils(0.67, pid="ancien")
    ancien.verdict_source = VerdictSource.UNKNOWN
    confirme = _plan_seuils(0.67, pid="confirme")
    confirme.manual = True
    confirme.verdict_source = VerdictSource.HUMAN
    for plan in (_plan_seuils(0.67, pid="a67"), _plan_seuils(0.30, pid="a30"), confirme, ancien):
        review._plans[plan.id] = plan

    reponse = client.post("/api/review/redecide")

    assert reponse.status_code == 200
    assert reponse.json() == {
        "examined": 4,
        "changed": 1,
        "changed_to": {"auto": 1, "review": 0, "reject": 0},
        "unchanged": 1,
        "imposed": 1,
        "legacy": 1,
        "policy": {"auto_apply_threshold": 0.60, "reject_threshold": 0.40},
        "planning_running": False,
    }
    assert review._plans["a67"].decision is Decision.AUTO


def test_le_reclassement_est_enregistre(client: TestClient, seuils_a_60) -> None:
    plan = _plan_seuils(0.67, pid="persiste")
    review._plans[plan.id] = plan

    client.post("/api/review/redecide")

    relus = {p.id: p for p in plans_in(deps.get_memory().load_blob(PLANS_KEY))}
    assert relus["persiste"].decision is Decision.AUTO
    assert relus["persiste"].reasons[0] == "score 0.67 -> auto"

    # Ce que fait un redemarrage : la file repart du disque.
    review._plans.clear()
    assert review.restore_plans() is True
    assert review._plans["persiste"].decision is Decision.AUTO
