"""Utilitaires partages par les tests d'integration.

Dans un module a part plutot que dans l'un des fichiers de test : importer un
test depuis un autre cree un couplage ou renommer un fichier casse l'autre, et
`tests/` n'etant pas un paquet, l'import relatif echoue de toute facon.
"""

from __future__ import annotations

from pathlib import Path

from sortilege.providers.base import Candidate

BIG_ENOUGH = 60 * 1024 * 1024
"""Au-dessus du seuil sous lequel le scanner ecarte les echantillons."""


class FakeTMDB:
    """Fournisseur simule : repond ce qu'on lui dit, et compte ses appels.

    Simuler n'est pas ici un moyen d'eviter le reseau, mais d'eviter qu'un test
    echoue un jour pour une raison etrangere au code — un changement de
    catalogue TMDB, un quota, une panne.
    """

    name = "tmdb"

    def __init__(self, movies=None, series=None, episode_titles=None, collections=None):
        self.movies = movies or {}
        self.series = series or {}
        self.episode_titles = episode_titles or {}
        self.collections = collections or {}
        self.calls = 0

    async def search_movie(self, title, year):
        self.calls += 1
        return self.movies.get(title.lower(), [])

    async def search_series(self, title, year):
        self.calls += 1
        return self.series.get(title.lower(), [])

    async def get_episode_title(self, series_id, season, episode):
        return self.episode_titles.get((series_id, season, episode))

    async def get_collection(self, movie_id):
        return self.collections.get(movie_id)

    async def aclose(self):
        pass


def cand(**kw) -> Candidate:
    base = dict(provider="tmdb", external_id="1", title="X", year=2024, popularity=0.8)
    base.update(kw)
    return Candidate(**base)


def big_file(path: Path) -> Path:
    """Cree un fichier creux assez gros pour ne pas etre pris pour un extrait."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(BIG_ENOUGH)
        handle.write(b"\0")
    return path
