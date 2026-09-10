"""Dire POURQUOI rien ne sort.

Toutes les pannes de configuration de Sortilege se ressemblent vues de
l'interface : une liste vide. Racine demontee, cle TheMovieDB refusee, scan
jamais lance — trois causes, un seul symptome, et le meme message rassurant
« rien ne traine dans la source, c'est l'etat recherche ».

Ces tests portent sur ce qui distingue les trois. Ils ne verifient pas qu'une
panne est detectee — elle l'etait deja, dans les journaux — mais qu'elle
ARRIVE jusqu'a l'ecran.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from sortilege.api import library as api_library
from sortilege.api import review as api_review
from sortilege.api import workspace as api_workspace
from sortilege.core.pipeline import Pipeline
from sortilege.core.scanner import ScanResult, scan
from sortilege.core.scoring import Policy
from sortilege.core.template import PRESETS
from sortilege.providers import base as providers_base
from sortilege.providers.base import RateLimiter
from sortilege.providers.tmdb import TMDBProvider

CLE_V3 = "0123456789abcdef0123456789abcdef"
JETON_V4 = "eyJhbGciOiJIUzI1NiJ9.charge-utile-factice.signature-factice"


@pytest.fixture(autouse=True)
def _memoire_des_refus_vierge():
    """Le registre des refus est global au processus.

    Sans remise a zero, un test qui simule un 401 ferait echouer les suivants —
    et l'ordre d'execution deciderait du resultat.
    """
    providers_base.forget_auth_errors()
    yield
    providers_base.forget_auth_errors()


# --- Ce que le scan n'a pas pu faire -----------------------------------------


def test_une_racine_absente_est_une_erreur_pas_un_scan_vide() -> None:
    resultat = scan([Path("/racine/qui/n/existe/pas")], deep=False)

    assert resultat.total == 0
    assert any("racine introuvable" in e for e in resultat.errors)


def test_l_etat_du_scan_porte_les_erreurs_et_les_ecartes(monkeypatch) -> None:
    """Sans ces deux champs, « 0 fichier » etait indistinguable d'une source
    propre — c'est precisement le cas que l'interface annonçait comme reussi."""
    monkeypatch.setattr(
        api_library._job,
        "result",
        ScanResult(errors=["racine introuvable : /storage/downloads"], skipped=4),
    )

    etat = api_library.scan_status()

    assert etat["errors"] == ["racine introuvable : /storage/downloads"]
    assert etat["skipped"] == 4
    assert etat["has_result"] is True


def test_avant_tout_scan_l_etat_le_dit(monkeypatch) -> None:
    monkeypatch.setattr(api_library._job, "result", None)

    etat = api_library.scan_status()

    assert etat["has_result"] is False, "zero fichier avant scan n'est pas zero fichier apres"
    assert etat["errors"] == []
    assert etat["skipped"] == 0


def test_la_vue_unifiee_transporte_les_diagnostics(monkeypatch) -> None:
    """La vue principale doit pouvoir afficher la cause SANS second appel : une
    liste vide et sa raison qui arrivent separement laissent un intervalle ou
    l'ecran affirme que tout va bien."""
    monkeypatch.setattr(
        api_library,
        "last_scan",
        lambda: ScanResult(errors=["racine introuvable : /storage/downloads"], skipped=2),
    )
    monkeypatch.setattr(api_review, "current_plans", list)
    monkeypatch.setattr(api_workspace.collection, "current_works", list)

    sortie = api_workspace.read_workspace()

    assert sortie["diagnostics"]["scanned"] is True
    assert sortie["diagnostics"]["errors"] == ["racine introuvable : /storage/downloads"]
    assert sortie["diagnostics"]["skipped"] == 2


