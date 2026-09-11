"""Declenchement depuis l'exterieur : « ce dossier vient de finir, occupe-t'en ».

C'est la piece qui manquait pour que Sortilege tienne le role que Radarr tient
depuis toujours : le client telecharge, l'outil range a la fin. Sans elle, un
transfert termine a trois heures du matin attend le prochain tour de la boucle
de surveillance, ou pire, un clic.

Deux contraintes viennent du client de telechargement, et elles gouvernent tout
ce fichier.

**Il ne peut pas attendre.** qBittorrent execute son programme externe et
tue le processus au bout de quelques secondes ; SABnzbd coupe pareil. Un scan
de bibliotheque prend des minutes. La route accepte donc, programme, et rend la
main tout de suite — 202, jamais 200 : le travail n'est pas fait quand la
reponse part, et le code HTTP doit le dire.

**Il appelle plusieurs fois.** Une fois par fichier d'un torrent de quarante
episodes, une fois de plus a chaque reessai apres un delai d'attente. La route
est donc idempotente : les appels rapproches se replient sur un seul cycle. Ce
n'est pas seulement une economie — un cycle traite TOUT ce qui est present, pas
le dossier annonce, donc quarante appels et un seul appel produisent
exactement le meme resultat. Le delai de grace avant de partir sert a cela : il
laisse la rafale se terminer avant de commencer a lire le disque.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, status
from pydantic import BaseModel

from . import automation
from .deps import get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integration", tags=["integration"])

GRACE_SECONDS = 5.0
"""Delai avant de lancer le cycle. Court, mais pas nul : un client qui appelle
une fois par fichier envoie sa rafale en moins d'une seconde, et partir au
premier appel ferait scanner pendant que les suivants arrivent."""

ATTEMPTS = 3
RETRY_SECONDS = 20.0
"""Un cycle deja en cours empeche d'en lancer un second sur le meme disque. Ce
n'est pas une perte — celui qui tourne scanne les memes sources et verra ces
fichiers — mais il a pu passer sur le dossier juste avant leur arrivee. On
retente donc, borne : une attente infinie ferait vivre une tache par appel."""


class DownloadComplete(BaseModel):
    """Ce qu'un client de telechargement sait dire de ce qu'il vient de finir.

    Les deux champs sont facultatifs : un `curl` sans corps du tout doit
    marcher, parce que c'est ce qu'on ecrit en premier pour verifier que la cle
    passe.
    """

    path: str | None = None
    """Dossier ou fichier termine. Ne pilote PAS le traitement — le cycle
    parcourt les sources configurees — mais permet de dire tout de suite si le
    crochet pointe en dehors du perimetre, ce qui est l'erreur de branchement
    la plus courante et la plus silencieuse."""

    name: str | None = None
    """Libelle du transfert, repris tel quel dans l'etat. Sert a reconnaitre
    dans l'interface d'ou vient un declenchement."""


@dataclass
class TriggerState:
    """Ce qui s'est passe, pour que l'integrateur puisse le verifier."""

    pending: bool = False
    rearm: bool = False
    accepted: int = 0
    last_at: float = 0.0
    last_source: str = ""
    last_result: str = ""


_state = TriggerState()
_task: asyncio.Task | None = None


def _in_scope(brut: str) -> bool:
    """Le chemin annonce tombe-t-il sous une source scannee ?

    Un crochet branche sur un dossier que Sortilege ne parcourt pas ne produit
    rien, et ne produit rien SILENCIEUSEMENT : c'est exactement le genre de
    panne qu'on ne decouvre qu'apres des semaines.
    """
    candidat = Path(brut).resolve(strict=False)
    for racine in get_store().resolved_sources():
        resolue = racine.resolve()
        if candidat == resolue or resolue in candidat.parents:
            return True
    return False


async def _un_cycle() -> None:
    for tentative in range(1, ATTEMPTS + 1):
        rapport = await automation.run_now()
        if rapport.get("started"):
            dernier = rapport.get("last") or {}
            _state.last_result = str(dernier.get("message") or "cycle terminé")
            return
        logger.info("declenchement externe : cycle deja en cours (tentative %s)", tentative)
        await asyncio.sleep(RETRY_SECONDS)
    _state.last_result = (
        "Un cycle tournait déjà à chaque tentative. Les fichiers seront pris par celui-là."
    )


async def _worker() -> None:
    """Une seule tache pour tous les appels en attente."""
    try:
        while True:
            _state.rearm = False
            await asyncio.sleep(GRACE_SECONDS)
            await _un_cycle()
            if not _state.rearm:
                return
    except asyncio.CancelledError:
        raise
    except Exception:
        # Un crochet externe ne doit jamais pouvoir tuer le processus : le
        # client a deja recu son 202 et ne saura rien de cet echec, d'ou le
        # journal.
        logger.exception("le declenchement externe a echoue")
        _state.last_result = "Échec du cycle déclenché — voir les journaux du conteneur."
    finally:
        _state.pending = False


def stop() -> None:
    """Annule le travail en attente a l'arret du processus."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
    _state.pending = False
    _state.rearm = False


@router.post("/download-complete", status_code=status.HTTP_202_ACCEPTED)
async def download_complete(body: DownloadComplete | None = None) -> dict[str, object]:
    """Signale qu'un transfert est termine. Rend la main immediatement.

    Repond 202 et non 200 : rien n'est range quand la reponse part. Un client
    qui lit le code de retour doit pouvoir distinguer « pris en compte » de
    « fait », sans quoi il conclura de la premiere reponse que le fichier est
    deja a sa place.
    """
    global _task
    body = body or DownloadComplete()
    chemin = (body.path or "").strip()

    if chemin and not _in_scope(chemin):
        sources = ", ".join(str(p) for p in get_store().resolved_sources()) or "(aucune)"
        return {
            "accepted": True,
            "scheduled": False,
            "in_scope": False,
            "path": chemin,
            "detail": (
                f"« {chemin} » n'est sous aucune source scannée ({sources}). Ajoute ce "
                "dossier dans Réglages → Sources, ou appelle cette route sans « path » "
                "pour traiter les sources déjà configurées."
            ),
        }

    _state.accepted += 1
    _state.last_at = time.time()
    _state.last_source = body.name or chemin or "(non précisé)"

    if _state.pending:
        # Deja programme : on note qu'un appel de plus est arrive pour que le
        # cycle reparte une fois de plus s'il etait deja lance, et on repond la
        # meme chose qu'au premier appel. Un client qui reessaie ne doit jamais
        # lire un echec pour avoir insiste.
        _state.rearm = True
        return {
            "accepted": True,
            "scheduled": False,
            "in_scope": True,
            "path": chemin or None,
            "detail": "Un traitement est déjà programmé : cet appel s'y ajoute.",
        }

    _state.pending = True
    _task = asyncio.create_task(_worker(), name="sortilege-integration")
    return {
        "accepted": True,
        "scheduled": True,
        "in_scope": True,
        "path": chemin or None,
        "detail": f"Traitement programmé dans {GRACE_SECONDS:.0f} s.",
    }


@router.get("")
def status_integration() -> dict[str, object]:
    """Etat du dernier declenchement externe.

    Sans cette route, brancher un client de telechargement se verifie en
    regardant si des fichiers ont bouge — c'est-a-dire trop tard, et sans
    distinguer « le crochet n'appelle pas » de « il appelle et rien ne
    correspond ».
    """
    return {
        "pending": _state.pending,
        "accepted": _state.accepted,
        "last_at": _state.last_at or None,
        "last_source": _state.last_source or None,
        "last_result": _state.last_result or None,
        "grace_seconds": GRACE_SECONDS,
    }
