"""File de reencodage : mise en attente, travail de nuit, verification au matin.

Le decoupage suit le rythme reel de l'usage, qui s'etale sur deux journees :

- **Le soir**, on met en file ce qui ne respecte pas la strategie. Ce geste ne
  coute rien et ne touche a rien.
- **La nuit**, un fil unique encode, un fichier a la fois, dans la plage
  horaire choisie. Rien n'est remplace.
- **Le matin**, on regarde les resultats — un lecteur video est branche dessus —
  et on remplace ceux qui conviennent.

Le fil de travail est un thread et non une tache asyncio : ffmpeg est un
processus externe qu'on attend en bloquant, et le faire depuis la boucle
d'evenements figerait toute l'application pendant des heures.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_settings
from ..core import reencode, transcode
from ..core.transcode import Job, State
from . import collection
from .deps import get_journal, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transcode", tags=["reencodage"])

TRASH_DIRNAME = ".corbeille"

_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_worker: threading.Thread | None = None
_stop = threading.Event()

VEILLE_SECONDES = 60.0
"""Frequence de reveil du fil quand il n'a rien a faire. Une minute : la plage
horaire se mesure en heures, verifier plus souvent ne ferait que consommer du
courant."""


# --- La file ----------------------------------------------------------------


class QueueRequest(BaseModel):
    paths: list[str] = []
    """Chemins RELATIFS a la racine de bibliotheque. Vide avec ``all`` = tout
    ce qui ne respecte pas la strategie."""

    all: bool = False


def _candidates() -> dict[str, reencode.Candidate]:
    """Ce qui ne respecte pas la strategie, indexe par chemin relatif.

    Recalcule a chaque appel depuis l'index du serveur : mettre en file un
    fichier d'apres ce que l'interface affichait il y a dix minutes reviendrait
    a encoder ce qui a peut-etre deja ete range, supprime ou remplace.
    """
    reglage = get_store().load().quality
    return {c.relative_path: c for c in reencode.audit(collection.current_works(), reglage)}


@router.post("/queue")
def enqueue(body: QueueRequest) -> dict[str, object]:
    """Met en file. Ne demarre rien : le fil de travail decidera de son heure."""
    conf = get_settings()
    disponibles = _candidates()
    vises = list(disponibles) if body.all else body.paths

    ajoutes = 0
    refuses: list[dict[str, str]] = []
    with _lock:
        deja = {
            job.relative_path
            for job in _jobs.values()
            if job.state in (State.QUEUED, State.RUNNING, State.DONE)
        }
        for relative in vises:
            candidat = disponibles.get(relative)
            if candidat is None:
                refuses.append({"path": relative, "message": "ne fait plus partie des candidats"})
                continue
            if relative in deja:
                refuses.append({"path": relative, "message": "deja en file"})
                continue

            source = conf.library_root / relative
            if not source.is_file():
                refuses.append({"path": relative, "message": "fichier introuvable"})
                continue

            job = transcode.new_job(
                source,
                relative,
                candidat.title,
                candidat.target,
                candidat.size_bytes,
                candidat.budget_bytes,
            )
            _jobs[job.id] = job
            deja.add(relative)
            ajoutes += 1

    if ajoutes:
        logger.info("reencodage : %s fichier(s) mis en file", ajoutes)
        _ensure_worker()
    return {"queued": ajoutes, "rejected": refuses, **_state()}


@router.post("/{job_id}/cancel")
def cancel(job_id: str) -> dict[str, object]:
    """Retire de la file. Un encodage EN COURS n'est pas interrompu.

    Tuer ffmpeg a la moitie jetterait le calcul deja fait, et le fichier
    d'origine n'est de toute facon pas en danger : rien ne sera remplace sans un
    geste explicite.
    """
    job = _get(job_id)
    if job.state is not State.QUEUED:
        raise HTTPException(409, "seul un travail en attente peut etre retire")
    with _lock:
        _jobs.pop(job_id, None)
    return _state()


@router.post("/{job_id}/replace")
def replace(job_id: str) -> dict[str, object]:
    """Installe le fichier reencode. L'original part en corbeille."""
    job = _get(job_id)
    conf = get_settings()
    ok, motif = transcode.replace(job, get_journal(), conf.library_root / TRASH_DIRNAME)
    if not ok:
        raise HTTPException(409, motif)
    # L'index porte desormais une taille fausse pour ce fichier : le laisser
    # ferait mentir les compteurs de place jusqu'a la prochaine relecture.
    collection.forget_index()
    return {"replaced": True, **_state()}


@router.post("/{job_id}/discard")
def discard(job_id: str) -> dict[str, object]:
    """Jette le resultat. L'original n'a jamais bouge."""
    transcode.discard(_get(job_id))
    return _state()


@router.post("/clear")
def clear() -> dict[str, object]:
    """Oublie les travaux termines, et efface ce qui traine sur le disque."""
    conf = get_settings()
    with _lock:
        restants = {
            job_id: job
            for job_id, job in _jobs.items()
            if job.state in (State.QUEUED, State.RUNNING, State.DONE)
        }
        _jobs.clear()
        _jobs.update(restants)
        vivants = list(_jobs.values())
    transcode.purge_staging(conf.library_root, vivants)
    return _state()


