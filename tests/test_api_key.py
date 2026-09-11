"""Tests de la cle d'API et du declenchement externe.

L'enjeu est double et symetrique. La cle doit OUVRIR — sans elle, un client de
telechargement ne peut rien declencher et la fonction n'existe pas. Et elle
doit etre la SEULE chose qui ouvre en dehors d'une session : une deuxieme porte
mal fermee vaut moins qu'une porte de moins.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps, integration
from sortilege.core import apikey
from sortilege.main import app

PASSWORD = "mot-de-passe-de-test"


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Volume de donnees propre : la cle generee ici ne doit pas fuir dans le
    dossier partage par le reste de la suite."""
    monkeypatch.setattr(deps, "DATA_DIR", tmp_path)
    for accesseur in (deps.get_store, deps.get_memory, deps.get_journal):
        accesseur.cache_clear()
    yield tmp_path
    for accesseur in (deps.get_store, deps.get_memory, deps.get_journal):
        accesseur.cache_clear()


@pytest.fixture(autouse=True)
def _declenchement_au_repos() -> None:
    """L'etat du declencheur est un module : un test qui laisse une tache en
    attente ferait echouer le suivant pour une raison sans rapport."""
    integration._state = integration.TriggerState()
    integration._task = None


@pytest.fixture
def anonyme(volume: Path) -> TestClient:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def logged(anonyme: TestClient) -> TestClient:
    assert anonyme.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    return anonyme


def cle(client: TestClient) -> str:
    return client.get("/api/settings/api-key").json()["api_key"]


# --- La cle -----------------------------------------------------------------


def test_une_cle_existe_des_le_premier_demarrage(logged: TestClient) -> None:
    """Personne ne doit avoir a la creer : elle serait absente le jour ou on en
    a besoin, c'est-a-dire en branchant un client de telechargement."""
    corps = logged.get("/api/settings/api-key").json()
    assert corps["api_key"].startswith(apikey.PREFIX)
    assert len(corps["api_key"]) >= apikey.MIN_LENGTH
    assert corps["header"] == "X-Api-Key"


def test_la_cle_se_lit_en_clair_contrairement_aux_autres(logged: TestClient) -> None:
    """Exception assumee : une cle qu'on ne peut pas lire ne peut pas etre
    recopiee dans un script, donc la fonction n'existe pas."""
    corps = logged.get("/api/settings/api-key").json()
    assert corps["api_key"] in corps["example"]
    assert "X-Api-Key" in corps["example"]


def test_la_cle_ne_sort_pas_dans_la_reponse_generale_des_reglages(logged: TestClient) -> None:
    """Cette reponse-la est demandee a chaque ouverture de l'ecran et finit dans
    le cache du navigateur : la cle y trainerait sans que personne ne la lise."""
    valeur = cle(logged)
    bloc = logged.get("/api/settings/preferences").json()["integration"]
    assert bloc["api_key_set"] is True
    assert bloc["api_key_masked"] != valeur
    assert valeur not in logged.get("/api/settings/preferences").text


def test_la_cle_se_regenere_et_revoque_l_ancienne(logged: TestClient) -> None:
    ancienne = cle(logged)
    nouvelle = logged.post("/api/settings/api-key/regenerate").json()["api_key"]
    assert nouvelle != ancienne

    with TestClient(app) as autre:
        assert autre.get("/api/settings", headers={"X-Api-Key": ancienne}).status_code == 401
        assert autre.get("/api/settings", headers={"X-Api-Key": nouvelle}).status_code == 200


def test_la_cle_n_est_jamais_mise_en_cache(logged: TestClient) -> None:
    """La cle vaut le mot de passe : ni le navigateur ni un proxy ne doivent la
    garder sur disque, ou la resservir apres une regeneration qui la croyait
    revoquee. La forme de la reponse, elle, ne change pas."""
    for reponse in (
        logged.get("/api/settings/api-key"),
        logged.post("/api/settings/api-key/regenerate"),
    ):
        assert reponse.status_code == 200
        assert reponse.headers["cache-control"] == "no-store"
        assert reponse.headers["pragma"] == "no-cache"
        assert set(reponse.json()) == {"api_key", "header", "masked", "example"}


def test_enregistrer_les_reglages_ne_perd_pas_la_cle(logged: TestClient) -> None:
    """Le formulaire ne porte pas la cle : l'omettre a la fusion la remettrait a
    vide, et tous les scripts branches dessus tomberaient sans qu'on y touche."""
    valeur = cle(logged)
    reponse = logged.put("/api/settings/preferences", json={"scan": {"min_size_mb": 42}})
    assert reponse.status_code == 200
    assert cle(logged) == valeur


def test_une_cle_courte_est_refusee_a_l_enregistrement(volume: Path) -> None:
    from sortilege.core.preferences import IntegrationSettings, PreferenceError, Preferences

    store = deps.get_store()
    with pytest.raises(PreferenceError, match="au moins") as refus:
        store.save(Preferences(integration=IntegrationSettings(api_key="trop-court")))
    # Le refus dit OU regenerer, avec le chemin exact de l'ecran.
    assert "Réglages → Système → Intégration" in str(refus.value)


# --- Ce que la cle ouvre, et ce qui reste ferme ------------------------------


