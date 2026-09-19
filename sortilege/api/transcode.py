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

La pause (``/pause``, ``/resume``) gele l'encodage en cours et retient la file.
Elle est enregistree dans le dossier de donnees : un redemarrage du conteneur
au milieu d'un film ne doit pas relancer ce qu'on venait d'arreter.

Les refus definitifs (Dolby Vision profil 5…) y sont enregistres aussi : un
fichier qu'on ne reencodera jamais ne doit plus etre propose chaque soir.
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
from ..core.preferences import TranscodeSettings
from ..core.transcode import Job, State
from . import collection, deps
from .deps import get_journal, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/transcode", tags=["reencodage"])

TRASH_DIRNAME = ".corbeille"

_jobs: dict[str, Job] = {}
_lock = threading.Lock()
_worker: threading.Thread | None = None
_stop = threading.Event()
_reveil = threading.Event()
"""Tire le fil de son sommeil : une reprise ne doit pas attendre la minute
suivante pour lancer le prochain fichier."""

VEILLE_SECONDES = 60.0
"""Frequence de reveil du fil quand il n'a rien a faire. Une minute : la plage
horaire se mesure en heures, verifier plus souvent ne ferait que consommer du
courant."""

_verrou_pause = threading.Lock()
_pause_relue = False
_perdu = ""
"""Titre de l'encodage suspendu qu'un redemarrage a fait perdre. Retenu pour le
DIRE : sans cela, le fichier disparaitrait de la file sans explication."""

_pause_non_enregistree = False

_verrou_refus = threading.Lock()


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

    Sans les fichiers refuses pour de bon (voir ``_refus``) : les proposer
    encore, c'est les voir revenir en echec chaque matin.
    """
    reglage = get_store().load().quality
    definitifs = _refus()
    return {
        c.relative_path: c
        for c in reencode.audit(collection.current_works(), reglage)
        if c.relative_path not in definitifs
    }


@router.post("/queue")
def enqueue(body: QueueRequest) -> dict[str, object]:
    """Met en file. Ne demarre rien : le fil de travail decidera de son heure."""
    conf = get_settings()
    disponibles = _candidates()
    definitifs = _refus()
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
            if relative in definitifs:
                # Le vrai motif, et non « ne fait plus partie des candidats » :
                # c'est lui qui explique pourquoi ce fichier ne partira jamais.
                refuses.append({"path": relative, "message": definitifs[relative].motif})
                continue
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
    """Jette le resultat. L'original n'a jamais bouge.

    Refuse sur un travail EN COURS, suspendu compris : son fichier etait
    supprime pendant que ffmpeg continuait d'y ecrire, puis la fin de
    l'encodage remettait le travail « termine » sur un fichier qui n'existait
    plus. Un encodage en cours se retire par la pause, ou attend sa fin.
    """
    job = _get(job_id)
    if job.state is State.RUNNING:
        raise HTTPException(
            409,
            "Cet encodage est en cours : il n'y a encore rien à jeter. "
            "Mets-le en pause si le NAS doit souffler, ou attends qu'il se termine.",
        )
    transcode.discard(job)
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


# --- La pause ---------------------------------------------------------------


@router.post("/pause")
def pause() -> dict[str, object]:
    """Gele l'encodage en cours et retient la file, jusqu'a la reprise.

    Le cas d'usage est l'imprevu : un film regarde pendant la plage de nuit, et
    le NAS qui rame. Il faut rendre le processeur TOUT DE SUITE — ne pas
    lancer le fichier suivant ne suffirait pas, l'encodage en cours peut durer
    encore deux heures.

    Valide sans encodage en cours : elle empeche alors le prochain de partir.
    Refusee seulement quand il n'y a rien du tout a retenir. Idempotente.
    """
    global _perdu
    _relire_pause()
    with _lock:
        jobs = list(_jobs.values())
    en_cours = next((j for j in jobs if j.state is State.RUNNING), None)
    en_attente = any(j.state is State.QUEUED for j in jobs)
    deja = transcode.en_pause()
    if not deja and en_cours is None and not en_attente:
        raise HTTPException(
            409, "Rien à mettre en pause : aucun réencodage en cours ni en attente."
        )

    gele = transcode.suspendre()
    if not deja:
        _perdu = ""
        logger.info(
            "reencodage mis en pause%s",
            f" : {gele.relative_path} suspendu" if gele else " (aucun encodage en cours)",
        )
    # Ce qui encodait est retenu, suspendu ou sur le point de l'etre (la pause
    # le gelera des son enregistrement) : c'est lui qu'un redemarrage ferait
    # perdre, et qu'il faudra nommer au retour.
    retenu = gele or en_cours
    travail = retenu.title if retenu is not None else _perdu
    _enregistrer_pause(transcode.PauseEnregistree(transcode.pause_depuis(), travail))
    return _state()


@router.post("/resume")
def resume() -> dict[str, object]:
    """Leve la pause : l'encodage gele repart ou il en etait, la file reprend.

    Acceptee meme file vide quand une pause est en place : apres un
    redemarrage, la file est vide mais la pause enregistree, et la refuser
    laisserait l'utilisateur sans moyen de la lever. Idempotente.
    """
    global _perdu
    _relire_pause()
    with _lock:
        jobs = list(_jobs.values())
    actifs = any(j.state in (State.QUEUED, State.RUNNING) for j in jobs)
    if not transcode.en_pause() and not actifs:
        raise HTTPException(
            409, "Rien à reprendre : aucun réencodage en pause, en cours ni en attente."
        )

    etait_en_pause = transcode.en_pause()
    # Appele meme hors pause : c'est sans effet, et cela garantit qu'aucun
    # processus ne reste gele quoi qu'il se soit passe avant.
    degele = transcode.relancer()
    if etait_en_pause:
        logger.info("reencodage repris%s", f" : {degele.relative_path} degele" if degele else "")
    _perdu = ""
    _enregistrer_pause(None)
    _reveil.set()
    if any(j.state is State.QUEUED for j in jobs):
        _ensure_worker()
    return _state()


def _fichier_pause() -> Path:
    """Lu a l'appel et non a l'import : les tests detournent ``deps.DATA_DIR``,
    et un import par valeur capturerait le chemin reel de l'utilisateur."""
    return deps.DATA_DIR / transcode.FICHIER_PAUSE


