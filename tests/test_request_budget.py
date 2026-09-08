"""Combien de requetes une vraie bibliotheque declenche-t-elle ?

Un scan qui interroge une fois par fichier se fait limiter par TMDB. Ces tests
MESURENT le volume au niveau du transport HTTP, sur le VRAI fournisseur : une
imitation contournerait precisement les caches qu'on cherche a verifier.

C'est ainsi qu'a ete trouvee une ruee sur le cache — 96 recherches pour 6
series, parce qu'un cache rempli apres la reponse ne sert a rien quand tout
part en parallele.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

import httpx
import pytest

from sortilege.providers.base import RateLimiter, SingleFlight
from sortilege.providers.tmdb import TMDBProvider

SERIES = 6
SEASONS = 2
EPISODES = 8


class Counting:
    """Transport HTTP simule qui compte les requetes par type d'endpoint."""

    def __init__(self, latency: float = 0.01) -> None:
        self.calls: Counter[str] = Counter()
        self.latency = latency

    def label(self, path: str) -> str:
        if "/search/tv" in path:
            return "search_tv"
        if "/search/movie" in path:
            return "search_movie"
        if "/season/" in path:
            return "season"
        return "other"

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls[self.label(path)] += 1
        # Une latence est indispensable : sans elle chaque requete se resout
        # avant que la suivante ne parte, et la concurrence — donc la ruee —
        # ne se produit jamais dans le test.
        await asyncio.sleep(self.latency)

        if "/search/tv" in path:
            return httpx.Response(
                200,
                json={"results": [{"id": 42, "name": "Serie", "first_air_date": "2020-01-01"}]},
            )
        if "/season/" in path:
            return httpx.Response(
                200,
                json={
                    "episodes": [
                        {"episode_number": n, "name": f"Episode {n}"}
                        for n in range(1, EPISODES + 1)
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})


def make_provider(counter: Counting) -> TMDBProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(counter.handle))
    provider = TMDBProvider("cle-de-test", client=client)
    # Le limiteur de debit n'est pas l'objet de ces tests et les rendrait lents.
    provider._limiter = RateLimiter(per_second=0)
    return provider


async def test_les_recherches_concurrentes_ne_partent_qu_une_fois() -> None:
    """La ruee sur le cache : 96 appels simultanes, 6 titres, 6 requetes."""
    counter = Counting()
    provider = make_provider(counter)

    try:
        await asyncio.gather(
            *(
                provider.search_series(f"Serie{i % SERIES}", None)
                for i in range(SERIES * SEASONS * EPISODES)
            )
        )
    finally:
        await provider.aclose()

    assert counter.calls["search_tv"] == SERIES


async def test_une_saison_n_est_demandee_qu_une_fois() -> None:
    """Les 8 episodes d'une saison partagent une seule requete."""
    counter = Counting()
    provider = make_provider(counter)

    try:
        await asyncio.gather(
            *(
                provider.get_episode_title("42", season, episode)
                for season in range(1, SEASONS + 1)
                for episode in range(1, EPISODES + 1)
            )
        )
    finally:
        await provider.aclose()

    assert counter.calls["season"] == SEASONS


async def test_budget_total_d_une_bibliotheque_realiste() -> None:
    """Le volume doit suivre le nombre de SERIES, pas de fichiers."""
    counter = Counting()
    provider = make_provider(counter)
    files = SERIES * SEASONS * EPISODES

    async def one(index: int) -> None:
        serie = f"Serie{index % SERIES}"
        season = (index // SERIES) % SEASONS + 1
        episode = index % EPISODES + 1
        await provider.search_series(serie, None)
        await provider.get_episode_title("42", season, episode)

    try:
        await asyncio.gather(*(one(i) for i in range(files)))
    finally:
        await provider.aclose()

    total = sum(counter.calls.values())
    assert total < files / 5, f"{total} requetes pour {files} fichiers, c'est trop"


async def test_un_second_scan_ne_recoute_rien() -> None:
    """Le cache sert aussi entre deux scans, pas seulement pendant."""
    counter = Counting()
    provider = make_provider(counter)

    try:
        await provider.search_series("Severance", None)
        premier = counter.calls["search_tv"]
        await provider.search_series("Severance", None)
        assert counter.calls["search_tv"] == premier
    finally:
        await provider.aclose()


# --- Briques ----------------------------------------------------------------


async def test_single_flight_partage_le_resultat() -> None:
    flight = SingleFlight()
    appels = 0

    async def lent():
        nonlocal appels
        appels += 1
        await asyncio.sleep(0.02)
        return "valeur"

    resultats = await asyncio.gather(*(flight.do("k", lent) for _ in range(20)))

    assert appels == 1
    assert resultats == ["valeur"] * 20


async def test_single_flight_repart_apres_completion() -> None:
    """Deduplication n'est pas memoisation : une fois fini, on peut redemander."""
    flight = SingleFlight()
    appels = 0

    async def compte():
        nonlocal appels
        appels += 1
        return appels

    await flight.do("k", compte)
    await flight.do("k", compte)
    assert appels == 2


async def test_le_limiteur_espace_les_departs() -> None:
    """Le semaphore borne la concurrence, pas le debit — d'ou ce limiteur."""
    limiter = RateLimiter(per_second=50.0)
    start = time.monotonic()
    for _ in range(5):
        await limiter.wait()
    assert time.monotonic() - start >= 0.07


async def test_un_limiteur_desactive_ne_ralentit_pas() -> None:
    limiter = RateLimiter(per_second=0)
    start = time.monotonic()
    for _ in range(50):
        await limiter.wait()
    assert time.monotonic() - start < 0.05


@pytest.mark.parametrize("per_second", [1.0, 20.0, 100.0])
def test_intervalle_coherent(per_second: float) -> None:
    assert RateLimiter(per_second)._interval == pytest.approx(1.0 / per_second)
