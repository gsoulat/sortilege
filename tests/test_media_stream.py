"""Lecture d'un fichier depuis l'interface.

Un titre et une affiche ne disent pas si le fichier est le bon. Quelques
secondes de video tranchent la ou aucun score ne le peut.

Deux proprietes decident si c'est utilisable et sur :

1. **Aucun chemin ne vient du client.** On designe un PLAN ; le serveur sait
   seul quel fichier cela vise. Un endpoint servant un chemin fourni par le
   navigateur donnerait la lecture de tout ce qui est monte dans le conteneur.
2. **Les requetes par plage fonctionnent.** Sans elles, deplacer le curseur
   d'une video de trois gigaoctets impose de la telecharger depuis le debut —
   le navigateur ne propose meme pas la barre de progression.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import review
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision
from sortilege.main import app

CONTENT = bytes(range(256)) * 8  # 2048 octets, reconnaissables a l'octet pres


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        response = c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"})
        assert response.status_code == 200, "la connexion de test a echoue"
        yield c


@pytest.fixture
def plan(tmp_path: Path):
    """Un plan dont la source existe reellement sur le disque."""
    source = tmp_path / "Severance.S01E01.mp4"
    source.write_bytes(CONTENT)
    p = Plan(
        id="lecture1",
        source=source,
        destination=tmp_path / "lib" / "x.mp4",
        kind="episode",
        score=0.78,
        decision=Decision.REVIEW,
        title="Severance",
    )
    review._plans[p.id] = p
    yield p
    review._plans.pop(p.id, None)


# --- Lecture complete -------------------------------------------------------


def test_le_fichier_est_servi_en_entier(client: TestClient, plan) -> None:
    r = client.get(f"/api/media/plan/{plan.id}")

    assert r.status_code == 200
    assert r.content == CONTENT
    assert r.headers["accept-ranges"] == "bytes"


def test_le_type_est_devine_depuis_l_extension(client: TestClient, plan) -> None:
    """Sans type correct, le navigateur refuse de lire meme un format qu'il
    sait decoder."""
    r = client.get(f"/api/media/plan/{plan.id}")
    assert r.headers["content-type"].startswith("video/")


def test_rien_n_est_mis_en_cache(client: TestClient, plan) -> None:
    """Le fichier peut etre deplace juste apres : servir plus tard un contenu
    qui n'est plus a cette place induirait en erreur."""
    r = client.get(f"/api/media/plan/{plan.id}")
    assert r.headers["cache-control"] == "no-store"


# --- Requetes par plage -----------------------------------------------------


def test_une_plage_renvoie_exactement_ce_qui_est_demande(client: TestClient, plan) -> None:
    r = client.get(f"/api/media/plan/{plan.id}", headers={"Range": "bytes=10-19"})

    assert r.status_code == 206
    assert r.content == CONTENT[10:20]
    assert r.headers["content-range"] == f"bytes 10-19/{len(CONTENT)}"
    assert r.headers["content-length"] == "10"


def test_une_plage_ouverte_va_jusqu_a_la_fin(client: TestClient, plan) -> None:
    """« bytes=2040- » est ce que demande un lecteur qui reprend."""
    r = client.get(f"/api/media/plan/{plan.id}", headers={"Range": "bytes=2040-"})

    assert r.status_code == 206
    assert r.content == CONTENT[2040:]


def test_une_plage_au_dela_de_la_fin_est_ramenee_aux_bornes(client: TestClient, plan) -> None:
    """Certains lecteurs demandent au-dela de la fin en fin de lecture :
    echouer les ferait afficher une erreur sur une video complete."""
    r = client.get(f"/api/media/plan/{plan.id}", headers={"Range": "bytes=999999-"})

    assert r.status_code == 206
    assert len(r.content) >= 1


def test_un_entete_de_plage_illisible_sert_le_fichier_entier(client: TestClient, plan) -> None:
    r = client.get(f"/api/media/plan/{plan.id}", headers={"Range": "octets=0-10"})
    assert r.status_code == 200
    assert r.content == CONTENT


# --- Ce qu'on refuse de servir ----------------------------------------------


def test_un_plan_inconnu_ne_revele_rien(client: TestClient) -> None:
    """La seule entree est un identifiant de plan : il n'y a pas de chemin a
    manipuler, et un identifiant invente ne donne qu'un 404."""
    assert client.get("/api/media/plan/inexistant").status_code == 404


def test_une_source_disparue_donne_404(client: TestClient, plan) -> None:
    plan.source.unlink()
    assert client.get(f"/api/media/plan/{plan.id}").status_code == 404


def test_la_lecture_exige_une_session(plan) -> None:
    """L'endpoint sert des fichiers : le laisser ouvert exposerait la
    mediatheque entiere a qui connait un identifiant."""
    with TestClient(app) as anonymous:
        assert anonymous.get(f"/api/media/plan/{plan.id}").status_code == 401