@router.get("")
def read_queue() -> dict[str, object]:
    return {**_state(), "candidates": _resume_candidats()}


def _resume_candidats() -> dict[str, object]:
    """Ce qui pourrait etre mis en file, sans le detail.

    Le detail est deja dans la vue mediatheque, fichier par fichier. Le
    repeter ici ferait payer deux fois le meme calcul a chaque
    rafraichissement.
    """
    try:
        candidats = list(_candidates().values())
    except Exception:
        # Un index absent n'est pas une erreur ici : la page doit s'afficher
        # meme quand la bibliotheque n'a pas encore ete relue.
        logger.debug("candidats indisponibles", exc_info=True)
        return {"count": 0, "recoverable_bytes": 0}
    return {
        "count": len(candidats),
        "recoverable_bytes": reencode.recoverable_bytes(candidats),
        "by_target": reencode.by_target(candidats),
    }


def _get(job_id: str) -> Job:
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "travail inconnu")
    return job


def _job_out(job: Job) -> dict[str, object]:
    return {
        "id": job.id,
        "title": job.title,
        "path": job.relative_path,
        "state": str(job.state),
        "target": f"{job.target_height}p",
        "source_bytes": job.source_bytes,
        "output_bytes": job.output_bytes,
        "savings_bytes": job.savings_bytes,
        "ratio": round(job.ratio, 3),
        "progress": round(job.progress, 3),
        "error": job.error,
        "queued_at": job.queued_at,
        "finished_at": job.finished_at,
        # Verifiable AVANT de remplacer : le controle automatique attrape un
        # encodage tronque, pas une image devenue laide.
        "playable": job.state is State.DONE,
    }


def _state() -> dict[str, object]:
    prefs = get_store().load().transcode
    with _lock:
        jobs = list(_jobs.values())
    return {
        "settings": {
            "enabled": prefs.enabled,
            "start_hour": prefs.start_hour,
            "end_hour": prefs.end_hour,
            "codec": prefs.codec,
            "crf": prefs.crf,
            "preset": prefs.preset,
        },
        "ffmpeg": transcode.ffmpeg_available(),
        "in_window": transcode.in_window(datetime.now(), prefs.start_hour, prefs.end_hour),
        "running": any(j.state is State.RUNNING for j in jobs),
        "queued": sum(1 for j in jobs if j.state is State.QUEUED),
        "done": sum(1 for j in jobs if j.state is State.DONE),
        "pending_savings_bytes": sum(j.savings_bytes for j in jobs if j.state is State.DONE),
        "jobs": [_job_out(j) for j in sorted(jobs, key=_rang)],
    }


def _rang(job: Job) -> tuple[int, str]:
    """Ce qui demande une decision d'abord, le reste ensuite."""
    ordre = {
        State.DONE: 0,  # attend une verification humaine
        State.RUNNING: 1,
        State.QUEUED: 2,
        State.FAILED: 3,
        State.REPLACED: 4,
        State.DISCARDED: 5,
    }
    return (ordre.get(job.state, 9), job.queued_at)


def job_output(job_id: str) -> Path | None:
    """Le fichier reencode, pour le lecteur video. None si indisponible."""
    job = _jobs.get(job_id)
    if job is None or job.state is not State.DONE or job.output is None:
        return None
    return job.output if job.output.is_file() else None


# --- Le fil de nuit ---------------------------------------------------------


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _stop.clear()
    _worker = threading.Thread(target=_boucle, name="sortilege-transcode", daemon=True)
    _worker.start()


def _boucle() -> None:
    """Encode tant qu'il reste du travail ET qu'on est dans la plage.

    Le fil s'arrete de lui-meme quand la file se vide : un thread qui dort pour
    rien est une ressource retenue sans raison, et le prochain ajout le
    relancera.
    """
    while not _stop.is_set():
        prefs = get_store().load().transcode
        with _lock:
            suivant = transcode.next_queued(list(_jobs.values()))

        if suivant is None:
            return

        if not prefs.enabled:
            logger.info("reencodage desactive dans les reglages : file en attente")
            return

        if not transcode.in_window(datetime.now(), prefs.start_hour, prefs.end_hour):
            # On ne CONSOMME pas la file hors plage : on attend l'heure. Le
            # fichier reste en attente, visible, et personne ne se demande
            # pourquoi rien ne bouge — l'interface affiche « hors plage ».
            if _stop.wait(VEILLE_SECONDES):
                return
            continue

        conf = get_settings()
        debut = time.monotonic()
        transcode.run(
            suivant,
            conf.library_root,
            codec=prefs.codec,
            crf=prefs.crf,
            preset=prefs.preset,
        )
        logger.info(
            "reencodage : %s termine en %.0f min (etat %s)",
            suivant.relative_path,
            (time.monotonic() - debut) / 60,
            suivant.state,
        )


def stop_worker() -> None:
    """Demande l'arret. Un encodage en cours va a son terme.

    Appele a l'extinction : ffmpeg ecrit dans un fichier a part, donc au pire on
    laisse un resultat partiel que le menage ramassera au demarrage suivant.
    """
    _stop.set()
