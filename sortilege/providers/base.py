"""Contrat commun aux fournisseurs de metadonnees.

Deux principes qui gouvernent tout ce paquet :

1. **Un fournisseur en panne n'est jamais fatal.** Reseau coupe, quota depasse,
   cle absente : le fournisseur renvoie une liste vide. Le signal
   ``provider_agreement`` baisse, le score baisse, le fichier part en revue
   manuelle. C'est exactement le comportement voulu — degrader vers l'humain,
   jamais vers une decision hasardeuse.

2. **Un fournisseur ne decide rien.** Il propose des candidats bruts. La
   comparaison, la ponderation et le verdict appartiennent a ``core/matching``
   et ``core/scoring``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


@dataclass(slots=True)
class Candidate:
    """Une oeuvre proposee par un fournisseur.

    ``popularity`` est normalise 0 -> 1 par le fournisseur lui-meme : les
    echelles brutes (votes TMDB, score AniList) ne sont pas comparables entre
    sources, et le scoring a besoin d'une grandeur homogene.
    """

    provider: str
    external_id: str
    title: str
    original_title: str = ""
    aliases: list[str] = field(default_factory=list)
    year: int | None = None
    popularity: float = 0.0
    kind: str = "movie"
    extra: dict[str, Any] = field(default_factory=dict)

    def all_titles(self) -> list[str]:
        """Tous les libelles connus.

        Indispensable : « Very Bad Trip » et « The Hangover » designent le meme
        film. Comparer au seul titre principal ferait echouer la moitie des
        fichiers francais.
        """
        titles = [self.title, self.original_title, *self.aliases]
        return [t for t in titles if t]


class Provider(Protocol):
    """Interface minimale attendue de chaque fournisseur."""

    name: str

    async def search_movie(self, title: str, year: int | None) -> list[Candidate]: ...

    async def search_series(self, title: str, year: int | None) -> list[Candidate]: ...


class TTLCache:
    """Cache memoire simple, indispensable pendant un scan.

    Une bibliotheque contient des dizaines d'episodes de la meme serie : sans
    cache on interroge le fournisseur une fois par fichier, on epuise le quota
    et le scan rampe.
    """

    def __init__(self, ttl_seconds: float = 3600.0, max_entries: int = 2000) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[str, tuple[float, list[Candidate]]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> list[Candidate] | None:
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            stored_at, value = entry
            if time.monotonic() - stored_at > self._ttl:
                del self._data[key]
                return None
            return value

    async def set(self, key: str, value: list[Candidate]) -> None:
        async with self._lock:
            if len(self._data) >= self._max:
                # Eviction naive de la plus ancienne : suffisant pour un scan,
                # et evite d'embarquer une dependance LRU.
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (time.monotonic(), value)


class BaseHTTPProvider:
    """Socle commun : client HTTP partage, cache, et absorption des pannes."""

    name: str = "base"

    def __init__(self, client: httpx.AsyncClient | None = None, cache: TTLCache | None = None):
        self._client = client
        self._owns_client = client is None
        self._cache = cache or TTLCache()

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get_json(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        """Requete GET tolerante : renvoie None au lieu de lever.

        Volontaire — voir le principe 1 du module. Un quota depasse ne doit pas
        interrompre le rangement de toute une bibliotheque.
        """
        try:
            response = await self.client.get(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("%s : reponse %s pour %s", self.name, exc.response.status_code, url)
        except httpx.HTTPError as exc:
            logger.warning("%s injoignable (%s)", self.name, type(exc).__name__)
        except ValueError:
            logger.warning("%s : reponse illisible (JSON invalide)", self.name)
        return None

    async def _post_json(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        try:
            response = await self.client.post(url, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("%s : reponse %s pour %s", self.name, exc.response.status_code, url)
        except httpx.HTTPError as exc:
            logger.warning("%s injoignable (%s)", self.name, type(exc).__name__)
        except ValueError:
            logger.warning("%s : reponse illisible (JSON invalide)", self.name)
        return None

    @staticmethod
    def _year_from(value: str | None) -> int | None:
        """Extrait l'annee d'une date ISO ; tolere vide et malforme."""
        if not value or len(value) < 4:
            return None
        try:
            return int(value[:4])
        except ValueError:
            return None
