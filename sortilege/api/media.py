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

import hashlib
import logging
import mimetypes
import re
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from . import review
from .deps import DATA_DIR

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


# --- Vignettes --------------------------------------------------------------
#
# Le lecteur ne suffit pas, et pas par manque de soin : aucun navigateur
# courant ne lit le MKV, qui est le format majoritaire des bibliotheques
# constituees. Un lecteur noir sur neuf fichiers sur dix ne repond pas a la
# question posee — « est-ce le bon fichier ? ».
#
# Quelques images extraites y repondent mieux, et pour bien moins cher qu'un
# transcodage : elles marchent sur tout ce que ffmpeg sait ouvrir, elles
# montrent l'oeuvre d'un coup d'oeil, et une image noire ou figee en fin de
# fichier revele un telechargement incomplet — ce qu'une lecture du debut ne
# montrerait jamais.

THUMB_TIMEOUT = 20.0
"""Un fichier pathologique ne doit pas retenir un worker indefiniment."""

THUMB_WIDTH = 480
DEFAULT_POSITIONS = (0.10, 0.30, 0.50, 0.70, 0.90)
"""Reparties sur toute la duree, la fin comprise : c'est la fin qui trahit un
telechargement interrompu."""


CACHE_DIR = DATA_DIR / "vignettes"
CACHE_MAX = 2000
"""Environ trente megaoctets a quarante image. Au-dela, on evince les plus
anciennes : une bibliotheque parcourue longuement remplirait sinon le volume de
donnees, qui contient aussi le journal d'annulation — la seule chose vraiment
precieuse ici."""


def _cache_key(path: Path, at: float) -> str:
    """Identifie une image de facon a ce qu'un fichier MODIFIE n'en herite pas.

    La taille et la date de modification entrent dans la cle : un fichier
    remplace par un autre encodage sous le meme nom produit une cle differente,
    et l'ancienne vignette n'est jamais servie a sa place.
    """
    try:
        info = path.stat()
        empreinte = f"{path}|{info.st_size}|{info.st_mtime_ns}|{at:.3f}"
    except OSError:
        empreinte = f"{path}|{at:.3f}"
    return hashlib.blake2s(empreinte.encode("utf-8"), digest_size=16).hexdigest()


def _cache_read(cle: str) -> bytes | None:
    fichier = CACHE_DIR / f"{cle}.jpg"
    try:
        return fichier.read_bytes()
    except OSError:
        return None


def _cache_write(cle: str, image: bytes) -> None:
    """Enregistre une vignette. Un echec est sans consequence : on recalculera.

    L'ecriture passe par un fichier temporaire puis un renommage : une lecture
    concurrente ne doit jamais tomber sur une image a moitie ecrite.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        provisoire = CACHE_DIR / f".{cle}.part"
        provisoire.write_bytes(image)
        provisoire.replace(CACHE_DIR / f"{cle}.jpg")
        _cache_evict()
    except OSError as exc:
        logger.debug("vignette non mise en cache : %s", exc)


def _cache_evict() -> None:
    """Retire les plus anciennes au-dela du plafond."""
    try:
        fichiers = sorted(CACHE_DIR.glob("*.jpg"), key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    for vieux in fichiers[: max(0, len(fichiers) - CACHE_MAX)]:
        try:
            vieux.unlink()
        except OSError:
            continue


def purge_thumbnails() -> int:
    """Vide le cache des vignettes. Appele a chaque scan.

    Les cles incluent taille et date, donc une vignette perimee n'est jamais
    SERVIE — mais elle n'est jamais liberee non plus. Un scan est le moment ou
    l'on sait que la bibliotheque a bouge : c'est la que le menage a le plus de
    sens, et le cout est nul puisque les images se reconstruisent a la demande.
    """
    supprimees = 0
    try:
        for image in CACHE_DIR.glob("*.jpg"):
            try:
                image.unlink()
                supprimees += 1
            except OSError:
                continue
    except OSError:
        return supprimees
    if supprimees:
        logger.info("cache de vignettes vide : %s image(s)", supprimees)
    return supprimees


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _duration_seconds(path: Path) -> float | None:
    # Chemin absolu resolu ici, comme dans core/probe.py : lancer « ffprobe »
    # tel quel dependrait du PATH du processus, qui n'est pas le notre.
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=THUMB_TIMEOUT,
            check=False,
        )
        return float(out.stdout.strip())
    except (ValueError, OSError, subprocess.SubprocessError):
        return None


def _grab_frame(path: Path, at_seconds: float) -> bytes | None:
    """Extrait une image unique. Renvoie None plutot que de lever.

    ``-ss`` AVANT ``-i`` : le positionnement se fait alors sur les images cles
    sans decoder tout ce qui precede. Sur un fichier de trois gigaoctets, la
    difference est d'un ordre de grandeur.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-ss",
                f"{max(0.0, at_seconds):.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                f"scale={THUMB_WIDTH}:-2",
                "-f",
                "image2",
                "-c:v",
                "mjpeg",
                "pipe:1",
            ],
            capture_output=True,
            timeout=THUMB_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("extraction d'image impossible pour %s : %s", path.name, exc)
        return None
    return out.stdout or None


