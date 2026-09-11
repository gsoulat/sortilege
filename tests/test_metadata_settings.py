"""La cle TheMovieDB et la langue, reglables depuis l'interface.

C'etait le dernier endroit ou Sortilege restait inutilisable sans editer un
fichier puis redemarrer la pile — pour la cle qui conditionne TOUTE
l'identification, donc des le premier geste d'une installation.

Le point qui decide si la bascule tient est l'ordre de priorite. Les
preferences doivent l'emporter sur l'environnement, sinon la saisie est
acceptee, enregistree et sans effet sur toute installation qui a un .env : le
pire des trois etats possibles. Et l'environnement doit rester lu quand les
preferences sont vides, sinon la mise a jour arrete d'identifier chez tous ceux
qui tournaient deja.
"""

from __future__ import annotations

import copy

import httpx
import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps
from sortilege.api import settings as api_settings
from sortilege.api.deps import get_store, tmdb_key, tmdb_language
from sortilege.core import vpn
from sortilege.core.preferences import PreferenceError
from sortilege.main import app
from sortilege.providers.base import Candidate, RateLimiter
from sortilege.providers.tmdb import TMDBProvider

CLE_ENV = "cle-venue-de-l-environnement"
CLE_PREFS = "cle-venue-des-preferences"


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


@pytest.fixture(autouse=True)
def store():
    """Rend ses preferences au magasin apres chaque test.

    Le magasin est memoise pour tout le processus : un test qui y ecrit une cle
    la laisserait aux suivants, et l'ordre de collecte deciderait du resultat.
    La copie est profonde parce que ``load`` sert l'objet du cache, que les
    tests modifient en place.
    """
    magasin = get_store()
    avant = copy.deepcopy(magasin.load())
    yield magasin
    magasin.save(avant)


def enregistre(magasin, **champs) -> None:
    prefs = magasin.load()
    for nom, valeur in champs.items():
        setattr(prefs.metadata, nom, valeur)
    magasin.save(prefs)


# --- L'ordre de priorite -----------------------------------------------------


def test_la_cle_des_preferences_prime_sur_l_environnement(store, monkeypatch) -> None:
    monkeypatch.setattr(deps.get_settings(), "tmdb_api_key", CLE_ENV, raising=False)
    enregistre(store, tmdb_api_key=CLE_PREFS)

    assert tmdb_key() == CLE_PREFS


def test_sans_cle_en_preferences_on_retombe_sur_l_environnement(store, monkeypatch) -> None:
    """Une installation qui porte sa cle dans son .env depuis le premier jour ne
    doit pas s'arreter d'identifier le jour de la mise a jour."""
    monkeypatch.setattr(deps.get_settings(), "tmdb_api_key", CLE_ENV, raising=False)
    enregistre(store, tmdb_api_key="")

    assert tmdb_key() == CLE_ENV


def test_une_cle_faite_d_espaces_ne_masque_pas_l_environnement(store, monkeypatch) -> None:
    """Sans le rognage, un champ ou l'on a laisse un espace couperait
    l'identification tout en s'affichant comme configure."""
    monkeypatch.setattr(deps.get_settings(), "tmdb_api_key", CLE_ENV, raising=False)
    enregistre(store, tmdb_api_key="   ")

    assert tmdb_key() == CLE_ENV


def test_sans_cle_nulle_part_la_valeur_est_vide(store, monkeypatch) -> None:
    monkeypatch.setattr(deps.get_settings(), "tmdb_api_key", "", raising=False)
    enregistre(store, tmdb_api_key="")

    assert tmdb_key() == ""


# --- L'API -------------------------------------------------------------------


def test_la_cle_ne_redescend_jamais_au_navigateur(client: TestClient) -> None:
    """Cette reponse finit dans le cache du navigateur et dans sa console."""
    r = client.put(
        "/api/settings/preferences",
        json={"metadata": {"tmdb_api_key": CLE_PREFS, "language": "fr-FR"}},
    )

    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["tmdb_key_set"] is True
    assert "tmdb_api_key" not in meta
    assert CLE_PREFS not in r.text


