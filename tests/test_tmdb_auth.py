"""Les deux identifiants proposes par TMDB doivent fonctionner.

La page des parametres du compte TMDB propose cote a cote une « Cle d'API »
(v3, en parametre d'URL) et un « Jeton d'acces en lecture » (v4, en en-tete
Authorization). Ils ne sont pas interchangeables et se tromper donne un 401
sans explication. Ces tests verifient qu'AUCUNE requete ne part sans son
identifiant, quel que soit le format choisi.
"""

from __future__ import annotations

import httpx

from sortilege.providers.base import RateLimiter
from sortilege.providers.tmdb import TMDBProvider

CLE_V3 = "0123456789abcdef0123456789abcdef"
JETON_V4 = "eyJhbGciOiJIUzI1NiJ9.charge-utile-factice.signature-factice"


class Recorder:
    """Enregistre chaque requete pour verifier comment elle est authentifiee."""

    def __init__(self, empty_first: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self.empty_first = empty_first

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        # Premiere reponse vide : declenche la relance sans annee, qui est
        # justement l'appel qu'on avait oublie d'authentifier.
        if self.empty_first and len(self.requests) == 1:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200, json={"results": [{"id": 1, "title": "Dune", "release_date": "2024-01-01"}]}
        )

    @property
    def all_authenticated_by_header(self) -> bool:
        return all(r.headers.get("authorization", "").startswith("Bearer ") for r in self.requests)

    @property
    def all_authenticated_by_param(self) -> bool:
        return all("api_key=" in str(r.url) for r in self.requests)


def make(secret: str, recorder: Recorder) -> TMDBProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder.handle))
    provider = TMDBProvider(secret, client=client)
    provider._limiter = RateLimiter(per_second=0)
    return provider


async def test_cle_v3_passe_en_parametre() -> None:
    recorder = Recorder()
    provider = make(CLE_V3, recorder)
    try:
        await provider.search_movie("Dune", 2024)
    finally:
        await provider.aclose()

    assert recorder.all_authenticated_by_param
    assert "authorization" not in recorder.requests[0].headers


async def test_jeton_v4_passe_en_en_tete() -> None:
    recorder = Recorder()
    provider = make(JETON_V4, recorder)
    try:
        await provider.search_movie("Dune", 2024)
    finally:
        await provider.aclose()

    assert recorder.all_authenticated_by_header
    # Le jeton ne doit PAS finir aussi dans l'URL : les URL se retrouvent dans
    # les journaux d'acces et les referents.
    assert "api_key=" not in str(recorder.requests[0].url)


async def test_toutes_les_relances_sont_authentifiees() -> None:
    """La relance sans annee partait sans en-tete : 401 silencieux."""
    recorder = Recorder(empty_first=True)
    provider = make(JETON_V4, recorder)
    try:
        await provider.search_movie("Dune", 2024)
    finally:
        await provider.aclose()

    assert len(recorder.requests) == 2, "la relance sans annee n'a pas eu lieu"
    assert recorder.all_authenticated_by_header


async def test_toutes_les_routes_sont_authentifiees() -> None:
    """Recherche, saison et saga : aucune ne doit oublier l'identifiant."""
    recorder = Recorder()
    provider = make(JETON_V4, recorder)
    try:
        await provider.search_series("Severance", None)
        await provider.get_season("42", 2)
        await provider.get_collection("693134")
    finally:
        await provider.aclose()

    assert len(recorder.requests) == 3
    assert recorder.all_authenticated_by_header


def test_detection_du_format() -> None:
    assert TMDBProvider(JETON_V4)._is_bearer is True
    assert TMDBProvider(CLE_V3)._is_bearer is False
