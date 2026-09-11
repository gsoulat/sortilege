"""La politique « exiger » tenue jusqu'au bout : rien ne part, rien ne trahit la maison.

Avant ces correctifs, un tunnel tombe sous « exiger » publiait l'adresse
publique de la maison sur Discord a chaque cycle — par la sortie meme qu'on
venait de juger non protegee. Ces tests fixent les verrous poses depuis : aucun
message sur un refus, un rapport de cycle sans adresse, un masquage a l'envoi,
une garde de sortie la ou du trafic partait encore, et les deux etapes que le
cycle automatique oubliait (sous-titres, serveur multimedia).

Aucun test ne touche le reseau : la mesure est fabriquee, l'envoi espionne.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sortilege.api import automation, collection, review
from sortilege.api.deps import get_store
from sortilege.core import vpn
from sortilege.core.notify import ADRESSE_MASQUEE, Notification, send
from sortilege.core.planner import Plan
from sortilege.core.preferences import AutomationSettings, Preferences
from sortilege.core.scoring import Decision
from sortilege.main import app

IP_MAISON = "88.120.4.17"
"""Reconnaissable : la retrouver dans un message ou un rapport est une fuite."""

WEBHOOK = "https://discord.com/api/webhooks/123456/abcdef"
GB = 1024**3


# --- Outillage ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mesure_fabriquee(monkeypatch: pytest.MonkeyPatch):
    """Une sortie mesuree par la connexion domestique, sans aucune requete."""

    async def fausse_mesure(client: httpx.AsyncClient | None) -> vpn.Measure:
        return vpn.Measure(
            public_ip=IP_MAISON,
            organization="AS3215 Orange S.A.",
            echo_service="test",
            interfaces_readable=True,
            at=time.monotonic(),
        )

    vpn.reset_cache()
    monkeypatch.setattr(vpn, "_measure_now", fausse_mesure)
    yield
    vpn.reset_cache()


def exiger(prefs: Preferences) -> None:
    """« Exiger », la maison pour reference : la mesure conclut a une fuite."""
    prefs.vpn.policy = "require"
    prefs.vpn.reference_ip = IP_MAISON


class FauxPipeline:
    """Un plan sur par fichier. Le pipeline lui-meme est teste ailleurs."""

    def __init__(self, **kwargs) -> None:
        pass

    async def plan_all(self, files, on_progress=None):
        return [
            Plan(
                id=f"p{i}",
                source=f.path,
                destination=f.path.parent.parent / "media" / f.path.name,
                kind="movie",
                title="Dune",
                year=2024,
                score=0.99,
                decision=Decision.AUTO,
            )
            for i, f in enumerate(files)
        ]

    async def aclose(self) -> None:
        pass


@pytest.fixture
def cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Une source avec un film, une bibliotheque, un canal Discord configure."""
    source = tmp_path / "dl"
    library = tmp_path / "media"
    source.mkdir()
    library.mkdir()

    conf = automation.get_settings()
    monkeypatch.setattr(conf, "library_root", library)
    monkeypatch.setattr(conf, "tmdb_api_key", "cle-de-test")

    prefs = Preferences()
    prefs.notifications.enabled = True
    prefs.notifications.webhook_url = WEBHOOK

    class Magasin:
        def load(self):
            return prefs

        def resolved_sources(self):
            return [source]

        def destination_root(self, kind, size=None):
            return library

    magasin = Magasin()
    monkeypatch.setattr(automation, "get_store", lambda: magasin)
    monkeypatch.setattr(automation, "Pipeline", FauxPipeline)
    monkeypatch.setattr(automation, "TMDBProvider", lambda *a, **k: None)
    monkeypatch.setattr(automation, "AniListProvider", lambda *a, **k: None)
    monkeypatch.setattr(automation, "_state", automation.AutomationState())
    automation._watcher.__init__()
    review._plans.clear()

    film = source / "Dune.2024.mkv"
    with film.open("wb") as handle:
        handle.seek(2 * GB)
        handle.write(b"\0")
    return library, prefs


@pytest.fixture
def envois(monkeypatch: pytest.MonkeyPatch) -> list[Notification]:
    """Ce qui aurait ete envoye a Discord."""
    vus: list[Notification] = []

    async def faux_send(url, notification, *, client=None) -> bool:
        vus.append(notification)
        return True

    monkeypatch.setattr(automation, "send", faux_send)
    return vus


