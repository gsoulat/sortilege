"""Reencodage differe : la nuit, un fichier a la fois, sans rien remplacer.

Trois decisions structurent ce module, et elles decoulent toutes de la meme
observation : reencoder est **lent, couteux et destructeur**. Une heure de
processeur pour un film, plusieurs jours pour une bibliotheque, et une perte de
qualite qu'aucune annulation ne rendra.

1. **Le fichier d'origine n'est jamais touche pendant l'encodage.** Le resultat
   est ecrit A COTE. Une coupure de courant, un disque plein, un ffmpeg tue par
   l'OOM killer : dans tous les cas on perd du temps de calcul, jamais un film.

2. **Le remplacement est un geste humain, separe et posterieur.** Le lendemain,
   on regarde le resultat, et on decide. Un encodeur qui remplacerait tout seul
   demanderait de lui faire confiance sur une operation qu'on ne peut pas
   defaire — et il suffit d'un filtre mal choisi pour degrader deux cents
   episodes pendant la nuit.

3. **Un seul encodage a la fois.** Deux ffmpeg en parallele sur un NAS ne vont
   pas deux fois plus vite : ils se disputent le processeur et les disques, et
   rendent la machine inutilisable pour ce a quoi elle sert — servir des films.

La fenetre nocturne n'est pas un confort. C'est ce qui rend l'operation
acceptable : le NAS travaille quand personne ne regarde, et se tait quand
quelqu'un regarde.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from .journal import Journal, MoveRecord, _move
from .probe import probe_media

logger = logging.getLogger(__name__)

STAGING_DIRNAME = ".sortilege-reencodage"
"""Ou atterrissent les fichiers encodes, EN ATTENTE de verification.

Sous la racine de bibliotheque, et non dans le dossier de donnees : le
remplacement doit etre un renommage instantane. Depuis un autre volume, ce
serait une copie de plusieurs gigaoctets — precisement ce que le reencodage
cherchait a economiser.
"""

# Ecart de duree tolere entre l'original et le resultat. Un encodage interrompu
# produit un fichier PLUS COURT et plus petit : sans cette verification, il
# ressemblerait a une reussite particulierement efficace, et on remplacerait un
# film entier par ses vingt premieres minutes.
DUREE_TOLERANCE = 0.02

# Au-dela, le resultat n'a pas tenu sa promesse. Reencoder pour gagner cinq
# pour cent, c'est perdre de la qualite pour rien.
GAIN_MINIMAL = 0.10


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    """Encode et en attente de verification humaine."""

    FAILED = "failed"
    REPLACED = "replaced"
    DISCARDED = "discarded"


@dataclass
class Job:
    """Un reencodage demande, dans l'etat ou il se trouve."""

    id: str
    source: Path
    relative_path: str
    title: str
    target_height: int
    source_bytes: int
    budget_bytes: int = 0
    """Poids maximal souhaite. 0 = pas de consigne, encodage a qualite
    constante."""

    state: State = State.QUEUED
    output: Path | None = None
    output_bytes: int = 0
    source_duration: float = 0.0
    output_duration: float = 0.0
    progress: float = 0.0
    """0 -> 1. Calcule sur la duree encodee, seule mesure fiable : le nombre
    d'images depend du debit, et la taille du fichier ne progresse pas
    lineairement."""

    error: str = ""
    queued_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str = ""
    finished_at: str = ""

    @property
    def savings_bytes(self) -> int:
        if self.state not in (State.DONE, State.REPLACED) or not self.output_bytes:
            return 0
        return max(0, self.source_bytes - self.output_bytes)

    @property
    def ratio(self) -> float:
        """Part du poids d'origine conservee. 0.35 = le fichier a fondu aux
        deux tiers."""
        if not self.source_bytes or not self.output_bytes:
            return 0.0
        return self.output_bytes / self.source_bytes


def new_job(
    source: Path,
    relative_path: str,
    title: str,
    target: str,
    size_bytes: int,
    budget_bytes: int = 0,
) -> Job:
    return Job(
        id=uuid.uuid4().hex[:12],
        source=source,
        relative_path=relative_path,
        title=title,
        target_height=int(target.rstrip("pi") or 0),
        source_bytes=size_bytes,
        budget_bytes=budget_bytes,
    )


# --- La fenetre nocturne ----------------------------------------------------


