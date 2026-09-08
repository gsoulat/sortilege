"""Conversion de la numerotation absolue des animes.

Les releases de fansub numerotent en continu — « One Piece - 1088 » — la ou
Jellyfin, Plex et le reste du monde attendent S21E13. Sans conversion, un anime
range reste illisible pour le lecteur : chaque episode atterrit hors saison.

La regle est un cumul, mais les cas limites comptent plus que le cas nominal :
une serie en cours a toujours plus d'episodes diffuses que TMDB n'en connait,
et deborder sur une saison inexistante est pire que ne rien affirmer.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from sortilege.providers.base import RateLimiter
from sortilege.providers.tmdb import TMDBProvider

# Trois saisons de 12, 13 et 10 episodes : 35 au total.
SEASONS = [
    {"season_number": 0, "episode_count": 4},  # hors-serie, hors numerotation
    {"season_number": 1, "episode_count": 12},
    {"season_number": 2, "episode_count": 13},
    {"season_number": 3, "episode_count": 10},
]


class Serving:
    def __init__(self, seasons=None, payload=None) -> None:
        self.seasons = SEASONS if seasons is None else seasons
        self.payload = payload
        self.calls = 0

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        body = self.payload if self.payload is not None else {"seasons": self.seasons}
        return httpx.Response(200, json=body)


def make_provider(server: Serving) -> TMDBProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    provider = TMDBProvider("cle-de-test", client=client)
    provider._limiter = RateLimiter(per_second=0)
    return provider


async def resolve(server: Serving, absolute: int):
    provider = make_provider(server)
    try:
        return await provider.resolve_absolute("42", absolute)
    finally:
        await provider.aclose()


# --- Cas nominal ------------------------------------------------------------


@pytest.mark.parametrize(
    ("absolute", "expected"),
    [
        (1, (1, 1)),
        (12, (1, 12)),  # dernier de la saison 1
        (13, (2, 1)),  # premier de la saison 2, la ou tout se joue
        (25, (2, 13)),
        (26, (3, 1)),
        (35, (3, 10)),  # dernier connu
    ],
)
async def test_un_numero_absolu_devient_saison_et_episode(absolute, expected) -> None:
    assert await resolve(Serving(), absolute) == expected


# --- Cas limites ------------------------------------------------------------


async def test_au_dela_du_total_connu_rien_n_est_affirme() -> None:
    """Serie en cours : TMDB ne connait pas encore l'episode 36.

    Deborder sur une « saison 4 » inexistante rangerait le fichier dans un
    dossier fantome, ce qui est plus difficile a rattraper qu'un fichier reste
    en attente de revue.
    """
    assert await resolve(Serving(), 36) is None


@pytest.mark.parametrize("absolute", [0, -3])
async def test_un_numero_invalide_ne_resout_rien(absolute) -> None:
    assert await resolve(Serving(), absolute) is None


async def test_la_saison_zero_ne_compte_pas() -> None:
    """Les hors-serie ne participent pas a la numerotation continue.

    Si les 4 episodes de la saison 0 etaient comptes, l'absolu 1 tomberait sur
    S00E01 et tout le reste serait decale de quatre.
    """
    assert await resolve(Serving(), 1) == (1, 1)


async def test_une_saison_vide_est_traversee() -> None:
    """TMDB annonce parfois une saison a 0 episode (annoncee, non diffusee).

    La sauter plutot que s'y arreter evite de rendre irresolvable tout ce qui
    la suit."""
    seasons = [
        {"season_number": 1, "episode_count": 12},
        {"season_number": 2, "episode_count": 0},
        {"season_number": 3, "episode_count": 10},
    ]
    assert await resolve(Serving(seasons), 13) == (3, 1)


async def test_une_serie_inconnue_ne_resout_rien() -> None:
    assert await resolve(Serving(payload={}), 5) is None


async def test_une_reponse_illisible_ne_leve_pas() -> None:
    """Un champ absent chez le fournisseur ne doit pas interrompre un scan."""
    assert await resolve(Serving(payload={"seasons": "pas une liste"}), 5) is None


# --- Cout -------------------------------------------------------------------


async def test_les_saisons_ne_sont_demandees_qu_une_fois() -> None:
    """Un anime complet, c'est des centaines de fichiers pour une seule serie.

    Une requete par fichier ferait limiter le compte avant la fin du scan."""
    server = Serving()
    provider = make_provider(server)
    try:
        results = await asyncio.gather(*(provider.resolve_absolute("42", n) for n in range(1, 31)))
    finally:
        await provider.aclose()

    assert server.calls == 1
    assert results[0] == (1, 1)
    assert results[25] == (3, 1)