@pytest.fixture
def etapes(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Espionne les deux etapes qui suivent un rangement."""
    vues: dict[str, list] = {"sous_titres": [], "serveur": []}

    async def faux_sous_titres(demandes):
        vues["sous_titres"].append(demandes)
        return {"examined": len(demandes), "written": 2, "reason": ""}

    async def faux_serveur() -> None:
        vues["serveur"].append(True)

    monkeypatch.setattr(review, "_recuperer_sous_titres", faux_sous_titres)
    monkeypatch.setattr(review, "_tell_media_server", faux_serveur)
    return vues


# --- F0 : un refus ne publie rien, et ne cite pas la maison ------------------


async def test_la_raison_du_refus_cite_bien_l_adresse() -> None:
    """Temoin : sans lui, les tests suivants pourraient passer simplement parce
    que la mesure fabriquee ne produit aucune adresse."""
    sortie = await vpn.egress_allowed("require", reference_ip=IP_MAISON)

    assert not sortie.allowed
    assert IP_MAISON in sortie.reason


async def test_un_cycle_refuse_ne_notifie_rien_et_ne_cite_pas_l_adresse(
    cycle, envois, monkeypatch
) -> None:
    _, prefs = cycle
    exiger(prefs)
    prefs.notifications.on_failure = True
    # Le tour programme attend qu'un fichier soit stable ; ici on veut qu'il
    # aille jusqu'a la garde de sortie.
    vrai_cycle = automation.run_cycle

    async def cycle_force() -> automation.CycleReport:
        return await vrai_cycle(forced=True)

    monkeypatch.setattr(automation, "run_cycle", cycle_force)

    report = await automation._tour()

    assert report is not None
    assert report.failed and report.egress_refused
    assert envois == [], "un refus de sortie a produit une notification"
    assert report.message == automation.MESSAGE_SORTIE_REFUSEE
    assert IP_MAISON not in json.dumps(automation.status())


async def test_notify_sous_refus_n_envoie_rien(cycle, envois) -> None:
    _, prefs = cycle
    exiger(prefs)

    await automation._notify(Notification(title="1 fichier(s) range(s)", body="Dune"))

    assert envois == []


async def test_notify_sans_exigence_envoie_toujours(cycle, envois) -> None:
    """Temoin : la garde ne doit pas couper le canal quand rien n'est exige."""
    await automation._notify(Notification(title="1 fichier(s) range(s)", body="Dune"))

    assert len(envois) == 1


async def test_une_adresse_dans_une_notification_part_masquee() -> None:
    corps: list[dict] = []

    async def discord(request: httpx.Request) -> httpx.Response:
        corps.append(json.loads(request.content))
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(discord))
    try:
        ok = await send(
            WEBHOOK,
            Notification(
                title="Échec",
                body=f"Émission refusée : adresse publique {IP_MAISON} (2001:db8::7).",
            ),
            client=client,
        )
    finally:
        await client.aclose()

    assert ok
    envoye = json.dumps(corps, ensure_ascii=False)
    assert IP_MAISON not in envoye
    assert "2001:db8::7" not in envoye
    assert ADRESSE_MASQUEE in corps[0]["embeds"][0]["description"]


# --- F2 : « exiger » arrete la collection, et l'application tardive ----------


@pytest.fixture
def client():
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


@pytest.fixture
def magasin():
    """Rend ses preferences au magasin partage apres le test."""
    m = get_store()
    avant = copy.deepcopy(m.load())
    yield m
    m.save(avant)


def test_construire_la_collection_sous_refus_repond_409(client, magasin, monkeypatch) -> None:
    def interdit(*args, **kwargs):
        raise AssertionError("TheMovieDB instancie malgre le refus de sortie")

    monkeypatch.setattr(collection, "TMDBProvider", interdit)
    prefs = magasin.load()
    prefs.metadata.tmdb_api_key = "cle-de-test"
    exiger(prefs)
    magasin.save(prefs)

    reponse = client.post("/api/collection/build")

    assert reponse.status_code == 409
    assert "tunnel" in reponse.json()["detail"]


async def test_un_refus_survenu_pendant_le_cycle_range_sans_affiches_ni_sous_titres(
    cycle, etapes, monkeypatch
) -> None:
    """La planification a pu depasser la minute de cache : la decision du debut
    ne vaut plus au moment de deposer des affiches."""
    _, prefs = cycle
    prefs.automation = AutomationSettings(enabled=True, apply_auto=True)
    prefs.local_metadata.artwork = True

    decisions = [
        vpn.Decision(allowed=True, warn=False, reason="Sortie confirmée."),
        vpn.Decision(allowed=False, warn=True, reason=f"Émission refusée ({IP_MAISON})."),
    ]

    async def sortie(policy, **kwargs) -> vpn.Decision:
        return decisions.pop(0)

    fiches = []
    vrai_apply = automation.apply_plan

    def espion_apply(plan, journal, **kwargs):
        fiches.append(kwargs["local_metadata"])
        return vrai_apply(plan, journal, **kwargs)

    monkeypatch.setattr(automation, "egress_allowed", sortie)
    monkeypatch.setattr(automation, "apply_plan", espion_apply)

    report = await automation.run_cycle(forced=True)

    assert report.applied == 1
    assert decisions == [], "la sortie n'a pas ete reverifiee avant l'application"
    assert [f.artwork for f in fiches] == [False]
    assert prefs.local_metadata.artwork is True, "le reglage lui-meme a ete modifie"
    assert etapes["sous_titres"] == []
    assert etapes["serveur"] == [True], "le serveur multimedia est local : il est prevenu"
    assert "affiches et sous-titres non récupérés" in report.message
    assert IP_MAISON not in report.message


# --- F6 et F28 : ce que le cycle automatique oubliait -------------------------


async def test_un_cycle_qui_range_cherche_les_sous_titres_et_previent_le_serveur(
    cycle, etapes
) -> None:
    library, prefs = cycle
    prefs.automation = AutomationSettings(enabled=True, apply_auto=True)

    report = await automation.run_cycle(forced=True)

    assert report.applied == 1
    assert etapes["sous_titres"] == [[(library / "Dune.2024.mkv", "Dune", 2024, None, None)]]
    assert etapes["serveur"] == [True]
    assert "2 sous-titre(s) déposé(s)" in report.message


async def test_un_cycle_qui_ne_range_rien_ne_previent_personne(cycle, etapes) -> None:
    _, prefs = cycle
    prefs.automation = AutomationSettings(enabled=True, apply_auto=False)

    report = await automation.run_cycle(forced=True)

    assert report.applied == 0
    assert etapes == {"sous_titres": [], "serveur": []}
