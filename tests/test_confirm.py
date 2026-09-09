"""Confirmation d'une identification proposee.

Il manquait le geste symetrique de « Ce n'est pas ca » : on pouvait refuser une
identification, jamais l'accepter. Or un plan a 79 % est tres souvent correct —
le score dit l'incertitude de la MACHINE, pas celle de la personne qui regarde
l'ecran.

Ce qui se joue ici est la PORTEE. Un bouton par ligne qui agirait en douce sur
toute une serie serait pire qu'absent : on ne saurait pas ce qu'on vient de
valider. La portee est donc toujours demandee, jamais supposee.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import review
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision
from sortilege.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


def plan(pid: str, *, title: str = "Reacher", kind: str = "episode", **kw) -> Plan:
    base = dict(
        id=pid,
        source=Path(f"/dl/{pid}.mkv"),
        destination=Path(f"/lib/{pid}.mkv"),
        kind=kind,
        score=0.79,
        decision=Decision.REVIEW,
        title=title,
        year=2022,
        provider="tmdb",
        external_id="108978",
    )
    base.update(kw)
    return Plan(**base)


@pytest.fixture
def queue():
    """Deux series distinctes, pour verifier qu'on ne deborde pas."""
    review._plans.clear()
    for p in [
        plan("r1"),
        plan("r2"),
        plan("r3"),
        plan("autre", title="Severance", external_id="95396"),
    ]:
        review._plans[p.id] = p
    yield review._plans
    review._plans.clear()


# --- Portee ----------------------------------------------------------------


def test_par_defaut_un_seul_fichier_est_confirme(client: TestClient, queue) -> None:
    """Le defaut est le plus etroit : elargir en silence est le defaut qu'on
    cherche a eviter."""
    r = client.post("/api/review/r1/confirm", json={})

    assert r.status_code == 200
    assert r.json()["confirmed"] == 1
    assert queue["r1"].decision is Decision.AUTO
    assert queue["r2"].decision is Decision.REVIEW


def test_une_liste_explicite_confirme_exactement_ce_qui_est_demande(
    client: TestClient, queue
) -> None:
    """C'est ce que fait le bouton de serie : l'interface sait precisement ce
    qu'elle affiche, elle n'a pas a le faire deviner par un titre."""
    r = client.post("/api/review/r1/confirm", json={"plan_ids": ["r1", "r2", "r3"]})

    assert r.json()["confirmed"] == 3
    assert all(queue[k].decision is Decision.AUTO for k in ("r1", "r2", "r3"))
    assert queue["autre"].decision is Decision.REVIEW, "l'autre serie n'a pas bouge"


def test_toute_la_serie_par_deduction_du_titre(client: TestClient, queue) -> None:
    r = client.post("/api/review/r1/confirm", json={"whole_series": True})

    assert r.json()["confirmed"] == 3
    assert queue["autre"].decision is Decision.REVIEW


def test_un_film_ne_deborde_jamais_sur_ses_homonymes(client: TestClient, queue) -> None:
    """Deux films du meme titre sont deux oeuvres ; les series, non."""
    review._plans["f1"] = plan("f1", title="Dune", kind="movie", external_id="1")
    review._plans["f2"] = plan("f2", title="Dune", kind="movie", external_id="2")

    client.post("/api/review/f1/confirm", json={"whole_series": True})

    assert review._plans["f2"].decision is Decision.REVIEW


def test_un_identifiant_inconnu_dans_la_liste_est_ignore(client: TestClient, queue) -> None:
    r = client.post("/api/review/r1/confirm", json={"plan_ids": ["r1", "fantome"]})
    assert r.json()["confirmed"] == 1


# --- Effet ------------------------------------------------------------------


def test_un_plan_confirme_devient_applicable(client: TestClient, queue) -> None:
    """C'est tout l'objet : passer de « a arbitrer » a « pret a ranger »."""
    client.post("/api/review/r1/confirm", json={})

    assert queue["r1"].decision is Decision.AUTO
    assert queue["r1"].score == 1.0
    assert queue["r1"].manual is True


def test_la_destination_n_est_pas_touchee(client: TestClient, queue) -> None:
    """Confirmer valide l'IDENTIFICATION, pas le chemin : recalculer ici
    changerait sous les yeux ce que l'utilisateur vient d'approuver."""
    avant = queue["r1"].destination
    client.post("/api/review/r1/confirm", json={})
    assert queue["r1"].destination == avant


def test_le_choix_est_retenu_pour_les_scans_suivants(client: TestClient, queue) -> None:
    """Sans memoire, la meme question reviendrait au prochain scan et la file
    ne convergerait jamais."""
    client.post("/api/review/r1/confirm", json={})

    decisions = client.get("/api/settings/decisions").json()["decisions"]
    assert any(d["title"] == "Reacher" and d["external_id"] == "108978" for d in decisions)


# --- Refus ------------------------------------------------------------------


def test_un_plan_inconnu_donne_404(client: TestClient, queue) -> None:
    assert client.post("/api/review/fantome/confirm", json={}).status_code == 404


def test_un_fichier_sans_identification_ne_se_confirme_pas(client: TestClient, queue) -> None:
    """Il n'y a rien a approuver : le laisser passer marquerait AUTO un plan
    sans destination, qui echouerait a l'application."""
    review._plans["vide"] = plan("vide", title="", provider="", external_id="")
    assert client.post("/api/review/vide/confirm", json={}).status_code == 400
