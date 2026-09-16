"""Les seuils de decision, reglables en pourcentage depuis l'interface.

Deux promesses, et ce fichier verifie les deux :

- **l'existant ne bouge pas.** Une installation qui a fixe ses seuils dans son
  environnement les garde tant que personne n'a rien enregistre depuis l'ecran
  — y compris avec un ``preferences.json`` ecrit avant que le reglage existe ;
- **le reglage agit vraiment.** Un seuil accepte, enregistre et affiche, mais
  ignore par le calcul des plans serait le pire des etats. D'ou les tests qui
  regardent la politique REELLEMENT passee au pipeline, dans le traitement
  manuel comme dans le cycle automatique, et le verdict d'un plan a score fixe.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from sortilege.api import automation, deps, review
from sortilege.core import vpn
from sortilege.core.parser import parse
from sortilege.core.planner import Plan
from sortilege.core.preferences import (
    SOURCE_ENV,
    SOURCE_SETTING,
    THRESHOLD_FROM_ENV,
    IdentificationSettings,
    PreferenceError,
    Preferences,
    PreferenceStore,
    effective_thresholds,
)
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile, ScanResult
from sortilege.core.scoring import Decision, Signals, decide
from sortilege.main import app

ENV_AUTO = 0.90
ENV_REJET = 0.35
"""Distincts des defauts livres (0.92 / 0.40) : un test qui passerait avec les
defauts ne prouverait pas que l'environnement est lu."""

SCORE_FIXE = 0.85
"""Pret a ranger sous un reglage a 80 %, a verifier sous l'environnement a 90 %."""

GO = 1024**3


def _signaux() -> Signals:
    return Signals(
        title_similarity=1.0,
        parse_quality=1.0,
        year_match=True,
        episode_match=None,
        provider_agreement=1,
        provider_popularity=0.5,
        ai_confidence=None,
        ambiguous_candidates=1,
    )


@pytest.fixture(autouse=True)
def environnement(monkeypatch):
    """Seuils de l'environnement maitrises : le .env local du poste en porte."""
    conf = deps.get_settings()
    monkeypatch.setattr(conf, "auto_apply_threshold", ENV_AUTO)
    monkeypatch.setattr(conf, "reject_threshold", ENV_REJET)
    return conf


@pytest.fixture(autouse=True)
def _preferences_rendues():
    """Rend au magasin partage ses preferences d'avant le test."""
    store = deps.get_store()
    avant = copy.deepcopy(store.load())
    yield store
    store._cache = None
    store.save(avant)
    vpn.reset_cache()


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


def _regle(**champs) -> None:
    store = deps.get_store()
    prefs = store.load()
    prefs.identification = IdentificationSettings(**champs)
    store.save(prefs)


# --- Seuils effectifs --------------------------------------------------------


def test_rien_de_regle_donne_exactement_l_environnement() -> None:
    seuils = effective_thresholds(IdentificationSettings(), ENV_AUTO, ENV_REJET)

    assert (seuils.auto_apply, seuils.reject) == (ENV_AUTO, ENV_REJET)
    assert seuils.auto_apply_source == seuils.reject_source == SOURCE_ENV
    assert seuils.ignored_reason == ""


def test_le_reglage_prime_sur_l_environnement() -> None:
    seuils = effective_thresholds(
        IdentificationSettings(auto_apply_percent=80, reject_percent=25), ENV_AUTO, ENV_REJET
    )

    assert (seuils.auto_apply, seuils.reject) == (0.80, 0.25)
    assert seuils.auto_apply_source == seuils.reject_source == SOURCE_SETTING
    assert (seuils.auto_apply_percent, seuils.reject_percent) == (80, 25)


def test_chaque_seuil_a_son_propre_repli() -> None:
    seuils = effective_thresholds(
        IdentificationSettings(auto_apply_percent=80), ENV_AUTO, ENV_REJET
    )

    assert (seuils.auto_apply, seuils.reject) == (0.80, ENV_REJET)
    assert seuils.auto_apply_source == SOURCE_SETTING
    assert seuils.reject_source == SOURCE_ENV