@router.get("/plan/{plan_id}/thumb")
def thumbnail(plan_id: str, at: float = 0.5) -> Response:
    """Une image du fichier, prise a ``at`` (fraction de la duree).

    Une fraction et non des secondes : l'appelant n'a pas a connaitre la duree,
    et « au milieu » veut dire la meme chose pour un episode de vingt minutes
    et pour un film de trois heures.
    """
    plan = next((p for p in review.current_plans() if p.id == plan_id), None)
    if plan is None or not plan.source.is_file():
        raise HTTPException(status_code=404, detail="Fichier introuvable.")
    if not _ffmpeg_available():
        raise HTTPException(status_code=503, detail="ffmpeg absent de l'image.")

    duration = _duration_seconds(plan.source)
    if not duration or duration <= 0:
        raise HTTPException(status_code=422, detail="Duree illisible : fichier corrompu ?")

    cle = _cache_key(plan.source, at)
    if (frame := _cache_read(cle)) is None:
        frame = _grab_frame(plan.source, duration * min(max(at, 0.0), 0.99))
        if frame is None:
            raise HTTPException(status_code=422, detail="Aucune image lisible a cet endroit.")
        _cache_write(cle, frame)

    return Response(
        content=frame,
        media_type="image/jpeg",
        # Pas de cache NAVIGATEUR : l'URL ne change pas quand le fichier
        # change, il servirait donc une image perimee sans moyen de le savoir.
        # Le cache disque, lui, est indexe sur la taille et la date du fichier
        # — c'est lui qui evite de relancer ffmpeg, et il suffit.
        headers={"Cache-Control": "no-store"},
    )


@router.post("/thumbs/purge")
def purge_cache() -> dict[str, object]:
    """Vide le cache des vignettes a la demande.

    Il se vide deja a chaque scan ; ce bouton sert quand on veut recuperer la
    place sans relancer un scan complet, ou quand on soupconne une image de ne
    pas correspondre.
    """
    return {"removed": purge_thumbnails()}


@router.get("/plan/{plan_id}/positions")
def thumb_positions(plan_id: str) -> dict[str, object]:
    """Ou placer les vignettes, et si l'on peut en produire.

    Repondre AVANT d'extraire evite a l'interface d'afficher cinq images
    cassees quand ffmpeg manque ou que le fichier est illisible.
    """
    plan = next((p for p in review.current_plans() if p.id == plan_id), None)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan inconnu.")

    available = _ffmpeg_available() and plan.source.is_file()
    duration = _duration_seconds(plan.source) if available else None
    return {
        "available": bool(available and duration),
        "duration": duration,
        "positions": list(DEFAULT_POSITIONS) if available and duration else [],
        "playable_in_browser": plan.source.suffix.lower() in BROWSER_FRIENDLY,
    }