def _relire_pause() -> None:
    """Relit la pause enregistree, une seule fois par processus.

    Paresseux plutot qu'a l'import, pour la raison de ``_fichier_pause``. Les
    routes comme la boucle passent par ici AVANT de decider : aucun encodage
    ne peut donc partir avant que la pause d'avant le redemarrage ait ete
    relue.
    """
    global _pause_relue, _perdu
    with _verrou_pause:
        if _pause_relue:
            return
        _pause_relue = True
        enregistree = transcode.lire_pause(_fichier_pause())
        if enregistree is None:
            return
        transcode.restaurer_pause(enregistree.depuis)
        # La file ne survit pas au redemarrage : ce qui encodait au moment de
        # la pause est perdu, quoi qu'il ait pu devenir depuis.
        _perdu = enregistree.travail
    logger.info("reencodage : pause enregistree retrouvee au demarrage, file retenue")


def _enregistrer_pause(pause: transcode.PauseEnregistree | None) -> None:
    """Ecrit l'etat de pause. Un echec n'annule pas la pause.

    Le processeur est deja rendu, et c'est ce qui comptait : refuser la pause
    parce que le disque de donnees est plein serait punir l'utilisateur pour
    un confort secondaire. L'echec est en revanche DIT dans la note — la pause
    ne survivrait pas a un redemarrage.
    """
    global _pause_non_enregistree
    try:
        transcode.ecrire_pause(_fichier_pause(), pause)
    except OSError as exc:
        _pause_non_enregistree = pause is not None
        logger.warning("etat de pause non enregistre : %s", exc)
    else:
        _pause_non_enregistree = False


# --- Les refus definitifs ---------------------------------------------------


def _fichier_refus() -> Path:
    """Lu a l'appel, pour la meme raison que ``_fichier_pause``."""
    return deps.DATA_DIR / transcode.FICHIER_REFUS


def _refus() -> dict[str, transcode.RefusEnregistre]:
    """Refus definitifs qui valent encore, par chemin relatif.

    Un fichier remplace depuis (autre taille ou autre date) n'est plus celui
    qui a ete refuse : il redevient candidat.
    """
    racine = get_settings().library_root
    return {
        relatif: refus
        for relatif, refus in transcode.lire_refus(_fichier_refus()).items()
        if refus.vaut_pour(racine / relatif)
    }