def in_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    """Sommes-nous dans la plage autorisee ?

    La plage traverse minuit dans le cas normal — 23 h a 7 h — ce qui interdit
    la comparaison naive ``start <= h < end``. Une plage 23-7 lue ainsi ne
    serait JAMAIS vraie, et le reencodage n'aurait jamais lieu sans que rien ne
    l'explique.
    """
    if start_hour == end_hour:
        return True  # plage vide == aucune restriction
    heure = now.hour
    if start_hour < end_hour:
        return start_hour <= heure < end_hour
    return heure >= start_hour or heure < end_hour


# --- La commande ------------------------------------------------------------


AUDIO_KBPS = 192
"""Debit suppose des pistes audio, qui sont COPIEES et non reencodees. On le
soustrait du budget : l'oublier ferait viser un poids total superieur a la
consigne, et le budget ne serait jamais tenu."""

DEBIT_PLANCHER = 300
"""En dessous, l'image devient une bouillie de blocs. Mieux vaut depasser le
budget et le DIRE que rendre un fichier inregardable en silence."""


def bitrate_for(budget_bytes: int, duration_seconds: float, *, audio_kbps: int = AUDIO_KBPS) -> int:
    """Debit video, en kbit/s, pour tenir un budget de poids.

    C'est la traduction de « un episode doit peser 500 Mo au plus » en une
    consigne que ffmpeg comprend. La resolution n'y suffirait pas : deux
    fichiers en 1080p peuvent peser 1,2 Go et 6 Go selon leur debit.

    Zero quand il n'y a pas de budget ou pas de duree connue : on retombe alors
    sur le mode a qualite constante, qui est le bon defaut.
    """
    if budget_bytes <= 0 or duration_seconds <= 0:
        return 0
    total_kbps = (budget_bytes * 8) / duration_seconds / 1000
    return max(DEBIT_PLANCHER, int(total_kbps - audio_kbps))


def build_command(
    source: Path,
    output: Path,
    height: int,
    *,
    codec: str = "libx264",
    crf: int = 21,
    preset: str = "medium",
    bitrate_kbps: int = 0,
) -> list[str]:
    """Argv complet, sans shell.

    Ce qui est copie plutot que reencode compte autant que le reste :

    - **l'audio**, parce qu'il pese peu face a la video et que le reencoder
      couterait de la qualite sur les pistes VF/VO sans gagner de place ;
    - **les sous-titres et les pieces jointes**, sans quoi les polices des
      sous-titres ASS disparaitraient et l'affichage deviendrait illisible.

    ``scale=-2:h`` conserve le rapport d'image et force une largeur PAIRE, que
    les encodeurs H.264 et HEVC exigent — un nombre impair fait echouer
    l'encodage a la premiere image.
    """
    return [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        # Les pistes optionnelles sont marquees « ? » : un fichier sans
        # sous-titres ne doit pas faire echouer l'encodage.
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-map",
        "0:t?",
        "-c:v",
        codec,
        # Deux modes exclusifs. A qualite constante (CRF) par defaut : c'est ce
        # qui donne le meilleur resultat quand on ne vise pas un poids precis.
        # A debit cible quand un budget est fixe — « 500 Mo au plus » est une
        # consigne de POIDS, et le CRF ne sait pas la tenir.
        #
        # maxrate/bufsize bornent les pics : sans eux, une scene chargee ferait
        # depasser le budget que la moyenne respectait.
        *(
            [
                "-b:v",
                f"{bitrate_kbps}k",
                "-maxrate",
                f"{int(bitrate_kbps * 1.5)}k",
                "-bufsize",
                f"{bitrate_kbps * 2}k",
            ]
            if bitrate_kbps > 0
            else ["-crf", str(crf)]
        ),
        "-preset",
        preset,
        "-vf",
        f"scale=-2:{height}",
        "-c:a",
        "copy",
        "-c:s",
        "copy",
        "-progress",
        "pipe:1",
        "-loglevel",
        "error",
        str(output),
    ]


def staging_root(library_root: Path) -> Path:
    return library_root / STAGING_DIRNAME


def output_path(job: Job, library_root: Path) -> Path:
    """Ou ecrire le resultat.

    Toujours en Matroska : c'est le seul conteneur courant qui accepte
    l'ensemble des pistes qu'on recopie — plusieurs audios, sous-titres,
    polices. Ecrire un AVI reencode dans un AVI perdrait tout le reste.
    """
    dossier = staging_root(library_root)
    dossier.mkdir(parents=True, exist_ok=True)
    return dossier / f"{job.id}-{job.target_height}p.mkv"