def test_la_vue_unifiee_transporte_les_blocages(monkeypatch) -> None:
    monkeypatch.setattr(api_library, "last_scan", lambda: None)
    # Le module de revue importe last_scan par valeur : patcher celui de la
    # bibliotheque ne le rebranche pas, et un scan laisse par un autre test
    # ferait disparaitre le blocage attendu.
    monkeypatch.setattr(api_review, "last_scan", lambda: None)
    monkeypatch.setattr(api_review, "current_plans", list)
    monkeypatch.setattr(api_workspace.collection, "current_works", list)

    sortie = api_workspace.read_workspace()

    codes = {b["code"] for b in sortie["blockers"]}
    assert "no_scan" in codes
    assert all({"code", "message", "where"} <= set(b) for b in sortie["blockers"])


# --- La cle TheMovieDB -------------------------------------------------------


async def refuse(request: httpx.Request) -> httpx.Response:
    """Repond 401 a tout, comme TMDB devant un identifiant invalide."""
    return httpx.Response(401, json={"status_message": "Invalid API key"})


def fournisseur(secret: str, handler) -> TMDBProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = TMDBProvider(secret, client=client)
    provider._limiter = RateLimiter(per_second=0)
    return provider


async def test_une_cle_refusee_laisse_une_explication() -> None:
    """Le principe 1 veut qu'un fournisseur en panne ne soit pas fatal ; il ne
    dit pas qu'il doive etre muet. Sans ce message, un 401 se lit « aucun
    candidat » sur toute la bibliotheque."""
    provider = fournisseur(CLE_V3, refuse)
    try:
        assert await provider.search_movie("Dune", 2024) == []
    finally:
        await provider.aclose()

    assert "401" in provider.last_auth_error
    assert "clé API v3" in provider.last_auth_error, "la forme lue doit etre nommee"


async def test_le_message_nomme_la_forme_reellement_lue() -> None:
    """Regenerer trois fois le meme identifiant ne sert a rien quand c'est
    l'AUTRE qu'il fallait copier."""
    provider = fournisseur(JETON_V4, refuse)
    try:
        await provider.search_movie("Dune", 2024)
    finally:
        await provider.aclose()

    assert "jeton d'accès v4" in provider.last_auth_error


async def test_le_refus_survit_a_la_fermeture_du_fournisseur() -> None:
    """Un fournisseur est construit puis ferme a chaque calcul de plans ; la
    question « pourquoi rien ne sort ? » se pose bien plus tard."""
    provider = fournisseur(CLE_V3, refuse)
    try:
        await provider.search_movie("Dune", 2024)
    finally:
        await provider.aclose()
    del provider

    assert providers_base.last_auth_error("tmdb")


async def test_une_reponse_acceptee_efface_le_refus() -> None:
    """Signaler un blocage deja corrige use la confiance dans les diagnostics."""
    # Une bascule explicite plutot qu'un compteur d'appels : une recherche
    # TMDB relance sans l'annee quand elle ne trouve rien, donc un « seulement
    # le premier appel » basculerait au milieu de la meme recherche.
    cle_corrigee = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if cle_corrigee:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(401, json={"status_message": "Invalid API key"})

    provider = fournisseur(CLE_V3, handler)
    try:
        await provider.search_movie("Dune", 2024)
        assert provider.last_auth_error, "le refus doit d'abord etre memorise"

        cle_corrigee = True
        await provider.search_movie("Arrival", 2016)
    finally:
        await provider.aclose()

    assert provider.last_auth_error == ""


# --- Les blocages, tels que l'interface les recoit ---------------------------


def blocage(codes: list[dict[str, str]], code: str) -> dict[str, str] | None:
    return next((b for b in codes if b["code"] == code), None)


def test_une_cle_absente_est_signalee(monkeypatch) -> None:
    monkeypatch.setattr(api_review.get_settings(), "tmdb_api_key", "", raising=False)

    trouve = blocage(api_review.blockers(), "tmdb_key_missing")

    assert trouve is not None
    assert "TMDB_API_KEY" in trouve["where"], "un blocage sans adresse laisse chercher"


