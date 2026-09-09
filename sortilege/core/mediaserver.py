"""Prevenir le serveur multimedia qu'un fichier vient d'arriver.

Sans cela, un film range n'apparait dans Jellyfin qu'au prochain scan planifie
— souvent plusieurs heures. Un appel apres rangement le rend visible dans la
minute, et c'est tout ce que l'integration a besoin de faire.

**Pourquoi un appel et non une fusion.** Forker Jellyfin pour y greffer le
rangement couterait une reecriture complete (C#/.NET contre Python), un rebase
perpetuel sur un projet de plusieurs centaines de milliers de lignes dont les
correctifs de securite comptent, et la perte de la separation qui fait qu'un
bug de rangement n'empeche pas de regarder un film. Un POST apporte l'essentiel
du benefice pour une fraction infime du cout.

Les memes precautions que pour les notifications :

- **L'URL est saisie depuis le navigateur et appelee par le SERVEUR.** Elle est
  donc restreinte a une adresse d'apparence locale — un serveur multimedia vit
  sur le reseau domestique, pas sur Internet — et l'appel ne suit aucune
  redirection, qui pourrait mener ailleurs.
- **La cle d'API ne redescend jamais au navigateur.**
- **Un echec ne casse jamais un rangement.** Le fichier est deja a sa place ;
  ne pas avoir prevenu le serveur est un desagrement, pas une perte.
"""

from __future__ import annotations

import ipaddress
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 10.0

# Noms d'hote acceptes en plus des adresses privees. Un serveur multimedia est
# joignable par son nom sur le reseau local bien plus souvent que par son IP.
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain")
LOCAL_NAMES = frozenset({"localhost"})


class MediaServerError(ValueError):
    """URL de serveur refusee — message destine a l'utilisateur."""


def _is_local(host: str) -> bool:
    """L'hote est-il sur le reseau domestique ?

    Un serveur multimedia n'est pas sur Internet. Restreindre a une adresse
    locale evite que ce champ, saisi depuis le navigateur et appele par le
    serveur, ne devienne un moyen d'emettre des requetes vers n'importe ou.
    """
    lowered = host.lower()
    if lowered in LOCAL_NAMES or lowered.endswith(LOCAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        # Un nom d'hote quelconque : on ne resout pas — la resolution pourrait
        # changer entre la verification et l'appel.
        return False
    if address.is_link_local:
        # 169.254.0.0/16 est « prive » au sens de Python, mais c'est aussi
        # l'adresse des metadonnees d'instance chez les hebergeurs — une cible
        # classique. Aucun serveur multimedia n'y vit.
        return False
    return address.is_private or address.is_loopback


def validate_server_url(url: str) -> str:
    """Verifie l'URL d'un serveur multimedia.

    Refuse plutot que neutralise : la valeur est saisie par un humain qui
    attend un retour, et la corriger en silence lui ferait croire que sa saisie
    a ete acceptee.
    """
    url = url.strip().rstrip("/")
    if not url:
        return ""

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise MediaServerError("L'URL doit commencer par http:// ou https://")
    if not parsed.hostname:
        raise MediaServerError("L'URL ne contient aucun hote")
    if not _is_local(parsed.hostname):
        raise MediaServerError(
            f"« {parsed.hostname} » n'est pas une adresse du reseau local. "
            "Un serveur multimedia se joint par son IP privee (192.168.x.x, "
            "10.x.x.x), par « localhost », ou par un nom en .local"
        )
    return url


async def refresh_library(
    base_url: str, api_key: str, *, client: httpx.AsyncClient | None = None
) -> bool:
    """Demande au serveur de relire sa bibliotheque. Ne leve jamais.

    Le rafraichissement porte sur toute la bibliotheque et non sur un dossier :
    l'API de Jellyfin ne permet pas de cibler un chemin, et un scan incremental
    sur une bibliotheque deja indexee coute peu.
    """
    try:
        base_url = validate_server_url(base_url)
    except MediaServerError as exc:
        logger.warning("serveur multimedia refuse : %s", exc)
        return False
    if not base_url or not api_key:
        return False

    owned = client is None
    # Les redirections ne sont pas suivies : une redirection pourrait mener
    # hors du reseau local, ce que la verification d'hote vient d'ecarter.
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
    try:
        response = await client.post(
            f"{base_url}/Library/Refresh",
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
        )
        if response.status_code >= 400:
            logger.warning("le serveur multimedia a refuse (%s)", response.status_code)
            return False
        return True
    except httpx.HTTPError as exc:
        logger.warning("serveur multimedia injoignable : %s", exc)
        return False
    finally:
        if owned:
            await client.aclose()
