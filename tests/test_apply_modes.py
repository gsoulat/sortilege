"""Simuler et executer sont deux boutons, pas deux deploiements.

Le mode vient desormais de la requete. La variable d'environnement reste un
VERROU : elle peut forcer la simulation, jamais l'autoriser a l'inverse — sinon
un verrou pose volontairement sur une instance serait contourne par un clic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import review
from sortilege.config import get_settings
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision
from sortilege.main import app

PASSWORD = "mot-de-passe-de-test"


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    conf = get_settings()
    monkeypatch.setattr(conf, "library_root", tmp_path / "media")
    (tmp_path / "media").mkdir()

    with TestClient(app) as c:
        c.post("/api/auth/login", json={"password": PASSWORD})
        yield c


@pytest.fixture
def un_plan(tmp_path: Path):
    """Un plan pret a etre applique, source reellement presente sur le disque."""
    source = tmp_path / "dl" / "Dune.2024.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("contenu")

    plan = Plan(
        id="plan-test",
        source=source,
        destination=tmp_path / "media" / "Films" / "Dune (2024).mkv",
        kind="movie",
        score=0.97,
        decision=Decision.AUTO,
    )
    review._plans.clear()
    review._plans[plan.id] = plan
    yield plan
    review._plans.clear()


def test_le_defaut_est_la_simulation(client: TestClient, un_plan) -> None:
    """Une requete sans le champ ne doit RIEN deplacer.

    Le defaut d'une operation irreversible doit etre l'inaction."""
    res = client.post("/api/review/apply", json={"plan_ids": ["plan-test"]})
    assert res.json()["dry_run"] is True
    assert un_plan.source.is_file()
    assert not un_plan.destination.exists()


def test_simuler_ne_deplace_rien(client: TestClient, un_plan) -> None:
    res = client.post("/api/review/apply", json={"plan_ids": ["plan-test"], "dry_run": True})
    body = res.json()
    assert body["dry_run"] is True
    assert body["applied"] == 1
    assert un_plan.source.is_file()


def test_executer_deplace(client: TestClient, un_plan, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "dry_run", False)
    res = client.post("/api/review/apply", json={"plan_ids": ["plan-test"], "dry_run": False})
    body = res.json()
    assert body["dry_run"] is False
    assert body["applied"] == 1
    assert not un_plan.source.exists()
    assert un_plan.destination.is_file()


def test_la_variable_peut_forcer_la_simulation(client: TestClient, un_plan, monkeypatch) -> None:
    """Le verrou l'emporte sur le bouton, et le dit."""
    monkeypatch.setattr(get_settings(), "dry_run", True)
    res = client.post("/api/review/apply", json={"plan_ids": ["plan-test"], "dry_run": False})
    body = res.json()
    assert body["dry_run"] is True
    assert body["locked"] is True
    assert un_plan.source.is_file(), "le verrou n'a pas empeche le deplacement"


def test_la_variable_ne_peut_pas_forcer_l_execution(
    client: TestClient, un_plan, monkeypatch
) -> None:
    """L'inverse n'existe pas : dry_run=false dans l'environnement ne doit pas
    transformer un clic sur « Simuler » en deplacement reel."""
    monkeypatch.setattr(get_settings(), "dry_run", False)
    res = client.post("/api/review/apply", json={"plan_ids": ["plan-test"], "dry_run": True})
    assert res.json()["dry_run"] is True
    assert un_plan.source.is_file()


def test_une_simulation_ne_vide_pas_la_file(client: TestClient, un_plan) -> None:
    """On doit pouvoir enchainer simulation puis execution sur les memes plans."""
    client.post("/api/review/apply", json={"plan_ids": ["plan-test"], "dry_run": True})
    assert "plan-test" in review._plans


def test_une_execution_retire_le_plan(client: TestClient, un_plan, monkeypatch) -> None:
    """Sa source n'existe plus : le laisser inviterait a le rejouer."""
    monkeypatch.setattr(get_settings(), "dry_run", False)
    client.post("/api/review/apply", json={"plan_ids": ["plan-test"], "dry_run": False})
    assert "plan-test" not in review._plans


def test_le_statut_expose_le_verrou(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "dry_run", True)
    assert client.get("/api/review").json()["dry_run_locked"] is True