def _retenir_refus(job: Job) -> None:
    """Enregistre un refus definitif. Un echec d'ecriture n'arrete pas la file.

    Au pire, le fichier sera propose de nouveau, et refuse de nouveau avant
    d'avoir coute la moindre minute d'encodage.
    """
    refus = transcode.refus_de(job)
    if refus is None:
        return
    with _verrou_refus:
        # Les refus perimes (fichier remplace, supprime) sont oublies au
        # passage : le fichier ne doit pas grossir d'annee en annee.
        definitifs = _refus()
        definitifs[job.relative_path] = refus
        try:
            transcode.ecrire_refus(_fichier_refus(), definitifs)
        except OSError as exc:
            logger.warning("refus de reencodage non enregistre : %s", exc)
            return
    logger.info("reencodage : %s ne sera plus propose (%s)", job.relative_path, job.error)


def _note(
    paused: bool, suspendu: Job | None, prefs: TranscodeSettings, dans_plage: bool
) -> str | None:
    """Ce que la pause fait, et ce qu'elle ne fait pas, en clair.

    Un bouton « Pause » laisse croire a trop de choses : que le fichier est
    arrete (il est gele, memoire comprise), que tout survit a un redemarrage
    (la pause oui, l'encodage non), que la reprise relancera tout (pas hors
    plage, pas si le reencodage est desactive). Chacune de ces confusions se
    paie en heures de calcul, ou en un NAS qui repart au mauvais moment.
    """
    if not paused:
        return None
    phrases: list[str] = []
    if suspendu is not None:
        phrases.append(
            "L'encodage en cours est suspendu : il ne consomme plus de processeur et "
            "reprendra exactement où il en était. Il garde sa mémoire tant qu'il est suspendu."
        )
    elif _perdu:
        phrases.append(
            f"La pause a été conservée au redémarrage de Sortilège, mais l'encodage de "
            f"« {_perdu} », qui était suspendu, a été perdu : il repartira de zéro une fois "
            "remis en file."
        )
    else:
        phrases.append("Le réencodage est en pause.")
    phrases.append("Aucun nouveau fichier ne démarre.")
    if suspendu is not None:
        phrases.append(
            "Si Sortilège redémarre pendant la pause, la pause sera conservée, "
            "mais cet encodage sera perdu."
        )

    if not prefs.enabled:
        phrases.append(
            "Le réencodage est aussi désactivé dans les réglages : la reprise "
            + ("terminera l'encodage suspendu, mais " if suspendu is not None else "")
            + "ne lancera aucun autre fichier tant qu'il le restera."
        )
    elif not dans_plage:
        phrases.append(
            "Hors de la plage horaire : à la reprise, l'encodage suspendu ira à son terme, "
            "mais le fichier suivant attendra l'ouverture de la plage."
            if suspendu is not None
            else "Hors de la plage horaire : même après la reprise, rien ne démarrera "
            "avant l'ouverture de la plage."
        )
    if _pause_non_enregistree:
        phrases.append(
            "La pause n'a pas pu être enregistrée sur le disque : "
            "elle ne survivrait pas à un redémarrage."
        )
    return " ".join(phrases)


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
        "by_kind": _reglages_par_type(),
    }


def _reglages_par_type() -> list[dict[str, object]]:
    """Ce que chaque type peut donner, et POURQUOI il ne donne rien.

    « Rien a reencoder » est vrai mais inutile : quelqu'un qui vient de regler
    ses series et ne voit aucune serie proposee ne peut pas savoir si c'est
    parce que ses fichiers sont deja conformes, ou parce que sa strategie ne
    demandera jamais rien.

    Le cas le plus frequent est le second : « Qualité maximale » ne classe
    AUCUNE resolution inferieure devant la resolution courante, donc elle ne
    propose jamais de reduire quoi que ce soit. C'est voulu — mais il faut le
    dire, sinon cela ressemble a une panne.
    """
    reglage = get_store().load().quality
    libelles = {"movie": "Films", "episode": "Séries", "anime": "Animes"}
    sortie = []
    for kind, libelle in libelles.items():
        strat = reglage.for_kind(kind)
        budget = reglage.budget_bytes(kind)
        reduit = any(reencode.target_for(strat, palier) for palier in ("2160p", "1080p", "720p"))
        sortie.append(
            {
                "kind": kind,
                "label": libelle,
                "strategy": strat.label,
                "budget_mb": budget // (1024 * 1024),
                "proposes": bool(reduit or budget),
                "why": (
                    ""
                    if reduit or budget
                    else f"« {strat.label} » ne demande jamais de réduire, "
                    "et aucun poids maximal n'est fixé"
                ),
            }
        )
    return sortie


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
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        # Verifiable AVANT de remplacer : le controle automatique attrape un
        # encodage tronque, pas une image devenue laide.
        "playable": job.state is State.DONE,
        "paused": job.suspended,
        # A retrancher de toute duree ou estimation calculee sur les horodatages
        # : la pause en cours y est deja comptee, a la seconde pres.
        "paused_seconds": round(job.seconds_paused(), 1),
        # HDR10+ ou Dolby Vision non conserves, audio converti : a lire AVANT
        # de remplacer l'original.
        "notes": list(job.notes),
        "permanent": job.definitif,
    }


