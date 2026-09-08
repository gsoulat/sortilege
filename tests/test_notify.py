"""Notifications Discord.

Deux choses comptent ici, et aucune n'est le formatage du message.

**Ce qu'on n'envoie pas.** Un canal qui recoit « 0 fichier range » toutes les
quinze minutes est un canal qu'on coupe, et le jour ou un echec arrive plus
personne ne regarde. Le silence est donc la valeur par defaut.

**Ou l'on accepte d'emettre.** L'URL est saisie depuis le navigateur et c'est
le SERVEUR qui va la chercher. Sans restriction d'hote, ce champ devient un
moyen de faire emettre au conteneur des requetes vers n'importe quelle adresse
du reseau domestique — un NAS, un routeur, une interface d'administration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import pytest

from sortilege.core.notify import (
    Notification,
    WebhookError,
    cycle_notification,
    failure_notification,
    send,
    validate_webhook,
)

VALID = "https://discord.com/api/webhooks/123456/abcdef"


# --- Ce qu'on accepte d'appeler --------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        VALID,
        "https://discordapp.com/api/webhooks/1/x",
        "https://canary.discord.com/api/webhooks/1/x",
    ],
)
def test_un_webhook_discord_est_accepte(url) -> None:
    assert validate_webhook(url) == url


@pytest.mark.parametrize(
    ("url", "raison"),
    [
        ("http://discord.com/api/webhooks/1/x", "https impose"),
        ("https://192.168.10.10/api/webhooks/1/x", "adresse du reseau local"),
        ("https://localhost:8117/api/webhooks/1/x", "l'application elle-meme"),
        ("https://169.254.169.254/api/webhooks/1/x", "metadonnees d'instance"),
        ("https://discord.com.evil.test/api/webhooks/1/x", "hote qui imite discord.com"),
        ("https://example.com/api/webhooks/1/x", "hote quelconque"),
    ],
)
def test_une_url_hors_discord_est_refusee(url, raison) -> None:
    """Chacune de ces adresses ferait du serveur un relais de requetes."""
    with pytest.raises(WebhookError):
        validate_webhook(url)


def test_une_url_discord_qui_n_est_pas_un_webhook_est_refusee() -> None:
    with pytest.raises(WebhookError):
        validate_webhook("https://discord.com/channels/1/2")


def test_une_url_vide_est_acceptee_comme_absence() -> None:
    """Champ vide = pas de notification, pas une erreur de saisie."""
    assert validate_webhook("  ") == ""


def test_le_refus_nomme_l_hote_recu() -> None:
    """Un message qui dit seulement « URL invalide » ne se corrige pas."""
    with pytest.raises(WebhookError, match=re.escape("example.com")):
        validate_webhook("https://example.com/api/webhooks/1/x")


# --- Ce qu'on dit, et quand on se tait --------------------------------------


@dataclass
class Report:
    detected: int = 0
    applied: int = 0
    queued: int = 0
    message: str = ""


def test_un_cycle_sans_rien_ne_dit_rien() -> None:
    """Le point central : sans ce silence, le canal devient du bruit."""
    assert cycle_notification(Report()) is None


def test_un_rangement_est_annonce() -> None:
    notif = cycle_notification(Report(detected=4, applied=4, message="4 range(s)"))
    assert notif is not None
    assert notif.level == "ok"
    assert ("Ranges", "4") in notif.fields


def test_une_file_en_attente_attire_l_oeil() -> None:
    """Des fichiers a arbitrer sont une demande d'action, pas un echec."""
    notif = cycle_notification(Report(detected=9, applied=2, queued=7))
    assert notif.level == "warn"
    assert ("A arbitrer", "7") in notif.fields


def test_un_echec_est_de_niveau_erreur() -> None:
    assert failure_notification("RuntimeError: disque plein").level == "error"


def test_un_echec_verbeux_est_tronque() -> None:
    """Discord refuse un embed trop long : une trace entiere perdrait le
    message au lieu de le transmettre."""
    notif = failure_notification("x" * 9000)
    assert len(notif.body) < 2000


def test_le_message_discord_a_la_forme_attendue() -> None:
    payload = Notification(title="T", body="B", fields=(("a", "1"),)).payload()
    embed = payload["embeds"][0]
    assert embed["title"] == "T"
    assert embed["fields"] == [{"name": "a", "value": "1", "inline": True}]


# --- Envoi ------------------------------------------------------------------


async def test_un_envoi_accepte_renvoie_vrai() -> None:
    seen: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await send(VALID, Notification("T", "B"), client=client) is True
    finally:
        await client.aclose()
    assert len(seen) == 1


async def test_un_refus_de_discord_ne_leve_pas() -> None:
    """Un webhook revoque ne doit pas interrompre un cycle de rangement."""

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Unknown Webhook"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await send(VALID, Notification("T", "B"), client=client) is False
    finally:
        await client.aclose()


async def test_un_reseau_coupe_ne_leve_pas() -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("injoignable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await send(VALID, Notification("T", "B"), client=client) is False
    finally:
        await client.aclose()


async def test_une_url_interdite_n_est_jamais_appelee() -> None:
    """La verification a lieu AVANT la requete : sinon le refus arriverait
    apres que le serveur a deja emis vers l'adresse visee."""
    appels = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal appels
        appels += 1
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert (
            await send("https://192.168.10.1/admin", Notification("T", "B"), client=client) is False
        )
    finally:
        await client.aclose()
    assert appels == 0


# --- Reglage ----------------------------------------------------------------


def make_store(tmp_path):
    from sortilege.core.preferences import PreferenceStore

    library = tmp_path / "media"
    library.mkdir()
    source = tmp_path / "dl"
    source.mkdir()
    return PreferenceStore(
        path=tmp_path / "data" / "preferences.json",
        library_root=library,
        source_roots=[source],
    )


def test_activer_sans_url_est_refuse(tmp_path) -> None:
    """Une notification activee sans canal est un reglage qui ne fera rien.

    Le dire a l'enregistrement vaut mieux que de le laisser decouvrir au
    moment de l'echec qu'on esperait justement etre notifie."""
    from sortilege.core.preferences import NotificationSettings, PreferenceError, Preferences

    store = make_store(tmp_path)
    prefs = Preferences(notifications=NotificationSettings(enabled=True, webhook_url=""))
    with pytest.raises(PreferenceError):
        store.save(prefs)


def test_activer_avec_une_url_hors_discord_est_refuse(tmp_path) -> None:
    """Le refus a lieu a l'enregistrement, pas seulement a l'envoi : une
    adresse de reseau local ne doit pas pouvoir dormir dans les preferences."""
    from sortilege.core.preferences import NotificationSettings, PreferenceError, Preferences

    store = make_store(tmp_path)
    prefs = Preferences(
        notifications=NotificationSettings(
            enabled=True, webhook_url="https://192.168.10.10/api/webhooks/1/x"
        )
    )
    with pytest.raises(PreferenceError):
        store.save(prefs)


def test_une_url_gardee_desactivee_n_est_pas_validee(tmp_path) -> None:
    """Couper les notifications ne doit pas exiger d'effacer l'URL d'abord."""
    from sortilege.core.preferences import NotificationSettings, Preferences

    store = make_store(tmp_path)
    prefs = Preferences(
        notifications=NotificationSettings(enabled=False, webhook_url="brouillon incomplet")
    )
    store.save(prefs)
    assert store.load().notifications.enabled is False
