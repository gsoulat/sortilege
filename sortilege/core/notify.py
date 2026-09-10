"""Notifications Discord.

Un rangement automatique se fait par definition sans personne devant l'ecran.
Le seul moment ou l'on regarde l'interface, c'est quand on soupconne un
probleme — donc trop tard. Une notification inverse ce rapport : l'outil dit ce
qu'il a fait, on ne va le voir que si quelque chose cloche.

Trois principes, tous appris a l'usage des outils du meme genre :

1. **Le silence est la valeur par defaut.** Un message toutes les quinze
   minutes disant « 0 fichier range » apprend a ignorer le canal, et le jour ou
   un vrai echec arrive il passe inapercu. On ne notifie que ce qui s'est
   passe.
2. **Une notification ne casse jamais un cycle.** Discord peut etre injoignable
   ou l'URL peut avoir ete revoquee ; c'est une consequence sans importance a
   cote d'un rangement interrompu.
3. **L'URL est un secret et une cible.** Elle est saisie depuis le navigateur
   et c'est le SERVEUR qui va la chercher : sans restriction d'hote, ce champ
   devient un moyen de faire emettre au conteneur des requetes vers n'importe
   quelle adresse de ton reseau. Elle est donc contrainte aux domaines Discord,
   et elle n'est jamais renvoyee au navigateur.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Un webhook Discord vit sur l'un de ces hotes, et sur aucun autre.
ALLOWED_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "ptb.discord.com",
        "canary.discord.com",
    }
)

TIMEOUT = 10.0
"""Court : un canal de discussion injoignable ne doit pas retenir un cycle."""

# Couleurs de la barre laterale de l'embed, pour trier d'un coup d'oeil.
COLOR_OK = 0x3BA55D
COLOR_WARN = 0xE3A008
COLOR_ERROR = 0xED4245


class WebhookError(ValueError):
    """URL de webhook refusee — message destine a l'utilisateur."""


def validate_webhook(url: str) -> str:
    """Verifie qu'une URL est bien un webhook Discord.

    Refuse plutot que neutralise : contrairement a une metadonnee venue d'une
    API, cette valeur est saisie par un humain qui attend un retour. La
    corriger en silence lui ferait croire que sa saisie a ete acceptee.
    """
    url = url.strip()
    if not url:
        return ""

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise WebhookError("L'URL du webhook doit commencer par https://")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise WebhookError(
            "Seuls les webhooks Discord sont acceptes "
            f"({', '.join(sorted(ALLOWED_HOSTS))}). Hote recu : {parsed.hostname or '?'}"
        )
    if "/api/webhooks/" not in parsed.path:
        raise WebhookError("Cette URL n'est pas un webhook : elle doit contenir /api/webhooks/")
    return url


@dataclass(frozen=True, slots=True)
class Notification:
    """Ce qu'on a a dire. Le rendu Discord est un detail de transport."""

    title: str
    body: str
    level: str = "ok"
    """ok, warn ou error."""

    fields: tuple[tuple[str, str], ...] = ()

    def color(self) -> int:
        return {"warn": COLOR_WARN, "error": COLOR_ERROR}.get(self.level, COLOR_OK)

    def payload(self) -> dict[str, object]:
        embed: dict[str, object] = {
            "title": self.title,
            "description": self.body,
            "color": self.color(),
        }
        if self.fields:
            embed["fields"] = [
                {"name": name, "value": value, "inline": True} for name, value in self.fields
            ]
        return {"username": "Sortilège", "embeds": [embed]}


async def send(
    url: str, notification: Notification, *, client: httpx.AsyncClient | None = None
) -> bool:
    """Envoie une notification. Ne leve jamais.

    Renvoie ``True`` si Discord a accepte — utile au bouton de test, qui doit
    dire honnetement si le canal repond.
    """
    try:
        url = validate_webhook(url)
    except WebhookError as exc:
        logger.warning("webhook refuse : %s", exc)
        return False
    if not url:
        return False

    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        response = await client.post(url, json=notification.payload())
        if response.status_code >= 400:
            logger.warning("Discord a refuse la notification (%s)", response.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning("notification Discord impossible : %s", exc)
        return False
    finally:
        if owned:
            await client.aclose()


def cycle_notification(report) -> Notification | None:
    """Traduit un rapport de cycle en notification — ou en silence.

    C'est ici que se joue l'utilite du canal, et la premiere version se
    trompait de critere : elle parlait des qu'un fichier avait ete DETECTE.

    Or « detecte » est un ETAT, pas un evenement. Les memes fichiers sont revus
    a chaque tour, et un fichier qui ne peut pas etre planifie — deja passe par
    le calcul, ou en attente d'arbitrage — reste detecte indefiniment. Toutes
    les quinze minutes, jour et nuit, le canal recevait donc « Cycle automatique
    termine — rien de nouveau a identifier ». Un canal qui repete la meme chose
    quatre fois par heure n'est plus lu, et les rares messages qui comptent
    disparaissent avec le reste.

    Le seul evenement qui merite d'interrompre quelqu'un, c'est qu'un fichier
    ait REELLEMENT ete range. Le reste se consulte dans l'application, quand on
    decide d'aller voir. Les echecs, eux, passent par un autre chemin et gardent
    leur propre reglage.
    """
    if not report.applied:
        return None

    fields: list[tuple[str, str]] = [("Ranges", str(report.applied))]
    if report.queued:
        fields.append(("A arbitrer", str(report.queued)))
    if report.remaining:
        fields.append(("Restants", str(report.remaining)))

    # Des fichiers en attente d'arbitrage sont une demande d'action : c'est le
    # seul cas ou la couleur doit attirer l'oeil sans etre une erreur.
    level = "warn" if report.queued else "ok"
    return Notification(
        title=f"{report.applied} fichier(s) range(s)",
        body=report.message or "Rangement automatique termine.",
        level=level,
        fields=tuple(fields),
    )


def failure_notification(error: str) -> Notification:
    return Notification(
        title="Le cycle automatique a echoue",
        body=f"```{error[:1500]}```",
        level="error",
    )