def test_l_en_tete_ouvre_l_api_sans_cookie(logged: TestClient, volume: Path) -> None:
    valeur = cle(logged)
    with TestClient(app) as sans_session:
        reponse = sans_session.get("/api/settings", headers={"X-Api-Key": valeur})
        assert reponse.status_code == 200
        assert not sans_session.cookies


def test_sans_cle_ni_cookie_tout_est_refuse(anonyme: TestClient) -> None:
    for methode, chemin in (
        ("GET", "/api/settings"),
        ("GET", "/api/backup/archive"),
        ("POST", "/api/integration/download-complete"),
        ("GET", "/api/integration"),
        ("GET", "/api/settings/api-key"),
        # Le schema OpenAPI a ete deplace sous /api/ precisement pour tomber
        # sous cette protection : il decrivait toute la surface de l'API a qui
        # atteignait le port.
        ("GET", "/api/openapi.json"),
    ):
        reponse = anonyme.request(methode, chemin, json={})
        assert reponse.status_code == 401, f"{methode} {chemin} est accessible sans rien"


def test_le_refus_dit_quoi_faire(anonyme: TestClient) -> None:
    detail = anonyme.get("/api/settings").json()["detail"]
    assert "X-Api-Key" in detail
    assert "Réglages → Système → Intégration" in detail


def test_une_mauvaise_cle_est_refusee(anonyme: TestClient) -> None:
    faux = apikey.PREFIX + "x" * 40
    assert anonyme.get("/api/settings", headers={"X-Api-Key": faux}).status_code == 401


def test_une_cle_vide_n_ouvre_rien() -> None:
    """Le cas dangereux : installation dont la cle a ete effacee. Un « == » sur
    deux chaines vides aurait ouvert l'API a une requete sans en-tete."""
    assert apikey.verify("", "") is False
    assert apikey.verify(None, "") is False
    assert apikey.verify("n'importe quoi", "") is False


def test_une_cle_trop_courte_n_est_jamais_acceptee() -> None:
    assert apikey.verify("abc", "abc") is False


def test_la_cle_masquee_ne_permet_pas_de_s_en_servir() -> None:
    valeur = apikey.generate()
    masquee = apikey.masked(valeur)
    assert masquee != valeur
    assert apikey.verify(masquee, valeur) is False


# --- Declenchement externe ---------------------------------------------------


def test_le_declenchement_rend_la_main_tout_de_suite(logged: TestClient, volume: Path) -> None:
    """202 et non 200 : rien n'est range quand la reponse part, et un client qui
    lit le code doit pouvoir distinguer « pris en compte » de « fait »."""
    reponse = logged.post("/api/integration/download-complete", json={})
    assert reponse.status_code == 202
    corps = reponse.json()
    assert corps["accepted"] is True
    assert corps["scheduled"] is True


def test_le_declenchement_est_idempotent(logged: TestClient, volume: Path) -> None:
    """Un client appelle une fois par fichier d'un torrent, et reessaie apres un
    delai d'attente : les appels rapproches doivent se replier sur un cycle."""
    premier = logged.post("/api/integration/download-complete", json={})
    second = logged.post("/api/integration/download-complete", json={})
    troisieme = logged.post("/api/integration/download-complete", json={"name": "Autre"})

    assert premier.json()["scheduled"] is True
    for suivant in (second, troisieme):
        # Meme code, meme « accepted » : insister ne doit jamais ressembler a un
        # echec du cote du client.
        assert suivant.status_code == 202
        assert suivant.json()["accepted"] is True
        assert suivant.json()["scheduled"] is False
    assert integration._state.accepted == 3


def test_le_declenchement_accepte_la_cle_d_api(logged: TestClient, volume: Path) -> None:
    valeur = cle(logged)
    with TestClient(app) as client_de_telechargement:
        reponse = client_de_telechargement.post(
            "/api/integration/download-complete",
            headers={"X-Api-Key": valeur},
            json={"path": "/tmp/sortilege-test/downloads/Un.Film.2024"},
        )
        assert reponse.status_code == 202
        assert reponse.json()["in_scope"] is True


def test_un_chemin_hors_perimetre_le_dit_au_lieu_de_se_taire(
    logged: TestClient, volume: Path
) -> None:
    """L'erreur de branchement la plus courante, et la plus silencieuse."""
    reponse = logged.post(
        "/api/integration/download-complete", json={"path": "/tmp/ailleurs/Un.Film.2024"}
    )
    assert reponse.status_code == 202
    corps = reponse.json()
    assert corps["in_scope"] is False
    assert corps["scheduled"] is False
    assert "Réglages" in corps["detail"]


def test_un_appel_sans_corps_marche(logged: TestClient, volume: Path) -> None:
    """C'est ce qu'on ecrit en premier pour verifier que la cle passe."""
    assert logged.post("/api/integration/download-complete").status_code == 202


def test_l_etat_du_declenchement_est_consultable(logged: TestClient, volume: Path) -> None:
    logged.post("/api/integration/download-complete", json={"name": "Un.Transfert"})
    corps = logged.get("/api/integration").json()
    assert corps["accepted"] == 1
    assert corps["last_source"] == "Un.Transfert"
    assert corps["pending"] is True