def test_une_cle_refusee_est_signalee_a_part(monkeypatch) -> None:
    """Distinct de l'absence de cle : les deux produisent le meme silence, mais
    pas le meme geste correctif."""
    monkeypatch.setattr(api_review.get_settings(), "tmdb_api_key", CLE_V3, raising=False)
    monkeypatch.setattr(
        api_review, "last_auth_error", lambda nom: "Clé TheMovieDB refusée (401) : …"
    )

    blocages = api_review.blockers()

    assert blocage(blocages, "tmdb_key_missing") is None
    refuse = blocage(blocages, "tmdb_key_refused")
    assert refuse is not None
    assert "401" in refuse["message"]


def test_aucune_racine_accessible_est_signalee(monkeypatch) -> None:
    monkeypatch.setattr(api_review, "get_store", lambda: _StoreFactice([Path("/racine/demontee")]))

    assert blocage(api_review.blockers(), "no_source_root") is not None


def test_une_racine_accessible_ne_declenche_rien(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(api_review, "get_store", lambda: _StoreFactice([tmp_path]))

    assert blocage(api_review.blockers(), "no_source_root") is None


class _StoreFactice:
    def __init__(self, racines: list[Path]) -> None:
        self._racines = racines

    def resolved_sources(self) -> list[Path]:
        return self._racines


# --- La fermeture du resolveur IA --------------------------------------------


class ResolveurQuiSeFerme:
    name = "factice"

    def __init__(self) -> None:
        self.ferme = False

    def resolve(self, items):
        return {}

    def close(self) -> None:
        self.ferme = True


class ResolveurSansFermeture:
    """Un double de test minimal, comme ceux qui existent deja dans la suite."""

    name = "minimal"

    def resolve(self, items):
        return {}


def pipeline(ai, lib: Path) -> Pipeline:
    return Pipeline(
        tmdb=None,
        anilist=None,
        library_root=lib,
        templates={k: PRESETS["jellyfin"][k] for k in ("movie", "episode", "anime")},
        policy=Policy(auto_apply_threshold=0.92, reject_threshold=0.40),
        ai=ai,
    )


async def test_fermer_le_pipeline_ferme_le_resolveur(tmp_path) -> None:
    """Un resolveur est construit a chaque calcul de plans ET a chaque cycle
    automatique — toutes les quinze minutes. Sans fermeture, une application qui
    tourne en permanence accumule les clients HTTP jusqu'a manquer de
    descripteurs."""
    resolveur = ResolveurQuiSeFerme()

    await pipeline(resolveur, tmp_path).aclose()

    assert resolveur.ferme


async def test_un_resolveur_sans_fermeture_ne_casse_rien(tmp_path) -> None:
    """aclose() est appelee depuis un bloc finally : une exception ici ferait
    perdre les plans deja calcules."""
    await pipeline(ResolveurSansFermeture(), tmp_path).aclose()


async def test_un_pipeline_sans_resolveur_se_ferme(tmp_path) -> None:
    await pipeline(None, tmp_path).aclose()


# --- Le menage promis au demarrage -------------------------------------------


def test_le_demarrage_ramasse_les_encodages_interrompus() -> None:
    """Deux commentaires promettaient ce menage, personne ne le faisait.

    Un ffmpeg tue par un redemarrage laisse un fichier partiel de plusieurs
    gigaoctets, et la file de travaux ne survit pas au processus : plus rien ne
    reclame ce fichier, il occupait donc indefiniment la place qu'on cherchait
    justement a liberer.
    """
    from fastapi.testclient import TestClient

    from sortilege.config import get_settings
    from sortilege.core.transcode import staging_root
    from sortilege.main import app

    attente = staging_root(get_settings().library_root)
    attente.mkdir(parents=True, exist_ok=True)
    orphelin = attente / "reencodage-interrompu.mkv"
    orphelin.write_bytes(b"resultat partiel")

    with TestClient(app):
        pass

    assert not orphelin.exists()
