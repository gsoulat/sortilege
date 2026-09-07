"""Tests des endpoints HTTP.

Le refus de demarrer sur une configuration incomplete est teste ici aussi :
c'est un comportement de securite, pas un detail de confort.
"""

import pytest
from fastapi.testclient import TestClient

from sortilege.config import Settings
from sortilege.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_catalogue_de_jetons(client: TestClient) -> None:
    r = client.get("/api/tokens")
    assert r.status_code == 200
    data = r.json()
    noms = {t["name"] for t in data["tokens"]}
    assert "title" in noms
    assert "absolute_episode" in noms
    assert "jellyfin" in data["presets"]


def test_apercu_nominal(client: TestClient) -> None:
    r = client.post(
        "/api/templates/preview",
        json={"template": "Films/{title}{? year: ($)}", "kind": "movie"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is True
    assert any("Dune (2024)" in res["path"] for res in data["results"])


def test_apercu_gabarit_invalide_ne_leve_pas(client: TestClient) -> None:
    """Un gabarit en cours de frappe est invalide en permanence : l'UI a besoin
    d'un message, pas d'une 500."""
    r = client.post(
        "/api/templates/preview",
        json={"template": "Films/{inconnu}", "kind": "movie"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["valid"] is False
    assert "jeton inconnu" in data["error"]
    assert data["results"] == []


def test_apercu_refuse_un_chemin_absolu(client: TestClient) -> None:
    r = client.post(
        "/api/templates/preview",
        json={"template": "/etc/{title}", "kind": "movie"},
    )
    assert r.json()["valid"] is False


def test_type_inconnu_retombe_sur_les_films(client: TestClient) -> None:
    r = client.post(
        "/api/templates/preview",
        json={"template": "{title}", "kind": "nawak"},
    )
    assert r.status_code == 200
    assert r.json()["valid"] is True


# --- Garde-fous de configuration ---------------------------------------------


def test_seuils_incoherents_rejetes() -> None:
    """reject >= auto viderait la file de revue : tout serait auto ou rejete."""
    with pytest.raises(ValueError, match="reject_threshold"):
        Settings(
            auto_apply_threshold=0.5,
            reject_threshold=0.9,
            secret_key="x" * 40,
            admin_password="p",
        )


def test_configuration_incomplete_detectee() -> None:
    s = Settings(secret_key="", admin_password="", source_roots=[])
    problemes = s.check_runtime()
    assert any("SECRET_KEY" in p for p in problemes)
    assert any("ADMIN_PASSWORD" in p for p in problemes)
    assert any("SOURCE_ROOTS" in p for p in problemes)


def test_ia_activee_sans_cle_detectee() -> None:
    s = Settings(
        secret_key="x" * 40,
        admin_password="p",
        source_roots=["/tmp"],
        ai_enabled=True,
        anthropic_api_key="",
    )
    assert any("ANTHROPIC_API_KEY" in p for p in s.check_runtime())


def test_racines_multiples_separees_par_deux_points() -> None:
    s = Settings(
        source_roots="/a:/b:/c",
        secret_key="x" * 40,
        admin_password="p",
    )
    assert len(s.source_roots) == 3