def test_une_cle_vide_conserve_celle_qui_est_enregistree(client: TestClient, store) -> None:
    """L'interface renvoie le formulaire entier a chaque enregistrement sans
    connaitre la valeur actuelle : sans cette regle, changer la langue
    effacerait la cle."""
    client.put("/api/settings/preferences", json={"metadata": {"tmdb_api_key": CLE_PREFS}})

    r = client.put("/api/settings/preferences", json={"metadata": {"language": "en-US"}})

    assert r.json()["metadata"]["tmdb_key_set"] is True
    assert r.json()["metadata"]["language"] == "en-US"
    assert store.load().metadata.tmdb_api_key == CLE_PREFS


def test_un_tiret_vide_explicitement_la_cle(client: TestClient, store) -> None:
    """Sans ce geste, une cle saisie par erreur ne pourrait plus jamais etre
    retiree depuis l'interface."""
    client.put("/api/settings/preferences", json={"metadata": {"tmdb_api_key": CLE_PREFS}})

    r = client.put("/api/settings/preferences", json={"metadata": {"tmdb_api_key": "-"}})

    assert r.json()["metadata"]["tmdb_key_set"] is False
    assert store.load().metadata.tmdb_api_key == ""


def test_l_origine_de_la_cle_est_annoncee(client: TestClient, monkeypatch) -> None:
    """Un champ vide a cote d'une application qui identifie parfaitement invite
    a ressaisir une cle deja presente ailleurs."""
    monkeypatch.setattr(deps.get_settings(), "tmdb_api_key", CLE_ENV, raising=False)
    client.put("/api/settings/preferences", json={"metadata": {"tmdb_api_key": "-"}})

    meta = client.get("/api/settings/preferences").json()["metadata"]

    assert meta["tmdb_key_set"] is False
    assert meta["tmdb_key_from_env"] is True


def test_le_diagnostic_voit_la_cle_des_preferences(client: TestClient, monkeypatch) -> None:
    """Il doit dire si l'identification PEUT fonctionner, pas si le .env est
    rempli — les deux ont cesse d'etre la meme question."""
    monkeypatch.setattr(deps.get_settings(), "tmdb_api_key", "", raising=False)
    client.put("/api/settings/preferences", json={"metadata": {"tmdb_api_key": CLE_PREFS}})

    reglages = client.get("/api/settings").json()

    tmdb = next(p for p in reglages["providers"] if p["name"] == "TheMovieDB")
    assert tmdb["configured"] is True
    assert next(c for c in reglages["diagnostics"] if c["name"] == "TheMovieDB")["ok"] is True


def test_la_taille_de_lot_ia_a_quitte_les_preferences(client: TestClient) -> None:
    """Personne ne pouvait la decider : elle n'apparaissait sur aucun ecran."""
    assert "batch_size" not in client.get("/api/settings/preferences").json()["ai"]


# --- La langue ---------------------------------------------------------------


def test_la_langue_vaut_le_francais_par_defaut(store) -> None:
    assert tmdb_language() == "fr-FR"