def test_la_fonction_unique_lit_le_magasin_et_l_environnement() -> None:
    assert deps.decision_policy().auto_apply_threshold == ENV_AUTO

    _regle(auto_apply_percent=80, reject_percent=25)

    politique = deps.decision_policy()
    assert (politique.auto_apply_threshold, politique.reject_threshold) == (0.80, 0.25)


def test_un_ancien_fichier_sans_le_bloc_retombe_sur_l_environnement(tmp_path: Path) -> None:
    fichier = tmp_path / "preferences.json"
    fichier.write_text(
        json.dumps({"destinations": {"movie": "Films"}, "ai": {"enabled": False}}),
        encoding="utf-8",
    )
    prefs = PreferenceStore(fichier, tmp_path / "media", [tmp_path / "dl"]).load()

    assert prefs.identification == IdentificationSettings()
    seuils = effective_thresholds(prefs.identification, ENV_AUTO, ENV_REJET)
    assert (seuils.auto_apply, seuils.reject) == (ENV_AUTO, ENV_REJET)


def test_un_seuil_de_la_mauvaise_nature_est_ignore_a_la_relecture(tmp_path: Path) -> None:
    """« "92" » ecrit a la main ne doit pas arriver jusqu'a la comparaison du score."""
    fichier = tmp_path / "preferences.json"
    fichier.write_text(
        json.dumps({"identification": {"auto_apply_percent": "92", "reject_percent": 30}}),
        encoding="utf-8",
    )
    prefs = PreferenceStore(fichier, tmp_path / "media", [tmp_path / "dl"]).load()

    assert prefs.identification.auto_apply_percent == THRESHOLD_FROM_ENV
    assert prefs.identification.reject_percent == 30


# --- Refus -------------------------------------------------------------------


@pytest.mark.parametrize(("auto", "rejet"), [(50, 50), (40, 60)])
def test_ecarte_au_dessus_de_pret_est_refuse(tmp_path: Path, auto: int, rejet: int) -> None:
    store = PreferenceStore(tmp_path / "p.json", tmp_path / "media", [tmp_path / "dl"])
    prefs = Preferences()
    prefs.identification = IdentificationSettings(auto_apply_percent=auto, reject_percent=rejet)

    with pytest.raises(PreferenceError, match="strictement sous"):
        store.validate(prefs)


@pytest.mark.parametrize("champ", ["auto_apply_percent", "reject_percent"])
def test_hors_de_0_a_100_est_refuse(tmp_path: Path, champ: str) -> None:
    store = PreferenceStore(tmp_path / "p.json", tmp_path / "media", [tmp_path / "dl"])
    prefs = Preferences()
    prefs.identification = IdentificationSettings(**{champ: 150})

    with pytest.raises(PreferenceError, match="entre 0 et 100 %"):
        store.validate(prefs)


def test_un_melange_incoherent_est_refuse_a_la_saisie() -> None:
    """« Pret » a 30 % sous un « ecarte » du .env a 35 %."""
    with pytest.raises(PreferenceError, match=r"valeur du \.env"):
        effective_thresholds(
            IdentificationSettings(auto_apply_percent=30), ENV_AUTO, ENV_REJET, strict=True
        )


def test_un_melange_devenu_incoherent_retombe_sur_l_environnement_en_le_disant() -> None:
    """Le .env a change apres coup : le calcul ne doit ni lever ni se taire."""
    seuils = effective_thresholds(
        IdentificationSettings(auto_apply_percent=30), ENV_AUTO, ENV_REJET
    )

    assert (seuils.auto_apply, seuils.reject) == (ENV_AUTO, ENV_REJET)
    assert seuils.auto_apply_source == SOURCE_ENV
    assert "ignoré" in seuils.ignored_reason


# --- API ---------------------------------------------------------------------


def test_get_expose_les_pourcentages_et_leur_source(client: TestClient) -> None:
    bloc = client.get("/api/settings/preferences").json()["identification"]

    assert bloc["auto_apply_percent"] == 90
    assert bloc["reject_percent"] == 35
    assert bloc["auto_apply_source"] == bloc["reject_source"] == "environnement"
    assert bloc["environment"]["auto_apply_percent"] == 90
    assert bloc["environment"]["reject_percent"] == 35
    assert bloc["ignored_reason"] == ""


