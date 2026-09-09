"""Recherche manuelle d'une oeuvre, quand aucune proposition ne convient.

Les candidats proposes viennent du titre LU dans le nom de fichier. Quand ce
nom est trop abime — un titre traduit, une abreviation, une faute du groupe de
release — aucune proposition ne peut etre bonne. L'utilisateur voyait alors que
c'etait faux sans aucun moyen de le corriger.

Le point qui decide si la fonction tient : un candidat trouve par recherche
doit etre acceptable par « choisir ». S'il ne l'etait pas, on aurait ajoute une
belle grille de jaquettes sur laquelle cliquer ne ferait rien.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import review
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision
from sortilege.main import app
from sortilege.providers.base import Candidate


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


@pytest.fixture
def plan_en_revue():
    p = Plan(
        id="cherche1",
        source=Path("/dl/un.nom.illisible.S01E01.mkv"),
        destination=Path("/lib/x.mkv"),
        kind="episode",
        score=0.42,
        decision=Decision.REVIEW,
        title="Un Nom Illisible",
        alternatives=[
            Candidate(provider="tmdb", external_id="111", title="Mauvaise Piste", year=2001)
        ],
    )
    review._plans[p.id] = p
    yield p
    review._plans.pop(p.id, None)


@pytest.fixture
def tmdb_simule(monkeypatch):
    """Un fournisseur qui repond, sans reseau."""

    class Faux:
        def __init__(self, *a, **k):
            pass

        async def search_series(self, titre, annee):
            return [
                Candidate(
                    provider="tmdb",
                    external_id="222",
                    title=f"Trouve : {titre}",
                    year=2024,
                    poster_url="https://exemple/affiche.jpg",
                    overview="x" * 500,
                )
            ]

        async def search_movie(self, titre, annee):
            return await self.search_series(titre, annee)

        async def aclose(self):
            pass

    monkeypatch.setattr(review, "TMDBProvider", Faux)

    # Une cle est necessaire pour que la recherche parte : sans elle, l'API
    # refuse explicitement plutot que de renvoyer une liste vide trompeuse.
    conf = review.get_settings()
    monkeypatch.setattr(conf, "tmdb_api_key", "cle-de-test", raising=False)
    return Faux


def test_sans_cle_la_recherche_le_dit(client, plan_en_revue, monkeypatch) -> None:
    """Une liste vide laisserait croire que la requete est mauvaise, alors que
    la recherche n'a jamais eu lieu."""
    conf = review.get_settings()
    monkeypatch.setattr(conf, "tmdb_api_key", "", raising=False)

    r = client.get("/api/review/cherche1/search", params={"q": "Severance"})
    assert r.status_code == 503
    assert "TheMovieDB" in r.json()["detail"]


# --- La recherche ------------------------------------------------------------


def test_une_recherche_rapporte_des_candidats(client, plan_en_revue, tmdb_simule) -> None:
    r = client.get("/api/review/cherche1/search", params={"q": "Severance"})

    assert r.status_code == 200
    resultats = r.json()["results"]
    assert resultats and resultats[0]["title"] == "Trouve : Severance"
    assert resultats[0]["poster_url"], "la jaquette est ce qui permet de trancher"


def test_le_resume_est_tronque(client, plan_en_revue, tmdb_simule) -> None:
    """Le selecteur affiche deux lignes, pas un synopsis."""
    r = client.get("/api/review/cherche1/search", params={"q": "Severance"})
    assert len(r.json()["results"][0]["overview"]) <= 200


def test_une_requete_trop_courte_est_refusee(client, plan_en_revue, tmdb_simule) -> None:
    """Une lettre ramenerait le catalogue entier."""
    assert client.get("/api/review/cherche1/search", params={"q": "a"}).status_code == 400


def test_un_plan_inconnu_donne_404(client, tmdb_simule) -> None:
    r = client.get("/api/review/fantome/search", params={"q": "Severance"})
    assert r.status_code == 404


# --- Le point qui fait tenir l'ensemble --------------------------------------


def test_un_resultat_de_recherche_devient_choisissable(client, plan_en_revue, tmdb_simule) -> None:
    """LA propriete a verifier. « Choisir » n'accepte qu'un candidat que le
    SERVEUR a rapporte — jamais un identifiant fabrique par le client. Sans
    cette jonction, on aurait ajoute une grille sur laquelle cliquer ne ferait
    rien."""
    client.get("/api/review/cherche1/search", params={"q": "Severance"})

    ids = {(c.provider, c.external_id) for c in review._plans["cherche1"].alternatives}
    assert ("tmdb", "222") in ids, "le resultat a rejoint les alternatives du plan"


def test_un_identifiant_invente_reste_refuse(client, plan_en_revue) -> None:
    """L'invariant tient toujours : chercher n'ouvre pas la porte a n'importe
    quel identifiant envoye par le navigateur."""
    r = client.post(
        "/api/review/cherche1/choose",
        json={"provider": "tmdb", "external_id": "999999"},
    )
    assert r.status_code == 400


def test_les_propositions_d_origine_survivent(client, plan_en_revue, tmdb_simule) -> None:
    """Chercher AJOUTE, ne remplace pas : on doit pouvoir revenir a ce qui
    etait propose si la recherche s'avere pire."""
    client.get("/api/review/cherche1/search", params={"q": "Severance"})

    titres = {c.title for c in review._plans["cherche1"].alternatives}
    assert "Mauvaise Piste" in titres


def test_une_recherche_repetee_ne_duplique_pas(client, plan_en_revue, tmdb_simule) -> None:
    for _ in range(3):
        client.get("/api/review/cherche1/search", params={"q": "Severance"})

    alternatives = review._plans["cherche1"].alternatives
    cles = [(c.provider, c.external_id) for c in alternatives]
    assert len(cles) == len(set(cles))