async def test_la_langue_part_dans_les_requetes() -> None:
    """Le titre renvoye est celui qu'on compare au nom du fichier : demander la
    mauvaise langue fait s'effondrer le score d'identifications pourtant
    justes."""
    vues: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        vues.append(request)
        return httpx.Response(200, json={"results": []})

    provider = TMDBProvider(
        "cle", "ja-JP", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    provider._limiter = RateLimiter(per_second=0)
    try:
        await provider.search_movie("Akira", None)
    finally:
        await provider.aclose()

    assert vues and "language=ja-JP" in str(vues[0].url)


def test_une_langue_vide_retombe_sur_le_defaut() -> None:
    """TMDB accepterait un parametre vide et l'ignorerait : on recevrait des
    titres anglais sans qu'aucun message ne dise pourquoi."""
    assert TMDBProvider("cle", "  ")._language == "fr-FR"


def test_une_langue_mal_formee_est_refusee(store) -> None:
    prefs = store.load()
    prefs.metadata.language = "francais"

    with pytest.raises(PreferenceError, match="langue"):
        store.save(prefs)


# --- L'essai de la cle -------------------------------------------------------


def test_l_essai_refuse_avant_toute_requete_quand_rien_n_est_enregistre(
    client: TestClient, monkeypatch
) -> None:
    """Un 502 laisserait croire a une panne de TheMovieDB, alors que rien n'a
    ete envoye."""
    monkeypatch.setattr(deps.get_settings(), "tmdb_api_key", "", raising=False)
    client.put("/api/settings/preferences", json={"metadata": {"tmdb_api_key": "-"}})

    r = client.post("/api/settings/metadata/test")

    assert r.status_code == 400
    assert "clé" in r.json()["detail"].lower()


def test_l_essai_interroge_reellement_le_fournisseur(client: TestClient, monkeypatch) -> None:
    """Une cle bien formee peut etre refusee : seule une vraie requete le dit."""
    appels: list[str] = []

    class Faux:
        def __init__(self, cle, langue, **kw):
            appels.append(f"{cle}/{langue}")

        async def search_movie(self, titre, annee):
            return [Candidate(provider="tmdb", external_id="1", title="Dune", year=2024)]

        async def aclose(self):
            pass

    monkeypatch.setattr(api_settings, "TMDBProvider", Faux)
    client.put(
        "/api/settings/preferences",
        json={"metadata": {"tmdb_api_key": CLE_PREFS, "language": "en-US"}},
    )

    r = client.post("/api/settings/metadata/test")

    assert r.status_code == 200
    assert r.json() == {"ok": True, "language": "en-US", "sample": "Dune"}
    assert appels == [f"{CLE_PREFS}/en-US"]


def test_une_cle_refusee_est_rapportee_avec_son_motif(client: TestClient, monkeypatch) -> None:
    """Le fournisseur nomme la confusion v3/v4, seule cause frequente ici : la
    remplacer par un message generique perdrait le seul indice utile."""

    class Refuse:
        def __init__(self, cle, langue, **kw):
            pass

        async def search_movie(self, titre, annee):
            return []

        async def aclose(self):
            pass

    monkeypatch.setattr(api_settings, "TMDBProvider", Refuse)
    monkeypatch.setattr(api_settings, "last_auth_error", lambda nom: "Clé refusée (401) : v3/v4")
    client.put("/api/settings/preferences", json={"metadata": {"tmdb_api_key": CLE_PREFS}})

    r = client.post("/api/settings/metadata/test")

    assert r.status_code == 502
    assert "401" in r.json()["detail"]


# --- Sortie reseau des boutons d'essai ---------------------------------------


@pytest.fixture
def sortie_refusee(store, monkeypatch):
    """Politique « exiger » et une mesure de sortie qui echoue.

    La mesure est fabriquee : aucun de ces tests ne touche le reseau. Chaque
    bouton recoit de quoi passer ses propres controles locaux, pour que le refus
    observe soit bien celui de la sortie et pas une cle manquante.
    """

    async def _panne(*args, **kwargs):
        return vpn.Measure(error="service d'echo injoignable")

    monkeypatch.setattr(vpn, "measure_egress", _panne)
    prefs = store.load()
    prefs.vpn.policy = vpn.Policy.REQUIRE
    prefs.metadata.tmdb_api_key = CLE_PREFS
    prefs.subtitles.opensubtitles_api_key = "cle-opensubtitles-de-test"
    prefs.notifications.webhook_url = "https://discord.com/api/webhooks/1/jeton-de-test"
    prefs.ai.enabled = True
    prefs.ai.api_key = "cle-ia-de-test"
    prefs.media_server.base_url = "http://192.168.1.10:8096"
    prefs.media_server.api_key = "cle-jellyfin-de-test"
    store.save(prefs)
    yield
    vpn.reset_cache()


def _interdit(nom: str, appels: list[str]):
    def _appel(*args, **kwargs):
        appels.append(nom)
        raise AssertionError(f"{nom} appele malgre le refus de sortie")

    return _appel


def test_les_boutons_d_essai_sont_refuses_en_409_sous_exiger(
    client: TestClient, sortie_refusee, monkeypatch
) -> None:
    """Un essai est un appel sortant comme un autre : sous « exiger », le laisser
    passer ferait du bouton la fuite exacte que la politique doit empecher."""
    appels: list[str] = []
    monkeypatch.setattr(api_settings, "TMDBProvider", _interdit("tmdb", appels))
    monkeypatch.setattr(api_settings, "send", _interdit("discord", appels))
    monkeypatch.setattr(api_settings, "resolver_status", lambda *a, **k: (True, ""))
    monkeypatch.setattr("sortilege.core.ai.build_resolver", _interdit("ia", appels))
    monkeypatch.setattr(
        "sortilege.providers.opensubtitles.OpenSubtitlesProvider",
        _interdit("opensubtitles", appels),
    )

    for route in ("metadata/test", "subtitles/test", "ai/test", "notifications/test"):
        r = client.post(f"/api/settings/{route}")
        assert r.status_code == 409, f"{route} : {r.status_code} {r.text}"
        assert "tunnel n'est pas confirmée" in r.json()["detail"], route
    assert appels == []


def test_le_serveur_multimedia_et_la_mesure_restent_permis_sous_exiger(
    client: TestClient, sortie_refusee, monkeypatch
) -> None:
    """Deux exceptions, et seulement deux : le reseau local, et la mesure elle-meme."""

    async def _rafraichi(*args, **kwargs):
        return True

    monkeypatch.setattr(api_settings, "refresh_library", _rafraichi)
    r = client.post("/api/settings/media-server/test")
    assert r.status_code == 200
    assert r.json() == {"refreshed": True}

    r = client.post("/api/settings/vpn/test")
    assert r.status_code == 200
    assert r.json()["verdict"] == "unavailable"


def test_la_mesure_ne_propose_plus_d_adopter_l_adresse_comme_temoin(
    client: TestClient, store, monkeypatch
) -> None:
    """Un verdict « inconnu » ne dit pas que la sortie est en clair : un VPN actif
    chez un fournisseur non reconnu donne le meme. Proposer d'adopter l'adresse
    mesuree enregistrait alors celle DU TUNNEL comme adresse de la maison, et une
    sortie en clair etait ensuite dite protegee."""
    prefs = store.load()
    prefs.vpn.reference_ip = ""
    store.save(prefs)

    async def _inconnu(*args, **kwargs):
        return vpn.Measure(
            public_ip="203.0.113.7",
            organization="Hebergeur Sans Nom SAS",
            interfaces=("wg0",),
            interfaces_readable=True,
        )

    monkeypatch.setattr(vpn, "measure_egress", _inconnu)
    corps = client.post("/api/settings/vpn/test").json()
    assert corps["verdict"] == "unknown"
    assert "offer_as_reference" not in corps


def test_l_essai_des_sous_titres_exige_la_cle_et_pas_seulement_le_jeton(
    client: TestClient, store, monkeypatch
) -> None:
    prefs = store.load()
    prefs.subtitles.opensubtitles_api_key = ""
    prefs.subtitles.opensubtitles_token = "jeton-vip-de-test"
    store.save(prefs)
    appels: list[str] = []
    monkeypatch.setattr(
        "sortilege.providers.opensubtitles.OpenSubtitlesProvider",
        _interdit("opensubtitles", appels),
    )

    r = client.post("/api/settings/subtitles/test")

    assert r.status_code == 400
    assert "jeton VIP seul ne suffit pas" in r.json()["detail"]
    assert appels == []


def test_changer_la_politique_vpn_oublie_la_derniere_mesure(
    client: TestClient, monkeypatch
) -> None:
    """La mesure en cache a ete jugee sous l'ancien reglage : elle ne doit pas
    survivre une minute de plus au changement."""
    monkeypatch.setattr(vpn, "_cache", vpn.Measure(public_ip="203.0.113.7"))
    r = client.put("/api/settings/preferences", json={"vpn": {"policy": "warn"}})
    assert r.status_code == 200
    assert vpn._cache is None