def test_put_en_pourcentage_puis_retour_a_l_environnement(client: TestClient) -> None:
    reponse = client.put(
        "/api/settings/preferences",
        json={"identification": {"auto_apply_percent": 80, "reject_percent": 25}},
    )
    assert reponse.status_code == 200
    bloc = reponse.json()["identification"]
    assert (bloc["auto_apply_percent"], bloc["reject_percent"]) == (80, 25)
    assert bloc["auto_apply_source"] == bloc["reject_source"] == "reglage"
    assert bloc["environment"]["auto_apply_percent"] == 90, "l'environnement reste affiche"

    # Un champ absent ne bouge pas ; null efface le reglage de ce seul champ.
    reponse = client.put(
        "/api/settings/preferences", json={"identification": {"reject_percent": None}}
    )
    bloc = reponse.json()["identification"]
    assert (bloc["auto_apply_percent"], bloc["auto_apply_source"]) == (80, "reglage")
    assert (bloc["reject_percent"], bloc["reject_source"]) == (35, "environnement")

    reponse = client.put(
        "/api/settings/preferences",
        json={"identification": {"auto_apply_percent": None, "reject_percent": None}},
    )
    bloc = reponse.json()["identification"]
    assert (bloc["auto_apply_percent"], bloc["reject_percent"]) == (90, 35)
    assert bloc["auto_apply_source"] == bloc["reject_source"] == "environnement"
    assert deps.get_store().load().identification == IdentificationSettings()


def test_un_autre_enregistrement_ne_touche_pas_aux_seuils(client: TestClient) -> None:
    _regle(auto_apply_percent=80, reject_percent=25)

    client.put("/api/settings/preferences", json={"scan": {"min_size_mb": 60}})

    assert deps.get_store().load().identification == IdentificationSettings(80, 25)


@pytest.mark.parametrize(
    ("corps", "extrait"),
    [
        ({"auto_apply_percent": 150}, "entre 0 et 100 %"),
        ({"reject_percent": -1}, "entre 0 et 100 %"),
        ({"auto_apply_percent": 50, "reject_percent": 50}, "strictement sous"),
        ({"auto_apply_percent": 30}, "valeur du .env"),
    ],
)
def test_put_refuse_en_disant_pourquoi(client: TestClient, corps: dict, extrait: str) -> None:
    reponse = client.put("/api/settings/preferences", json={"identification": corps})

    assert reponse.status_code == 400
    assert extrait in reponse.json()["detail"]
    assert deps.get_store().load().identification == IdentificationSettings(), "rien d'ecrit"


def test_le_diagnostic_du_systeme_montre_les_seuils_effectifs(client: TestClient) -> None:
    _regle(auto_apply_percent=80)

    comportement = client.get("/api/settings").json()["behaviour"]

    assert comportement["auto_apply_threshold"] == 0.80
    assert comportement["auto_apply_source"] == "reglage"
    assert comportement["reject_threshold"] == ENV_REJET


# --- Le branchement : les plans calcules suivent le reglage -------------------


def test_le_verdict_d_un_score_fixe_change_avec_le_reglage() -> None:
    assert decide(SCORE_FIXE, _signaux(), deps.decision_policy()) is Decision.REVIEW

    _regle(auto_apply_percent=80, reject_percent=25)

    assert decide(SCORE_FIXE, _signaux(), deps.decision_policy()) is Decision.AUTO


class PipelineEspion:
    """Retient la politique recue et decide un plan a score fixe avec elle."""

    instances: ClassVar[list[PipelineEspion]] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        PipelineEspion.instances.append(self)

    async def plan_all(self, files, on_progress=None, on_plan=None):
        politique = self.kwargs["policy"]
        plans = [
            Plan(
                id=f"seuil-{i}",
                source=f.path,
                destination=None,
                kind="movie",
                score=SCORE_FIXE,
                decision=decide(SCORE_FIXE, _signaux(), politique),
            )
            for i, f in enumerate(files)
        ]
        # Publies avant de rendre la main, comme le vrai pipeline : le test
        # relit la file depuis un autre fil que celui du calcul.
        for plan in plans:
            if on_plan is not None:
                on_plan(plan)
        return plans

    async def aclose(self) -> None:
        pass


