"""Notifications Discord.

Un rangement automatique se fait par definition sans personne devant l'ecran.
Le seul moment ou l'on regarde l'interface, c'est quand on soupconne un
probleme — donc trop tard. Une notification inverse ce rapport : l'outil dit ce
qu'il a fait, on ne va le voir que si quelque chose cloche.

Quatre principes. Les trois premiers viennent de l'usage des outils du meme
genre ; le quatrieme, d'un audit de celui-ci :

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
4. **Rien de ce qui part ne dit ou est la maison.** Un message Discord quitte
   le reseau, parfois par la sortie meme qu'on vient de juger non protegee :
   une adresse IP qu'il contiendrait serait publiee chez un tiers. Toute
   adresse est donc masquee au moment de construire le message, quel que soit
   l'appelant — un garde-fou pose chez chaque appelant finit toujours par en
   oublier un.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

ADRESSE_MASQUEE = "[adresse masquée]"
"""Ce qui remplace une adresse IP dans un message qui quitte la maison."""

# Les motifs ne font que proposer des candidats ; c'est ``ipaddress`` qui
# tranche. IPv4 est cherche largement : quatre nombres pointes sont presque
# toujours une adresse, et en masquer une de trop ne coute rien. IPv6 exige des
# bords de mot, faute de quoi « Data::Dumper » passerait pour une adresse.
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?!\.?\d)")
_IPV6 = re.compile(r"(?<![\w:])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:])")


def masquer_adresses(texte: str) -> str:
    """Remplace chaque adresse IP d'un texte par ``ADRESSE_MASQUEE``.

    Une heure « 12:30:45 » ou une version « 1.2.3.4.5 » ressemblent a une
    adresse sans en etre une : les masquer rendrait le message illisible sans
    rien proteger.
    """

    def _si_adresse(trouve: re.Match[str]) -> str:
        candidat = trouve.group(0)
        try:
            adresse = ipaddress.ip_address(candidat)
        except ValueError:
            return candidat
        return candidat if adresse.is_unspecified else ADRESSE_MASQUEE

    return _IPV6.sub(_si_adresse, _IPV4.sub(_si_adresse, texte))


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
        """Le corps JSON envoye a Discord, adresses masquees.

        C'est l'unique endroit ou titre, texte et champs sont assembles pour
        partir : le masquage vit donc ici (principe 4), et tout appelant de
        ``send`` en beneficie — le cycle, les echecs, le bouton d'essai, et
        ceux qui viendront.
        """
        embed: dict[str, object] = {
            "title": masquer_adresses(self.title),
            "description": masquer_adresses(self.body),
            "color": self.color(),
        }
        if self.fields:
            embed["fields"] = [
                {
                    "name": masquer_adresses(name),
                    "value": masquer_adresses(value),
                    "inline": True,
                }
                for name, value in self.fields
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

    fields: list[tuple[str, str]] = [("Rangés", str(report.applied))]
    if report.queued:
        fields.append(("À arbitrer", str(report.queued)))
    if report.remaining:
        fields.append(("Restants", str(report.remaining)))

    # Des fichiers en attente d'arbitrage sont une demande d'action : c'est le
    # seul cas ou la couleur doit attirer l'oeil sans etre une erreur.
    level = "warn" if report.queued else "ok"
    return Notification(
        title=f"{report.applied} fichier(s) rangé(s)",
        body=report.message or "Rangement automatique terminé.",
        level=level,
        fields=tuple(fields),
    )


def failure_notification(error: str) -> Notification:
    # Masquer AVANT de tronquer : une coupure au milieu d'une adresse en
    # laisserait trois octets, que le masquage de l'envoi ne reconnaitrait plus.
    return Notification(
        title="Le cycle automatique a échoué",
        body=f"```{masquer_adresses(error)[:1500]}```",
        level="error",
    )
