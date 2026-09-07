"""TheMovieDB — films et series.

Source principale : c'est aussi celle vers laquelle FileBot a bascule pour les
series apres les restrictions d'API de TheTVDB.

La langue est demandee en francais avec repli anglais : sans cela, une
bibliotheque francaise renvoie des titres anglais et la similarite de titre
s'effondre alors que l'identification est correcte.
"""

from __future__ import annotations

from .base import BaseHTTPProvider, Candidate

API = "https://api.themoviedb.org/3"

# Les votes TMDB s'etalent de 0 a plusieurs dizaines de milliers. On ecrase sur
# 0 -> 1 avec un plafond : au-dela de ce seuil, « tres connu » suffit, la
# difference entre 5 000 et 40 000 votes n'apporte plus rien au departage.
_POPULARITY_CAP = 2000.0


class TMDBProvider(BaseHTTPProvider):
    name = "tmdb"

    def __init__(self, api_key: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._api_key = api_key

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _params(self, **extra) -> dict[str, str]:
        return {
            "api_key": self._api_key,
            "language": "fr-FR",
            "include_adult": "false",
            **{k: str(v) for k, v in extra.items() if v is not None},
        }

    async def search_movie(self, title: str, year: int | None) -> list[Candidate]:
        if not self.available or not title:
            return []

        key = f"movie:{title.lower()}:{year}"
        if (hit := await self._cache.get(key)) is not None:
            return hit

        data = await self._get_json(
            f"{API}/search/movie", params=self._params(query=title, year=year)
        )
        results = self._to_candidates(data, kind="movie")

        # Un filtre sur l'annee peut vider la reponse alors que l'oeuvre existe
        # (annee de ressortie, erreur de release). On retente sans.
        if not results and year is not None:
            data = await self._get_json(f"{API}/search/movie", params=self._params(query=title))
            results = self._to_candidates(data, kind="movie")

        await self._cache.set(key, results)
        return results

    async def search_series(self, title: str, year: int | None) -> list[Candidate]:
        if not self.available or not title:
            return []

        key = f"series:{title.lower()}:{year}"
        if (hit := await self._cache.get(key)) is not None:
            return hit

        data = await self._get_json(
            f"{API}/search/tv", params=self._params(query=title, first_air_date_year=year)
        )
        results = self._to_candidates(data, kind="episode")

        if not results and year is not None:
            data = await self._get_json(f"{API}/search/tv", params=self._params(query=title))
            results = self._to_candidates(data, kind="episode")

        await self._cache.set(key, results)
        return results

    async def get_collection(self, movie_id: str) -> str | None:
        """Nom de la saga a laquelle appartient le film, s'il y en a une.

        « Hunger Games - Saga », « La Guerre des étoiles - Saga ». C'est ce qui
        permet de regrouper tous les films d'une franchise dans un dossier.

        Necessite un appel supplementaire : la recherche TMDB ne renvoie pas
        ``belongs_to_collection``, seul le detail du film le porte. D'ou le
        cache — sur une bibliotheque, beaucoup de films partagent une saga.
        """
        data = await self._get_json(f"{API}/movie/{movie_id}", params=self._params())
        if not data:
            return None
        collection = data.get("belongs_to_collection")
        if not isinstance(collection, dict):
            return None
        return collection.get("name") or None

    async def get_episode_title(self, series_id: str, season: int, episode: int) -> str | None:
        """Titre d'un episode precis.

        Sert deux buts : alimenter le jeton {episode_title}, et fournir le
        signal ``episode_match`` — un 404 signifie que la saison ou l'episode
        n'existe pas chez ce candidat, donc que l'identification est douteuse.
        """
        data = await self._get_json(
            f"{API}/tv/{series_id}/season/{season}/episode/{episode}", params=self._params()
        )
        if not data:
            return None
        return data.get("name") or None

    def _to_candidates(self, data: dict | None, kind: str) -> list[Candidate]:
        if not data or not isinstance(data.get("results"), list):
            return []

        candidates: list[Candidate] = []
        for item in data["results"][:10]:
            if kind == "movie":
                title = item.get("title") or ""
                original = item.get("original_title") or ""
                date = item.get("release_date")
            else:
                title = item.get("name") or ""
                original = item.get("original_name") or ""
                date = item.get("first_air_date")

            if not title and not original:
                continue

            votes = float(item.get("vote_count") or 0)
            candidates.append(
                Candidate(
                    provider=self.name,
                    external_id=str(item.get("id", "")),
                    title=title or original,
                    original_title=original,
                    year=self._year_from(date),
                    popularity=min(votes / _POPULARITY_CAP, 1.0),
                    kind=kind,
                    extra={"overview": item.get("overview", "")[:200]},
                )
            )
        return candidates
