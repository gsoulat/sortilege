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
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0

AUTH_STATUSES = frozenset({401, 403})
"""Codes qui accusent l'identifiant lui-meme, pas la requete.

Distingues des autres erreurs HTTP parce qu'ils ne se resolvent pas en
patientant : un 429 passera au prochain scan, un 401 ne passera jamais tant que
personne n'aura touche la cle."""

_AUTH_ERRORS: dict[str, str] = {}
"""Dernier refus d'authentification, par nom de fournisseur.

Au niveau du module et non du seul objet : un fournisseur est construit puis
ferme a chaque calcul de plans, alors que la question « pourquoi rien ne sort ? »
se pose plus tard, depuis une autre requete. Garde sur l'instance, la reponse
mourrait avec elle."""


def last_auth_error(provider_name: str) -> str:
    """Pourquoi ce fournisseur a refuse l'identifiant, ou chaine vide.

    Le principe 1 veut qu'une panne de fournisseur ne soit jamais fatale ; il ne
    dit pas qu'elle doive etre invisible. Une cle invalide degrade EXACTEMENT
    comme une bibliotheque exotique — « aucun candidat » sur tous les fichiers —
    et rien ne permettait de distinguer les deux.
    """
    return _AUTH_ERRORS.get(provider_name, "")


def forget_auth_errors(provider_name: str | None = None) -> None:
    """Efface la memoire des refus — d'un seul fournisseur, ou de tous.

    Le registre est global au processus : sans remise a zero, un test qui
    simule un 401 contaminerait ceux qui suivent. Effacer un seul fournisseur
    sert quand on s'apprete a le reinterroger : un refus d'hier ne doit pas
    empecher d'essayer une cle corrigee, mais le refus de TheMovieDB, lui,
    doit rester affiche.
    """
    if provider_name is None:
        _AUTH_ERRORS.clear()
    else:
        _AUTH_ERRORS.pop(provider_name, None)


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
    poster_url: str = ""
    """Jaquette, pour l'arbitrage humain.

    Entre deux oeuvres homonymes — « Dark Matter » 2015 et 2024 — une affiche
    tranche en une seconde la ou une date demande de reflechir. C'est le seul
    endroit ou une image a une valeur fonctionnelle et pas decorative."""

    backdrop_url: str = ""
    """Image de fond, large et sans texte. Aucun role dans l'arbitrage — elle
    sert a deposer « fanart.jpg » dans la bibliotheque, ou c'est elle que
    Jellyfin et Plex affichent derriere une fiche."""

    overview: str = ""
    """Resume court. Departage ce que l'affiche ne suffit pas a distinguer."""

    extra: dict[str, Any] = field(default_factory=dict)

    def all_titles(self) -> list[str]:
        """Tous les libelles connus.

        Indispensable : « Very Bad Trip » et « The Hangover » designent le meme
        film. Comparer au seul titre principal ferait echouer la moitie des
        fichiers francais.
        """
        titles = [self.title, self.original_title, *self.aliases]
        return [t for t in titles if t]


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


