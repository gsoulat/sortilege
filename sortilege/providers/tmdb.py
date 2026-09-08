"""TheMovieDB — films et series.

Source principale : c'est aussi celle vers laquelle FileBot a bascule pour les
series apres les restrictions d'API de TheTVDB.

La langue est demandee en francais avec repli anglais : sans cela, une
bibliotheque francaise renvoie des titres anglais et la similarite de titre
s'effondre alors que l'identification est correcte.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date

from .base import BaseHTTPProvider, Candidate

API = "https://api.themoviedb.org/3"

# Les votes TMDB s'etalent de 0 a plusieurs dizaines de milliers. On ecrase sur
# 0 -> 1 avec un plafond : au-dela de ce seuil, « tres connu » suffit, la
# difference entre 5 000 et 40 000 votes n'apporte plus rien au departage.
_POPULARITY_CAP = 2000.0

# w185 : assez grand pour reconnaitre une affiche, assez petit pour en
# charger vingt sans ralentir la page.
IMAGE_BASE = "https://image.tmdb.org/t/p/w185"


@dataclass(frozen=True, slots=True)
class EpisodeInfo:
    """Un episode tel que le fournisseur le connait.

    La date de diffusion n'est pas decorative : sans elle, un episode qui sort
    la semaine prochaine serait compte comme manquant, et la liste des trous
    deviendrait du bruit.
    """

    name: str
    air_date: str = ""

    @property
    def aired(self) -> bool:
        if not self.air_date:
            # Pas de date connue : on suppose diffuse plutot que de masquer un
            # vrai trou. Un faux positif se voit ; un oubli, non.
            return True
        return self.air_date <= date.today().isoformat()


class TMDBProvider(BaseHTTPProvider):
    name = "tmdb"

    def __init__(self, api_key: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self._api_key = api_key
        # Cache dedie : le cache generique stocke des listes de candidats, pas
        # des tables d'episodes.
        self._seasons: dict[tuple[str, int], dict[int, EpisodeInfo] | None] = {}
        self._season_lock = asyncio.Lock()
        self._collections: dict[str, str | None] = {}
        self._collection_lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def _is_bearer(self) -> bool:
        """Le secret fourni est-il un jeton v4 plutot qu'une cle v3 ?

        TMDB propose deux identifiants cote a cote dans les parametres du
        compte : une « Cle d'API » (32 caracteres hexadecimaux, authentification
        v3, en parametre d'URL) et un « Jeton d'acces en lecture » (JWT v4, en
        en-tete Authorization). Ils ne sont pas interchangeables, et se tromper
        donne un 401 sans la moindre explication.

        On accepte donc les deux, en reconnaissant le JWT a sa forme.
        """
        return self._api_key.startswith("eyJ")

    def _headers(self) -> dict[str, str]:
        if self._is_bearer:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _params(self, **extra) -> dict[str, str]:
        base = {
            "language": "fr-FR",
            "include_adult": "false",
            **{k: str(v) for k, v in extra.items() if v is not None},
        }
        # La cle v3 voyage en parametre ; le jeton v4 est deja dans l'en-tete.
        if not self._is_bearer:
            base["api_key"] = self._api_key
        return base

    async def search_movie(self, title: str, year: int | None) -> list[Candidate]:
        if not self.available or not title:
            return []

        async def fetch():
            data = await self._get_json(
                f"{API}/search/movie",
                params=self._params(query=title, year=year),
                headers=self._headers(),
            )
            results = self._to_candidates(data, kind="movie")

            # Un filtre sur l'annee peut vider la reponse alors que l'oeuvre
            # existe (ressortie, erreur de release). On retente sans.
            if not results and year is not None:
                data = await self._get_json(
                    f"{API}/search/movie",
                    params=self._params(query=title),
                    headers=self._headers(),
                )
                results = self._to_candidates(data, kind="movie")
            return results

        return await self.cached(f"movie:{title.lower()}:{year}", fetch)

    async def search_series(self, title: str, year: int | None) -> list[Candidate]:
        if not self.available or not title:
            return []

        async def fetch():
            data = await self._get_json(
                f"{API}/search/tv",
                params=self._params(query=title, first_air_date_year=year),
                headers=self._headers(),
            )
            results = self._to_candidates(data, kind="episode")

            if not results and year is not None:
                data = await self._get_json(
                    f"{API}/search/tv", params=self._params(query=title), headers=self._headers()
                )
                results = self._to_candidates(data, kind="episode")
            return results

        return await self.cached(f"series:{title.lower()}:{year}", fetch)

    async def get_collection(self, movie_id: str) -> str | None:
        """Nom de la saga a laquelle appartient le film, s'il y en a une.

        « Hunger Games - Saga », « La Guerre des étoiles - Saga ». C'est ce qui
        permet de regrouper tous les films d'une franchise dans un dossier.

        Necessite un appel supplementaire : la recherche TMDB ne renvoie pas
        ``belongs_to_collection``, seul le detail du film le porte. D'ou le
        cache — sur une bibliotheque, beaucoup de films partagent une saga.
        """
        async with self._collection_lock:
            if movie_id in self._collections:
                return self._collections[movie_id]

        async def fetch():
            data = await self._get_json(
                f"{API}/movie/{movie_id}", params=self._params(), headers=self._headers()
            )
            name: str | None = None
            if data:
                collection = data.get("belongs_to_collection")
                if isinstance(collection, dict):
                    name = collection.get("name") or None
            async with self._collection_lock:
                self._collections[movie_id] = name
            return name

        return await self._flight.do(f"collection:{movie_id}", fetch)

    async def get_season(self, series_id: str, season: int) -> dict[int, EpisodeInfo] | None:
        """Tous les titres d'episodes d'une saison, en UNE requete.

        C'est le point qui decide du volume total d'appels. Interroger chaque
        episode separement demande une requete par fichier : sur une
        bibliotheque de 400 episodes cela fait 400 requetes la ou 60 suffisent,
        et place le scan dans la zone ou TMDB commence a limiter.

        Le resultat est mis en cache : les 12 episodes d'une saison partagent
        la meme reponse.
        """
        key = (series_id, season)
        async with self._season_lock:
            if key in self._seasons:
                return self._seasons[key]

        async def fetch():
            data = await self._get_json(
                f"{API}/tv/{series_id}/season/{season}",
                params=self._params(),
                headers=self._headers(),
            )
            titles: dict[int, EpisodeInfo] | None = None
            if data and isinstance(data.get("episodes"), list):
                titles = {
                    int(ep["episode_number"]): EpisodeInfo(
                        name=ep.get("name") or "",
                        air_date=ep.get("air_date") or "",
                    )
                    for ep in data["episodes"]
                    if ep.get("episode_number") is not None
                }
            async with self._season_lock:
                # Un None est memorise aussi : une saison inexistante ne doit
                # pas etre redemandee pour chacun de ses pretendus episodes.
                self._seasons[key] = titles
            return titles

        return await self._flight.do(f"season:{series_id}:{season}", fetch)

    async def get_episode_title(self, series_id: str, season: int, episode: int) -> str | None:
        """Titre d'un episode precis, servi depuis la saison mise en cache.

        Sert deux buts : alimenter le jeton {episode_title}, et fournir le
        signal ``episode_match`` — une absence signifie que la saison ou
        l'episode n'existe pas chez ce candidat, donc que l'identification est
        douteuse.
        """
        titles = await self.get_season(series_id, season)
        if not titles:
            return None
        info = titles.get(episode)
        return (info.name if info else "") or None

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
            poster = item.get("poster_path")
            candidates.append(
                Candidate(
                    provider=self.name,
                    external_id=str(item.get("id", "")),
                    title=title or original,
                    original_title=original,
                    year=self._year_from(date),
                    popularity=min(votes / _POPULARITY_CAP, 1.0),
                    kind=kind,
                    poster_url=f"{IMAGE_BASE}{poster}" if poster else "",
                    overview=(item.get("overview") or "")[:220],
                )
            )
        return candidates