# --- L'execution ------------------------------------------------------------


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def run(job: Job, library_root: Path, *, codec: str, crf: int, preset: str) -> Job:
    """Encode un fichier. Bloquant : appele depuis un fil dedie.

    Ne leve jamais. Un encodage rate est un etat du travail, pas une exception
    a remonter : la file doit continuer avec le suivant.
    """
    binaire = shutil.which("ffmpeg")
    if binaire is None:
        return _echec(job, "ffmpeg absent de l'image")

    if not job.source.is_file():
        return _echec(job, "fichier source introuvable")

    job.state = State.RUNNING
    job.started_at = datetime.now(UTC).isoformat()
    job.source_duration = probe_media(job.source).duration_seconds or 0.0
    sortie = output_path(job, library_root)
    job.output = sortie

    debit = bitrate_for(job.budget_bytes, job.source_duration)
    commande = build_command(
        job.source,
        sortie,
        job.target_height,
        codec=codec,
        crf=crf,
        preset=preset,
        bitrate_kbps=debit,
    )
    commande[0] = binaire
    logger.info(
        "reencodage demarre : %s -> %sp%s",
        job.relative_path,
        job.target_height,
        f", debit vise {debit} kbit/s" if debit else f", qualite constante (CRF {crf})",
    )

    try:
        processus = subprocess.Popen(  # noqa: S603 - argv fixe, pas de shell
            commande,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return _echec(job, f"lancement impossible : {exc}")

    _suivre(job, processus)
    code = processus.wait()
    erreur = (processus.stderr.read() if processus.stderr else "").strip()

    if code != 0:
        # Le fichier partiel est retire : le laisser ferait croire a un
        # resultat disponible, et il occuperait la place qu'on cherchait a
        # gagner.
        sortie.unlink(missing_ok=True)
        return _echec(job, erreur.splitlines()[-1] if erreur else f"ffmpeg a echoue (code {code})")

    return _conclure(job, sortie)


def _suivre(job: Job, processus: subprocess.Popen) -> None:
    """Lit la progression de ffmpeg au fil de l'eau.

    ``-progress pipe:1`` ecrit des lignes ``cle=valeur``. On ne retient que le
    temps encode : c'est la seule mesure qui avance de facon reguliere.
    """
    if processus.stdout is None:
        return
    for ligne in processus.stdout:
        if not ligne.startswith("out_time_us=") or job.source_duration <= 0:
            continue
        try:
            secondes = int(ligne.split("=", 1)[1]) / 1_000_000
        except ValueError:
            continue
        job.progress = min(1.0, secondes / job.source_duration)


def _conclure(job: Job, sortie: Path) -> Job:
    job.output_bytes = sortie.stat().st_size if sortie.is_file() else 0
    job.output_duration = probe_media(sortie).duration_seconds or 0.0
    job.progress = 1.0
    job.finished_at = datetime.now(UTC).isoformat()
    job.state = State.DONE
    logger.info(
        "reencodage termine : %s — %s -> %s octets (%.0f %% du poids d'origine)",
        job.relative_path,
        job.source_bytes,
        job.output_bytes,
        job.ratio * 100,
    )
    return job


def _echec(job: Job, message: str) -> Job:
    job.state = State.FAILED
    job.error = message
    job.finished_at = datetime.now(UTC).isoformat()
    logger.warning("reencodage echoue : %s — %s", job.relative_path, message)
    return job


# --- La verification, puis le remplacement ----------------------------------


def verify(job: Job) -> tuple[bool, str]:
    """Le resultat est-il remplacable ? Refuser vaut mieux que se tromper.

    La verification de DUREE est la plus importante, et la moins evidente : un
    encodage interrompu produit un fichier plus court ET plus petit. Sans elle,
    il ressemblerait a une reussite particulierement efficace — et on
    remplacerait un film entier par ses vingt premieres minutes.
    """
    if job.state is not State.DONE:
        return False, "ce reencodage n'est pas termine"
    if job.output is None or not job.output.is_file():
        return False, "le fichier reencode a disparu"
    if not job.source.is_file():
        return False, "le fichier d'origine a disparu — rien a remplacer"
    if job.output_bytes <= 0:
        return False, "le fichier reencode est vide"

    if job.source_duration > 0 and job.output_duration > 0:
        ecart = abs(job.output_duration - job.source_duration) / job.source_duration
        if ecart > DUREE_TOLERANCE:
            return False, (
                f"durees incoherentes : {job.source_duration:.0f} s a l'origine, "
                f"{job.output_duration:.0f} s apres — encodage probablement interrompu"
            )

    if job.budget_bytes and job.output_bytes > job.budget_bytes * 1.15:
        # Le debit vise n'a pas suffi : cela arrive sur une source tres
        # chargee. On ne bloque pas — le fichier est peut-etre tres bien — mais
        # on ne laisse pas croire que le budget a ete tenu.
        logger.info(
            "budget depasse pour %s : %s octets vises, %s obtenus",
            job.relative_path,
            job.budget_bytes,
            job.output_bytes,
        )

    if job.ratio > 1 - GAIN_MINIMAL:
        return False, (
            f"le gain est negligeable ({100 - job.ratio * 100:.0f} %) — "
            "remplacer perdrait de la qualite sans rendre de place"
        )
    return True, ""


def replace(job: Job, journal: Journal, trash_root: Path | None) -> tuple[bool, str]:
    """Met le fichier reencode a la place de l'original, qui part en corbeille.

    Meme ordre que partout ailleurs dans l'application : l'original part
    D'ABORD, ce qui libere la place, et il est REMIS si la suite echoue. Laisser
    la bibliotheque sans le fichier serait pire que de n'avoir rien tente.

    Jamais de suppression : un reencodage peut etre visuellement decevant sans
    que le controle automatique l'ait vu, et cela ne se decouvre parfois qu'en
    regardant le film.
    """
    ok, motif = verify(job)
    if not ok:
        return False, motif
    if trash_root is None:
        return False, "aucune corbeille configuree"

    if job.output is None:  # deja garanti par verify, garde pour le typage
        return False, "aucun fichier reencode"
    lot = datetime.now(UTC).strftime("%Y-%m-%d")
    ecarte = trash_root / lot / job.source.name
    ecarte.parent.mkdir(parents=True, exist_ok=True)
    if ecarte.exists():
        ecarte = ecarte.with_name(f"{job.id}-{job.source.name}")

    try:
        methode = _move(job.source, ecarte)
    except OSError as exc:
        return False, f"impossible d'ecarter l'original : {exc}"
    _inscrire(journal, job, job.source, ecarte, methode, "trash")

    # Le remplacant prend le nom de l'original, avec l'extension du conteneur
    # reellement produit : un AVI reencode devient un MKV, et pretendre le
    # contraire donnerait un fichier que certains lecteurs refuseraient.
    destination = job.source.with_suffix(job.output.suffix)
    try:
        methode = _move(job.output, destination)
    except OSError as exc:
        try:
            _move(ecarte, job.source)
        except OSError:
            logger.exception("restauration impossible apres echec : %s", job.source)
        return False, f"impossible d'installer le fichier reencode : {exc}"

    _inscrire(journal, job, job.output, destination, methode, "video")
    job.state = State.REPLACED
    logger.info("fichier remplace : %s (%s octets rendus)", destination, job.savings_bytes)
    return True, ""


def discard(job: Job) -> None:
    """Jette le resultat sans rien remplacer. L'original n'a jamais bouge."""
    if job.output is not None:
        job.output.unlink(missing_ok=True)
    job.state = State.DISCARDED


def _inscrire(
    journal: Journal, job: Job, source: Path, destination: Path, methode: str, genre: str
) -> None:
    journal.append(
        MoveRecord(
            timestamp=datetime.now(UTC).isoformat(),
            plan_id=f"reencodage:{job.id}",
            source=str(source),
            destination=str(destination),
            method=methode,
            kind=genre,
            title=job.title,
        )
    )


def purge_staging(library_root: Path, jobs: list[Job]) -> int:
    """Efface les fichiers en attente qui ne correspondent plus a aucun travail.

    Un redemarrage pendant un encodage laisse un fichier partiel que plus rien
    ne reclame. Sans ce menage, il occuperait indefiniment la place qu'on
    cherchait a liberer.
    """
    dossier = staging_root(library_root)
    if not dossier.is_dir():
        return 0
    connus = {job.output for job in jobs if job.output is not None}
    efface = 0
    for fichier in dossier.iterdir():
        if fichier.is_file() and fichier not in connus:
            fichier.unlink(missing_ok=True)
            efface += 1
    if efface:
        logger.info("menage du dossier de reencodage : %s fichier(s) orphelin(s)", efface)
    return efface


def next_queued(jobs: list[Job]) -> Job | None:
    """Le prochain a encoder. Un seul a la fois, dans l'ordre d'arrivee."""
    if any(job.state is State.RUNNING for job in jobs):
        return None
    return next((job for job in jobs if job.state is State.QUEUED), None)