class SingleFlight:
    """Deduplique les requetes identiques encore en vol.

    Un cache classique ne sert a rien sous concurrence : il n'est rempli
    QU'APRES la reponse. Quand 96 episodes de 6 series sont traites en
    parallele, les 96 consultent le cache avant la premiere reponse, les 96
    manquent, et les 96 partent — le cache n'a rien evite.

    Ici, le premier appelant lance la requete et les suivants attendent LA MEME
    tache. Six requetes au lieu de quatre-vingt-seize, sans changer le code
    appelant.
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    async def do(self, key: str, factory):
        async with self._lock:
            task = self._inflight.get(key)
            if task is None:
                task = asyncio.create_task(factory())
                self._inflight[key] = task

        try:
            # shield : l'annulation d'un appelant ne doit pas priver les autres
            # du resultat qu'ils attendent.
            return await asyncio.shield(task)
        finally:
            async with self._lock:
                if self._inflight.get(key) is task and task.done():
                    del self._inflight[key]


class RateLimiter:
    """Espace les requetes d'un minimum de temps.

    Le semaphore du pipeline borne les appels SIMULTANES ; il ne borne pas le
    debit. Six requetes concurrentes de 50 ms enchainees font 120 requetes par
    seconde, ce qui suffit a se faire limiter. Ce limiteur garantit un
    intervalle minimal entre deux departs, quelle que soit la vitesse des
    reponses.
    """

    def __init__(self, per_second: float = 20.0) -> None:
        self._interval = 1.0 / per_second if per_second > 0 else 0.0
        self._next_slot = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait_for = max(0.0, self._next_slot - now)
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait_for:
            await asyncio.sleep(wait_for)


class BaseHTTPProvider:
    """Socle commun : client HTTP partage, cache, debit borne, pannes absorbees."""

    name: str = "base"

    # TMDB ne publie plus de limite ferme mais bride au-dela d'environ 50
    # requetes par seconde. On reste tres en dessous : un scan un peu plus lent
    # ne coute rien, un bannissement coute une bibliotheque non rangee.
    requests_per_second: float = 20.0

    def __init__(self, client: httpx.AsyncClient | None = None, cache: TTLCache | None = None):
        self._client = client
        self._owns_client = client is None
        self._cache = cache or TTLCache()
        self._limiter = RateLimiter(self.requests_per_second)
        self._flight = SingleFlight()

    async def cached(self, key: str, factory):
        """Cache + deduplication des appels concurrents, en un seul point.

        Les deux vont ensemble : le cache evite de redemander plus tard, la
        deduplication evite de demander cent fois maintenant. Separes, le
        second manque et le premier ne sert qu'aux scans suivants.
        """
        if (hit := await self._cache.get(key)) is not None:
            return hit

        async def run():
            result = await factory()
            await self._cache.set(key, result)
            return result

        return await self._flight.do(key, run)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def last_auth_error(self) -> str:
        """Le refus memorise pour ce fournisseur, ou chaine vide."""
        return last_auth_error(self.name)

    def _auth_message(self, status_code: int) -> str:
        """Ce qu'un humain doit lire pour corriger. Surcharge par les
        fournisseurs qui connaissent un piege precis a signaler."""
        return f"Identifiant {self.name} refusé ({status_code}) : vérifie la clé configurée."

    def _note_auth(self, status_code: int | None) -> None:
        """Tient a jour le registre des refus.

        ``None`` signale une reponse acceptee, et efface un refus anterieur :
        laisser le message apres correction de la cle ferait signaler un
        blocage deja resolu, ce qui use la confiance dans les diagnostics.
        """
        if status_code is None:
            _AUTH_ERRORS.pop(self.name, None)
        elif status_code in AUTH_STATUSES:
            _AUTH_ERRORS[self.name] = self._auth_message(status_code)

    async def _get_json(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        """Requete GET tolerante : renvoie None au lieu de lever.

        Volontaire — voir le principe 1 du module. Un quota depasse ne doit pas
        interrompre le rangement de toute une bibliotheque.
        """
        await self._limiter.wait()
        try:
            response = await self.client.get(url, **kwargs)
            response.raise_for_status()
            self._note_auth(None)
            return response.json()
        except httpx.HTTPStatusError as exc:
            self._note_auth(exc.response.status_code)
            logger.warning("%s : reponse %s pour %s", self.name, exc.response.status_code, url)
        except httpx.HTTPError as exc:
            logger.warning("%s injoignable (%s)", self.name, type(exc).__name__)
        except ValueError:
            logger.warning("%s : reponse illisible (JSON invalide)", self.name)
        return None

    async def _post_json(self, url: str, **kwargs: Any) -> dict[str, Any] | None:
        await self._limiter.wait()
        try:
            response = await self.client.post(url, **kwargs)
            response.raise_for_status()
            self._note_auth(None)
            return response.json()
        except httpx.HTTPStatusError as exc:
            self._note_auth(exc.response.status_code)
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
