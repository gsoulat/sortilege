"""Tests de l'authentification.

L'enjeu n'est pas que la connexion marche, mais que TOUT le reste soit ferme
sans elle. Une seule route oubliee suffit a rendre la protection decorative.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sortilege.core.auth import (
    SESSION_COOKIE,
    LoginThrottle,
    check_password,
    issue_session,
    verify_session,
)
from sortilege.main import app

PASSWORD = "mot-de-passe-de-test"  # celui pose par conftest
SECRET = "k" * 48


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def logged(client: TestClient) -> TestClient:
    res = client.post("/api/auth/login", json={"password": PASSWORD})
    assert res.status_code == 200
    return client


# --- Fermeture par defaut ---------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "/api/library/scan"),
        ("GET", "/api/workspace"),
        ("POST", "/api/review/plan"),
        ("POST", "/api/review/evacuate"),
        ("GET", "/api/collection/trash"),
        ("POST", "/api/collection/trash/purge"),
        ("GET", "/api/media/plan/quelconque"),
        ("POST", "/api/review/apply"),
        ("POST", "/api/review/undo"),
        ("GET", "/api/review/journal"),
        ("GET", "/api/settings"),
        ("GET", "/api/settings/preferences"),
        ("PUT", "/api/settings/preferences"),
        ("GET", "/api/settings/browse"),
        ("GET", "/api/tokens"),
        ("POST", "/api/templates/preview"),
    ],
)
def test_tout_est_ferme_sans_session(client: TestClient, method: str, path: str) -> None:
    response = client.request(method, path, json={})
    assert response.status_code == 401, f"{method} {path} est accessible sans session"


def test_health_reste_ouvert(client: TestClient) -> None:
    """Necessaire au HEALTHCHECK du conteneur, qui n'a pas de session."""
    assert client.get("/api/health").status_code == 200


def test_l_interface_reste_servie_sans_session(client: TestClient) -> None:
    """C'est elle qui affiche l'ecran de connexion : la fermer bloquerait tout."""
    assert client.get("/api/auth/me").json()["authenticated"] is False


# --- Connexion --------------------------------------------------------------


def test_mot_de_passe_correct_ouvre_l_acces(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/settings").status_code == 200


def test_mot_de_passe_incorrect_refuse(client: TestClient) -> None:
    res = client.post("/api/auth/login", json={"password": "faux"})
    assert res.status_code == 401
    assert client.get("/api/settings").status_code == 401


def test_deconnexion_referme(logged: TestClient) -> None:
    assert logged.get("/api/settings").status_code == 200
    logged.post("/api/auth/logout")
    assert logged.get("/api/settings").status_code == 401


def test_cookie_inaccessible_au_javascript(client: TestClient) -> None:
    """HttpOnly limite le vol de session par XSS."""
    res = client.post("/api/auth/login", json={"password": PASSWORD})
    cookie = res.headers.get("set-cookie", "")
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()


def test_le_mot_de_passe_ne_transite_jamais_dans_le_cookie(client: TestClient) -> None:
    res = client.post("/api/auth/login", json={"password": PASSWORD})
    assert PASSWORD not in res.headers.get("set-cookie", "")


# --- Jetons -----------------------------------------------------------------


def test_jeton_valide() -> None:
    assert verify_session(issue_session(SECRET), SECRET) is True


def test_jeton_forge_refuse() -> None:
    assert verify_session("n-importe-quoi", SECRET) is False


def test_jeton_absent_refuse() -> None:
    assert verify_session(None, SECRET) is False


def test_changer_la_cle_invalide_les_sessions() -> None:
    """Regenerer SECRET_KEY doit deconnecter tout le monde."""
    token = issue_session(SECRET)
    assert verify_session(token, "une-autre-cle-tout-a-fait-differente") is False


def test_cookie_falsifie_rejete(client: TestClient) -> None:
    client.cookies.set(SESSION_COOKIE, "valeur.forgee.a.la.main")
    assert client.get("/api/settings").status_code == 401


# --- Comparaison et limitation ----------------------------------------------


def test_comparaison_de_mot_de_passe() -> None:
    assert check_password("abc", "abc") is True
    assert check_password("abd", "abc") is False


def test_mot_de_passe_vide_refuse_tout() -> None:
    """Sans mot de passe configure, on ferme au lieu d'ouvrir."""
    assert check_password("", "") is False
    assert check_password("quoi que ce soit", "") is False


def test_verrouillage_apres_trop_d_echecs() -> None:
    throttle = LoginThrottle()
    assert throttle.remaining_lockout() == 0
    for _ in range(8):
        throttle.record_failure()
    assert throttle.remaining_lockout() > 0


def test_une_reussite_remet_le_compteur_a_zero() -> None:
    throttle = LoginThrottle()
    for _ in range(5):
        throttle.record_failure()
    throttle.record_success()
    assert throttle.failures == 0
    assert throttle.remaining_lockout() == 0
