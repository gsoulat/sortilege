"""Sauvegarde et restauration de l'etat.

L'etat precieux de Sortilege tient dans un volume monte, et rien ne permettait
d'en sortir une copie : un conteneur recree sans son volume repart de zero,
gabarits et arbitrages compris. Ces routes rendent l'archive telechargeable
depuis l'interface, comme Radarr le fait — pour que la sauvegarde ne suppose ni
d'acceder au NAS en SSH, ni de connaitre le chemin du volume.

La regle qui structure tout le reste vit dans ``core/backup.py`` : **les
secrets ne partent pas dans l'archive**, et la restauration ne les ecrase pas.
Elle est repetee ici parce que ces routes sont ce qu'un integrateur lit en
premier, et elle est ecrite dans l'archive elle-meme pour celui qui l'ouvrira
sans avoir jamais vu ce fichier.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response

from .. import __version__
from ..core import backup
from . import deps, library, review

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backup", tags=["sauvegarde"])


def _data_dir() -> Path:
    """Lu a l'appel et non a l'import : les tests detournent ``deps.DATA_DIR``,
    et un import par valeur capturerait le chemin reel de l'utilisateur."""
    return deps.DATA_DIR


@router.get("")
def describe() -> dict[str, object]:
    """Ce qu'une archive contiendrait aujourd'hui, et ce qu'elle laisserait.

    Repondre avant de telecharger evite le seul mauvais scenario possible ici :
    croire tenir une sauvegarde complete, decouvrir a la restauration qu'elle
    ne portait pas la cle qu'on croyait dedans.
    """
    racine = _data_dir()
    pieces = []
    for piece in backup.PIECES:
        chemin = racine / piece.name
        present = chemin.is_file()
        pieces.append(
            {
                "name": piece.name,
                "label": piece.label,
                "present": present,
                "bytes": chemin.stat().st_size if present else 0,
            }
        )

    return {
        "format": backup.FORMAT,
        "version": backup.VERSION,
        "app_version": __version__,
        "filename": backup.filename(__version__),
        "pieces": pieces,
        "secrets_included": False,
        "secrets_excluded": backup.configured_secrets(racine),
        "secrets_policy": (
            "Les clés et jetons ne partent jamais dans l'archive : elle se télécharge, se "
            "copie et se transmet. À la restauration, ces champs ne sont pas écrasés — une "
            "instance qui a déjà ses clés les garde, une instance neuve les laisse vides."
        ),
    }


@router.get("/archive")
def download() -> Response:
    """Produit l'archive et la renvoie en telechargement."""
    try:
        contenu = backup.build(_data_dir(), app_version=__version__)
    except backup.BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    nom = backup.filename(__version__)
    return Response(
        content=contenu,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{nom}"',
            # Dit sur la reponse elle-meme ce que l'archive ne contient pas.
            # Un client qui automatise la sauvegarde ne lira jamais le
            # LISEZMOI range a l'interieur du zip.
            "X-Sortilege-Secrets": "exclus",
        },
    )


async def _corps(request: Request) -> bytes:
    """Le zip arrive dans le corps brut, sans formulaire multipart.

    Un `curl --data-binary @archive.zip` suffit alors, et le navigateur peut
    envoyer l'objet File tel quel. Le multipart n'apporterait qu'un champ de
    plus a encoder des deux cotes.
    """
    blob = await request.body()
    if not blob:
        raise HTTPException(
            status_code=400,
            detail=(
                "Aucune archive reçue. Envoie le fichier .zip dans le corps de la requête "
                "(curl --data-binary @sortilege.zip)."
            ),
        )
    return blob


@router.post("/inspect")
async def inspect(request: Request) -> dict[str, object]:
    """Dit ce que contient une archive sans rien ecraser.

    C'est ce qui donne une matiere a la confirmation : on ne confirme pas
    « restaurer », on confirme « remplacer mes réglages et mon journal par ceux
    d'une archive du 3 août ».
    """
    try:
        return backup.inspect(await _corps(request))
    except backup.BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/restore")
async def restore(request: Request, confirm: bool = False) -> dict[str, object]:
    """Remplace l'etat en place par celui de l'archive. Irreversible sans filet.

    La confirmation est explicite et cote serveur : cette route est aussi
    appelable depuis un script, ou un `curl` de trop dans un historique de
    shell effacerait des centaines d'arbitrages sans qu'aucune boite de
    dialogue n'ait eu l'occasion de s'ouvrir.
    """
    blob = await _corps(request)
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "La restauration écrase les réglages, les décisions mémorisées et le journal "
                "en place. Regarde d'abord ce que contient l'archive avec POST "
                "/api/backup/inspect, puis rappelle cette requête avec « ?confirm=true »."
            ),
        )

    try:
        rapport = backup.restore(blob, _data_dir(), app_version=__version__)
    except backup.BackupError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=(
                f"Écriture impossible dans le volume de données : {exc}. Vérifie que le "
                "volume est monté en écriture et que PUID/PGID correspondent à son "
                "propriétaire."
            ),
        ) from exc

    _recharge()
    return rapport


def _recharge() -> None:
    """Remet le processus en accord avec le disque qu'on vient de remplacer.

    Sans cela, l'application continuerait a servir les preferences et les plans
    charges avant la restauration : l'utilisateur verrait une restauration
    « réussie » sans aucun effet visible, jusqu'au prochain redemarrage du
    conteneur.
    """
    deps.get_store.cache_clear()
    deps.get_memory.cache_clear()
    deps.get_journal.cache_clear()
    try:
        if library.restore_scan():
            review.restore_plans()
    except Exception:
        # Le scan et les plans sont reconstructibles : echouer a les reprendre
        # ne doit pas faire passer pour ratee une restauration qui a bien
        # remplace les fichiers.
        logger.exception("reprise de l'etat de travail impossible apres restauration")
