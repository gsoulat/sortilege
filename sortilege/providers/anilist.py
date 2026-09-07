"""AniList — animes.

Indispensable et non substituable par TMDB : les releases de fansub portent un
titre romaji (« Sousou no Frieren »), parfois anglais, rarement francais, et
numerotent souvent en absolu. AniList expose les trois libelles plus les
synonymes, ce qui est exactement ce dont la comparaison de titres a besoin.

Aucune cle d'API pour la recherche publique.
"""

from __future__ import annotations

from typing import Any

from .base import BaseHTTPProvider, Candidate

API = "https://graphql.anilist.co"

_SEARCH_QUERY = """
query ($search: String) {
  Page(page: 1, perPage: 10) {
    media(search: $search, type: ANIME, sort: SEARCH_MATCH) {
      id
      startDate { year }
      episodes
      popularity
      format
      title { romaji english native }
      synonyms
    }
  }
}
"""

# La popularite AniList se compte en dizaines de milliers pour les titres
# majeurs. Meme logique de plafond que TMDB.
_POPULARITY_CAP = 20000.0


class AniListProvider(BaseHTTPProvider):
    name = "anilist"

    @property
    def available(self) -> bool:
        return True  # pas de cle requise

    async def search_anime(self, title: str, year: int | None = None) -> list[Candidate]:
        if not title:
            return []

        key = f"anime:{title.lower()}"
        if (hit := await self._cache.get(key)) is not None:
            return hit

        data = await self._post_json(
            API,
            json={"query": _SEARCH_QUERY, "variables": {"search": title}},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        results = self._to_candidates(data)
        await self._cache.set(key, results)
        return results

    # L'anime reste une serie : ces alias permettent de traiter un fichier
    # comme l'un ou l'autre sans que l'appelant ait a le savoir.
    async def search_series(self, title: str, year: int | None) -> list[Candidate]:
        return await self.search_anime(title, year)

    async def search_movie(self, title: str, year: int | None) -> list[Candidate]:
        """Les films d'animation existent aussi (Ghibli, Shinkai...)."""
        return [c for c in await self.search_anime(title, year) if c.kind == "movie"]

    def _to_candidates(self, data: dict[str, Any] | None) -> list[Candidate]:
        try:
            media = data["data"]["Page"]["media"]
        except (TypeError, KeyError):
            return []
        if not isinstance(media, list):
            return []

        candidates: list[Candidate] = []
        for item in media:
            titles = item.get("title") or {}
            romaji = titles.get("romaji") or ""
            english = titles.get("english") or ""
            native = titles.get("native") or ""

            principal = romaji or english or native
            if not principal:
                continue

            synonyms = [s for s in (item.get("synonyms") or []) if s]
            aliases = [t for t in (english, native, *synonyms) if t and t != principal]

            popularity = float(item.get("popularity") or 0)
            start = item.get("startDate") or {}

            candidates.append(
                Candidate(
                    provider=self.name,
                    external_id=str(item.get("id", "")),
                    title=principal,
                    original_title=native,
                    aliases=aliases,
                    year=start.get("year"),
                    popularity=min(popularity / _POPULARITY_CAP, 1.0),
                    kind="movie" if item.get("format") == "MOVIE" else "anime",
                    extra={"episode_count": item.get("episodes")},
                )
            )
        return candidates