def _state() -> dict[str, object]:
    _relire_pause()
    prefs = get_store().load().transcode
    with _lock:
        jobs = list(_jobs.values())
    paused = transcode.en_pause()
    dans_plage = transcode.in_window(datetime.now(), prefs.start_hour, prefs.end_hour)
    return {
        "settings": {
            "enabled": prefs.enabled,
            "start_hour": prefs.start_hour,
            "end_hour": prefs.end_hour,
            # Libelle et non nom d'encodeur : la pastille de la page dit
            # « HEVC 10 bits · CRF 21 ».
            "codec": transcode.LIBELLE_FORMAT,
            "crf": prefs.crf,
            "crf_uhd": transcode.crf_pour(2160, prefs.crf),
            "preset": prefs.preset,
            "compress_audio": prefs.compress_audio,
        },
        "ffmpeg": transcode.ffmpeg_available(),
        "in_window": dans_plage,
        "paused": paused,
        "paused_since": transcode.pause_depuis() or None,
        "note": _note(paused, transcode.travail_suspendu(), prefs, dans_plage),
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

    En pause, il ATTEND au lieu de s'arreter, comme hors plage : la file doit
    repartir a la reprise sans qu'on ait a la relancer.
    """
    while not _stop.is_set():
        _relire_pause()
        prefs = get_store().load().transcode
        with _lock:
            suivant = transcode.next_queued(list(_jobs.values()))

        if suivant is None:
            return

        if not prefs.enabled:
            logger.info("reencodage desactive dans les reglages : file en attente")
            return

        if transcode.en_pause():
            # Rien ne demarre pendant la pause. Si elle est levee entre ce
            # controle et le lancement, tant mieux ; si elle arrive entre les
            # deux, ``transcode.run`` gele ffmpeg des son enregistrement.
            if _dormir(VEILLE_SECONDES):
                return
            continue

        if not transcode.in_window(datetime.now(), prefs.start_hour, prefs.end_hour):
            # On ne CONSOMME pas la file hors plage : on attend l'heure. Le
            # fichier reste en attente, visible, et personne ne se demande
            # pourquoi rien ne bouge — l'interface affiche « hors plage ».
            if _dormir(VEILLE_SECONDES):
                return
            continue

        conf = get_settings()
        debut = time.monotonic()
        # Ce qu'est la source (HDR, Dolby Vision, pistes audio) et ce que sait
        # ffmpeg, lus AVANT de lancer : c'est ce qui permet de refuser un
        # Dolby Vision profil 5 sans avoir consomme une minute de calcul.
        preparation = transcode.preparer(suivant.source)
        transcode.run(
            suivant,
            conf.library_root,
            crf=prefs.crf,
            preset=prefs.preset,
            compresser_audio=prefs.compress_audio,
            preparation=preparation,
        )
        _retenir_refus(suivant)
        logger.info(
            "reencodage : %s termine en %.0f min (etat %s)",
            suivant.relative_path,
            (time.monotonic() - debut) / 60,
            suivant.state,
        )


def _dormir(secondes: float) -> bool:
    """Attend la minute suivante, une reprise ou l'arret. True = arret demande."""
    _reveil.wait(secondes)
    _reveil.clear()
    return _stop.is_set()


def stop_worker() -> None:
    """Demande l'arret. Un encodage en cours va a son terme.

    Appele a l'extinction : ffmpeg ecrit dans un fichier a part, donc au pire on
    laisse un resultat partiel que le menage ramassera au demarrage suivant.

    Un ffmpeg gele par la pause est DEGELE d'abord : suspendu, il ne traiterait
    jamais le signal de fin et survivrait, fige, a Sortilege. La pause reste
    enregistree — c'est elle que le prochain demarrage doit retrouver.
    """
    _stop.set()
    _reveil.set()
    transcode.degeler_pour_arret()
