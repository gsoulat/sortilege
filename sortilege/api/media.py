"""Lecture d'un fichier depuis l'interface, pour verifier son contenu.

Un titre et une affiche ne disent pas si le fichier est le bon : une release
peut etre mal nommee, tronquee, ou contenir tout autre chose. Quelques secondes
de video tranchent la ou aucun score ne le peut.

**Aucun chemin ne vient du navigateur.** Le client designe un PLAN par son
identifiant, et le serveur sait seul quel fichier cela designe. Un endpoint qui
servirait un chemin fourni par le client serait une traversee de repertoire en
puissance — et ici elle donnerait la lecture de n'importe quel fichier monte
dans le conteneur.

**Les requetes par plage sont indispensables**, pas un raffinement : sans
elles, deplacer le curseur d'une video de trois gigaoctets impose de tout
telecharger depuis le debut. Le navigateur ne propose meme pas la barre de
progression tant que le serveur n'annonce pas les accepter.
"""

from __future__ import annotations

import logging
import mimetypes
import re
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import review

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/media", tags=["lecture"])

CHUNK = 1024 * 512
"""512 Ko par morceau. Assez gros pour ne pas multiplier les allers-retours,
assez petit pour qu'un arret de lecture ne laisse pas un megaoctet en vol."""

_RANGE = re.compile(r"bytes=(?P<start>\d*)-(?P<end>\d*)")

# Conteneurs que les navigateurs savent lire nativement. Le MKV n'en fait pas
# partie — Chrome le refuse, Firefox l'accepte parfois selon les codecs. On le
# sert quand meme : c'est au navigateur de dire qu'il ne sait pas faire, et
# l'interface previent plutot que d'interdire.
BROWSER_FRIENDLY = {".mp4", ".m4v", ".webm", ".mov"}


def _iter_range(path: Path, start: int, end: int) -> Iterator[bytes]:
    remaining = end - start + 1
    with path.open("rb") as handle:
        handle.seek(start)
        while remaining > 0:
            chunk = handle.read(min(CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@router.get("/plan/{plan_id}")
def stream_plan(plan_id: str, request: Request) -> StreamingResponse:
    """Sert la video d'un plan, en acceptant les requetes par plage.

    Le plan est la seule entree : le chemin ne transite jamais par le client,
    et un identifiant inconnu ne revele rien de plus qu'un 404.
    """
    plan = next((p for p in review.current_plans() if p.id == plan_id), None)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan inconnu.")

    source = plan.source
    if not source.is_file():
        raise HTTPException(status_code=404, detail="Fichier introuvable.")

    size = source.stat().st_size
    start, end = 0, size - 1
    status = 200

    if raw_range := request.headers.get("range"):
        if match := _RANGE.match(raw_range):
            first, last = match.group("start"), match.group("end")
            start = int(first) if first else 0
            end = int(last) if last else size - 1
            # Une plage hors bornes se corrige plutot que d'echouer : certains
            # lecteurs demandent au-dela de la fin en fin de lecture.
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            status = 206

    media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
        # Le fichier peut etre deplace juste apres : ne rien mettre en cache
        # evite de servir plus tard un contenu qui n'est plus a cette place.
        "Cache-Control": "no-store",
    }
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"

    return StreamingResponse(
        _iter_range(source, start, end),
        status_code=status,
        media_type=media_type,
        headers=headers,
    )