@pytest.fixture
def espion(monkeypatch):
    PipelineEspion.instances.clear()
    monkeypatch.setattr(review, "Pipeline", PipelineEspion)
    monkeypatch.setattr(automation, "Pipeline", PipelineEspion)
    yield PipelineEspion
    review._plans.clear()
    review._job.done_paths.clear()


async def _sortie_libre(*args, **kwargs) -> vpn.Decision:
    return vpn.Decision(allowed=True, warn=False, reason="")


def test_build_plans_utilise_les_seuils_du_reglage(
    client: TestClient, espion, monkeypatch, tmp_path: Path
) -> None:
    _regle(auto_apply_percent=80, reject_percent=25)
    film = tmp_path / "dl" / "Dune.2021.mkv"
    # Present sur le disque : un fichier du scan disparu depuis n'est plus a
    # identifier, c'est la regle commune avec le compteur de l'ecran.
    film.parent.mkdir(parents=True)
    film.write_bytes(b"")
    # Au-dessus du plancher de taille : un fichier trop petit n'est jamais planifie.
    scanne = ScannedFile(path=film, size_bytes=GO, parsed=parse(film, ["dl"]), probe=FileProbe())
    monkeypatch.setattr(review, "last_scan", lambda: ScanResult(files=[scanne]))
    monkeypatch.setattr(review, "tmdb_key", lambda: "cle-de-test")
    monkeypatch.setattr(review, "egress_allowed", _sortie_libre)

    reponse = client.post("/api/review/plan?reset=true")
    assert reponse.status_code == 200
    assert reponse.json()["started"] is True
    assert reponse.json()["policy"] == {"auto_apply_threshold": 0.80, "reject_threshold": 0.25}

    # Le calcul tourne en tache de fond : on attend qu'il rende la main.
    limite = time.monotonic() + 5
    while review._job.running and time.monotonic() < limite:
        time.sleep(0.02)
    assert not review._job.running

    politique = espion.instances[-1].kwargs["policy"]
    assert (politique.auto_apply_threshold, politique.reject_threshold) == (0.80, 0.25)
    assert review._plans["seuil-0"].decision is Decision.AUTO


async def test_le_cycle_automatique_utilise_les_seuils_du_reglage(
    espion, monkeypatch, tmp_path: Path
) -> None:
    source = tmp_path / "dl"
    bibliotheque = tmp_path / "media"
    source.mkdir()
    bibliotheque.mkdir()
    film = source / "Dune.2021.mkv"
    with film.open("wb") as fichier:
        fichier.seek(2 * GO)
        fichier.write(b"\0")

    conf = automation.get_settings()
    monkeypatch.setattr(conf, "library_root", bibliotheque)

    prefs = Preferences()
    prefs.identification = IdentificationSettings(auto_apply_percent=80, reject_percent=25)

    class Magasin:
        def load(self):
            return prefs

        def resolved_sources(self):
            return [source]

        def destination_root(self, kind, size=None):
            return bibliotheque

    magasin = Magasin()
    monkeypatch.setattr(automation, "get_store", lambda: magasin)
    monkeypatch.setattr(automation, "tmdb_key", lambda: "cle-de-test")
    monkeypatch.setattr(automation, "TMDBProvider", lambda *a, **k: None)
    monkeypatch.setattr(automation, "AniListProvider", lambda *a, **k: None)
    monkeypatch.setattr(automation, "egress_allowed", _sortie_libre)
    automation._watcher.__init__()

    rapport = await automation.run_cycle(forced=True)

    politique = espion.instances[-1].kwargs["policy"]
    assert (politique.auto_apply_threshold, politique.reject_threshold) == (0.80, 0.25)
    assert rapport.planned == 1
    assert rapport.queued == 0, "a 85 %, le plan est pret sous un seuil regle a 80 %"
    assert "1 plan(s) prêt(s)" in rapport.message
