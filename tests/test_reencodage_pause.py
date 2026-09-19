"""Pause et reprise du reencodage.

La pause sert un moment precis : quelqu'un regarde un film pendant la plage de
nuit, et le NAS rame. Elle doit donc rendre le processeur TOUT DE SUITE — ne
pas lancer le fichier suivant ne suffirait pas, l'encodage en cours peut durer
encore deux heures — puis reprendre exactement ou l'encodage en etait, sans
jeter le calcul deja fait.

D'ou ce qui est verifie ici : le processus en cours est gele (SIGSTOP) et
degele (SIGCONT), rien ne demarre pendant la pause, la pause survit a un
redemarrage, et le temps passe en pause est compte a part — sans quoi la duree
affichee et le temps restant seraient faux d'autant.

Pas de vrai ffmpeg : un double de ``Popen`` pour les signaux, et un petit
script qui imite la sortie ``-progress`` pour verifier qu'un vrai processus
gele cesse reellement d'avancer.
"""

from __future__ import annotations

import importlib
import io
import json
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps
from sortilege.api import transcode as api
from sortilege.core import lecture, transcode
from sortilege.core.preferences import Preferences, TranscodeSettings
from sortilege.core.probe import FileProbe
from sortilege.core.transcode import Job, State

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGSTOP"), reason="la pause repose sur SIGSTOP/SIGCONT (POSIX)"
)


# --- Doubles ----------------------------------------------------------------


class FauxFfmpeg:
    """Double de ``Popen`` : un ffmpeg qui n'avance que quand le test le decide,
    et qui NOTE les signaux au lieu de les subir."""

    lances: ClassVar[list[FauxFfmpeg]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.pid = 999_999
        self.returncode: int | None = None
        self.signaux: list[int] = []
        self.refus: type[OSError] | None = None
        self.fin = threading.Event()
        self.stdout = self._progression()
        self.stderr = io.StringIO("")
        FauxFfmpeg.lances.append(self)

    def _progression(self):
        yield "out_time_us=10000000\n"
        self.fin.wait(10)

    def send_signal(self, sig: int) -> None:
        if self.refus is not None:
            raise self.refus()
        self.signaux.append(sig)

    def wait(self) -> int:
        self.returncode = 0
        return 0


class FauxReglages:
    """Magasin de preferences reduit a ce que lit la file."""

    def __init__(self, **reglage: object) -> None:
        base = {"enabled": True, "start_hour": 0, "end_hour": 0}
        self.prefs = Preferences(transcode=TranscodeSettings(**{**base, **reglage}))

    def load(self) -> Preferences:
        return self.prefs


def attendre(condition: Callable[[], bool], delai: float = 5.0) -> bool:
    fin = time.monotonic() + delai
    while time.monotonic() < fin:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def un_travail(racine: Path, titre: str = "Dune", etat: State = State.QUEUED) -> Job:
    source = racine / "media" / "Films" / f"{titre}.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x" * 100)
    job = transcode.new_job(source, f"Films/{titre}.mkv", titre, "1080p", 10 * 1024**3)
    job.state = etat
    return job


def en_file(racine: Path, titre: str = "Dune", etat: State = State.QUEUED) -> Job:
    job = un_travail(racine, titre, etat)
    api._jobs[job.id] = job
    return job


_fils: list[threading.Thread] = []


def lancer(job: Job, racine: Path) -> threading.Thread:
    """``run`` dans son propre fil, comme le fait la boucle de nuit.

    Avec une source SDR deja analysee, comme la boucle la prepare : sans elle,
    ``run`` lancerait ffprobe lui-meme, et les doubles de ``Popen`` verraient
    passer autre chose que ffmpeg.
    """
    image = {"color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709"}
    sdr = transcode.Preparation(
        analyse=lecture.lire_sonde(
            {"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p"}]},
            {"frames": [{"pix_fmt": "yuv420p", **image}]},
            nom=job.source.name,
        )
    )
    fil = threading.Thread(
        target=transcode.run,
        args=(job, racine / "media"),
        kwargs={"codec": "libx264", "crf": 21, "preset": "medium", "preparation": sdr},
        daemon=True,
    )
    fil.start()
    _fils.append(fil)
    return fil


@pytest.fixture(autouse=True)
def file_isolee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Chaque test part d'un processus « neuf » : pas de pause, file vide,
    dossier de donnees a lui."""
    monkeypatch.setattr(deps, "DATA_DIR", tmp_path / "donnees")
    monkeypatch.setattr(transcode, "_controle", transcode._Controle())
    monkeypatch.setattr(api, "_pause_relue", False)
    monkeypatch.setattr(api, "_perdu", "")
    monkeypatch.setattr(api, "_pause_non_enregistree", False)
    monkeypatch.setattr(api, "_jobs", {})
    monkeypatch.setattr(api, "_worker", None)
    monkeypatch.setattr(api, "_stop", threading.Event())
    monkeypatch.setattr(api, "_reveil", threading.Event())
    monkeypatch.setattr(api, "VEILLE_SECONDES", 0.05)
    monkeypatch.setattr(api, "get_store", lambda: FauxReglages())
    monkeypatch.setattr(FauxFfmpeg, "lances", [])
    yield
    # Rien ne doit survivre au test : ni un processus gele (un vrai resterait
    # fige apres la suite), ni un fil qui lirait l'etat du test suivant.
    transcode.degeler_pour_arret()
    api._stop.set()
    api._reveil.set()
    for faux in FauxFfmpeg.lances:
        faux.fin.set()
    while _fils:
        _fils.pop().join(15)
    if api._worker is not None:
        api._worker.join(5)


@pytest.fixture
def faux_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run`` lance le double au lieu de ffmpeg, sans sonde ni binaire."""
    monkeypatch.setattr(transcode.shutil, "which", lambda nom: f"/faux/{nom}")
    monkeypatch.setattr(transcode.subprocess, "Popen", FauxFfmpeg)
    monkeypatch.setattr(transcode, "probe_media", lambda _chemin: FileProbe(duration_seconds=100.0))


@pytest.fixture
def encodages(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Remplace l'encodage par un enregistrement : ce qui compte ici, c'est
    QUAND la boucle decide de lancer, pas ce que ffmpeg produit."""
    lances: list[str] = []

    def faux_run(job: Job, _racine: Path, **_reglage: object) -> Job:
        lances.append(job.title)
        job.state = State.DONE
        return job

    monkeypatch.setattr(transcode, "run", faux_run)
    return lances


# --- Le processus en cours est gele, puis degele -----------------------------


def test_la_pause_gele_ffmpeg_et_la_reprise_le_degele(tmp_path: Path, faux_ffmpeg) -> None:
    job = un_travail(tmp_path)
    fil = lancer(job, tmp_path)
    assert attendre(lambda: job.progress > 0), "l'encodage simule n'a pas demarre"
    ffmpeg = FauxFfmpeg.lances[0]

    assert transcode.suspendre() is job
    assert ffmpeg.signaux == [signal.SIGSTOP]
    assert job.suspended
    assert transcode.travail_suspendu() is job

    assert transcode.relancer() is job
    assert ffmpeg.signaux == [signal.SIGSTOP, signal.SIGCONT]
    assert not job.suspended
    assert not transcode.en_pause()

    ffmpeg.fin.set()
    fil.join(5)
    assert job.state is State.DONE


def test_deux_pauses_de_suite_ne_cassent_rien(tmp_path: Path, faux_ffmpeg) -> None:
    """Un double clic ne doit ni renvoyer de signal, ni remettre le compteur
    de pause a zero."""
    job = un_travail(tmp_path)
    lancer(job, tmp_path)
    assert attendre(lambda: job.progress > 0)
    ffmpeg = FauxFfmpeg.lances[0]

    transcode.suspendre()
    depuis = transcode.pause_depuis()
    debut = job.suspended_at
    transcode.suspendre()

    assert ffmpeg.signaux == [signal.SIGSTOP]
    assert transcode.pause_depuis() == depuis
    assert job.suspended_at == debut


def test_une_pause_sans_encodage_gele_le_suivant_des_son_lancement(
    tmp_path: Path, faux_ffmpeg
) -> None:
    """La course qui compte : la boucle a verifie « pas de pause », puis la
    pause arrive pendant la sonde de duree. ffmpeg ne doit pas tourner pour
    autant — il est gele des qu'il existe."""
    assert transcode.suspendre() is None
    assert transcode.en_pause()

    job = un_travail(tmp_path)
    lancer(job, tmp_path)
    assert attendre(lambda: bool(FauxFfmpeg.lances) and job.suspended)
    assert FauxFfmpeg.lances[0].signaux == [signal.SIGSTOP]

    transcode.relancer()
    assert FauxFfmpeg.lances[0].signaux == [signal.SIGSTOP, signal.SIGCONT]


def test_un_processus_deja_termine_ne_fait_pas_lever(tmp_path: Path) -> None:
    """ffmpeg qui se termine a l'instant ou l'on appuie sur pause est un cas
    normal : ni exception, ni travail marque suspendu a tort."""
    job = un_travail(tmp_path, etat=State.RUNNING)
    disparu = FauxFfmpeg()
    disparu.refus = ProcessLookupError
    transcode._prendre_en_charge(job, disparu)

    assert transcode.suspendre() is None
    assert not job.suspended
    assert transcode.en_pause(), "la pause reste voulue, pour le suivant"
    assert transcode.relancer() is None


def test_un_processus_qui_disparait_pendant_la_pause_ne_fait_pas_lever(tmp_path: Path) -> None:
    job = un_travail(tmp_path, etat=State.RUNNING)
    ffmpeg = FauxFfmpeg()
    transcode._prendre_en_charge(job, ffmpeg)
    transcode.suspendre()

    ffmpeg.refus = ProcessLookupError
    assert transcode.relancer() is job
    assert not job.suspended, "le compte de pause doit etre clos malgre tout"


def test_un_vrai_processus_termine_ne_recoit_rien(tmp_path: Path) -> None:
    """Meme verification sur un vrai processus, deja recolte : ``send_signal``
    ne lui envoie rien, et la pause ne le compte pas comme suspendu."""
    processus = subprocess.Popen([sys.executable, "-c", "pass"])
    processus.wait()
    job = un_travail(tmp_path, etat=State.RUNNING)
    transcode._prendre_en_charge(job, processus)

    assert transcode.suspendre() is None
    assert not job.suspended
    transcode._lacher(job)


FAUX_FFMPEG = """#!{python}
import time
for i in range(1, 150):
    print(f"out_time_us={{i * 1000000}}", flush=True)
    time.sleep(0.01)
"""


def test_un_vrai_processus_gele_cesse_d_avancer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SIGSTOP suspend REELLEMENT : la progression s'arrete net pendant la
    pause, et repart a la reprise. Sans /proc sur macOS, c'est l'absence de
    progression qui fait foi."""
    if " " in sys.executable:
        pytest.skip("chemin de l'interpreteur inutilisable dans un shebang")
    script = tmp_path / "ffmpeg"
    script.write_text(FAUX_FFMPEG.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(transcode.shutil, "which", lambda _nom: str(script))
    monkeypatch.setattr(
        transcode, "probe_media", lambda _chemin: FileProbe(duration_seconds=1000.0)
    )
    # Les vrais processus lances, pour les retrouver quoi qu'il arrive : une
    # assertion ratee entre la pause et la reprise laissait un python GELE
    # (SIGSTOP) survivre a la suite, invisible et impossible a arreter
    # autrement qu'a la main.
    lances: list[subprocess.Popen] = []

    class PopenNote(subprocess.Popen):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            lances.append(self)

    monkeypatch.setattr(transcode.subprocess, "Popen", PopenNote)

    job = un_travail(tmp_path)
    fil = lancer(job, tmp_path)
    try:
        assert attendre(lambda: job.progress >= 0.02), "le faux ffmpeg n'a pas demarre"

        assert transcode.suspendre() is job
        time.sleep(0.3)  # le temps de lire ce qui etait deja dans le tube
        fige = job.progress
        time.sleep(0.6)
        assert job.progress == fige, "le processus gele a continue d'avancer"

        transcode.relancer()
        assert attendre(lambda: job.progress > fige), "le processus n'est pas reparti"
        fil.join(15)
        assert job.state is State.DONE
        assert job.paused_seconds >= 0.8
    finally:
        transcode.relancer()
        for processus in lances:
            if processus.poll() is None:
                # SIGKILL est le seul signal qu'un processus gele recoit.
                processus.kill()
            processus.wait(5)
        fil.join(15)


# --- Le temps passe en pause -------------------------------------------------


def test_le_temps_de_pause_s_accumule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    maintenant = [100.0]
    monkeypatch.setattr(transcode, "horloge", lambda: maintenant[0])
    job = un_travail(tmp_path, etat=State.RUNNING)
    transcode._prendre_en_charge(job, FauxFfmpeg())

    transcode.suspendre()
    maintenant[0] = 130.0
    sortie = api._job_out(job)
    assert sortie["paused"] is True
    assert sortie["paused_seconds"] == 30.0, "la pause en cours compte des maintenant"

    transcode.relancer()
    maintenant[0] = 500.0
    assert job.seconds_paused() == 30.0, "hors pause, le compteur ne bouge plus"
    assert api._job_out(job)["paused"] is False

    transcode.suspendre()
    maintenant[0] = 510.0
    transcode.relancer()
    assert job.paused_seconds == 40.0

    transcode._lacher(job)


def test_seul_le_travail_suspendu_est_marque_en_pause(tmp_path: Path) -> None:
    en_cours = en_file(tmp_path, "Dune", State.RUNNING)
    suivant = en_file(tmp_path, "Alien")
    transcode._prendre_en_charge(en_cours, FauxFfmpeg())
    api.pause()

    marques = {j["title"]: j["paused"] for j in api._state()["jobs"]}
    assert marques == {"Dune": True, "Alien": False}
    assert suivant.seconds_paused() == 0.0


# --- Rien ne demarre pendant la pause ----------------------------------------


def test_aucun_travail_ne_demarre_pendant_la_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encodages: list[str]
) -> None:
    # Une longue veille prouve que la reprise REVEILLE le fil au lieu de le
    # laisser dormir jusqu'a la minute suivante.
    monkeypatch.setattr(api, "VEILLE_SECONDES", 30.0)
    en_file(tmp_path, "Dune")
    api.pause()
    api._ensure_worker()

    time.sleep(0.3)
    assert encodages == []
    assert api._worker is not None and api._worker.is_alive(), (
        "en pause, la file attend : elle ne s'arrete pas"
    )

    api.resume()
    assert attendre(lambda: encodages == ["Dune"], delai=3.0)


def test_une_pause_sans_encodage_en_cours_bloque_le_prochain_demarrage(
    tmp_path: Path, encodages: list[str]
) -> None:
    """Hors plage, rien ne tourne : la pause est valable quand meme, et le
    fichier en attente ne part pas a l'ouverture de la plage."""
    en_file(tmp_path, "Dune")
    etat = api.pause()
    assert etat["paused"] is True
    assert etat["running"] is False

    api._ensure_worker()
    time.sleep(0.3)
    assert encodages == []


def test_desactiver_pendant_la_pause_arrete_la_file_sans_lever_la_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encodages: list[str]
) -> None:
    en_file(tmp_path, "Dune")
    api.pause()
    monkeypatch.setattr(api, "get_store", lambda: FauxReglages(enabled=False))

    api._boucle()  # rend la main aussitot : desactive, comme aujourd'hui

    assert encodages == []
    etat = api._state()
    assert etat["paused"] is True
    assert "désactivé" in etat["note"]


def test_hors_plage_la_reprise_termine_l_encodage_mais_pas_le_suivant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    heure = datetime.now().hour
    monkeypatch.setattr(
        api,
        "get_store",
        lambda: FauxReglages(start_hour=(heure + 2) % 24, end_hour=(heure + 3) % 24),
    )
    en_cours = en_file(tmp_path, "Dune", State.RUNNING)
    ffmpeg = FauxFfmpeg()
    transcode._prendre_en_charge(en_cours, ffmpeg)
    en_file(tmp_path, "Alien")

    etat = api.pause()
    assert "fichier suivant attendra l'ouverture de la plage" in etat["note"]

    api.resume()
    assert ffmpeg.signaux == [signal.SIGSTOP, signal.SIGCONT], (
        "commence dans la plage, l'encodage va a son terme"
    )


# --- La pause survit au redemarrage ------------------------------------------


def test_la_pause_est_enregistree_atomiquement(tmp_path: Path) -> None:
    en_file(tmp_path, "Dune")
    api.pause()

    fichier = deps.DATA_DIR / transcode.FICHIER_PAUSE
    contenu = json.loads(fichier.read_text(encoding="utf-8"))
    assert contenu["paused"] is True
    assert contenu["paused_since"]
    assert [p.name for p in deps.DATA_DIR.iterdir()] == [transcode.FICHIER_PAUSE], (
        "aucun temporaire ne doit rester"
    )

    api.resume()
    assert not fichier.exists()


def test_la_pause_survit_au_rechargement_du_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encodages: list[str]
) -> None:
    en_cours = en_file(tmp_path, "Dune", State.RUNNING)
    transcode._prendre_en_charge(en_cours, FauxFfmpeg())
    api.pause()

    # Redemarrage simule : processus neuf (aucun ffmpeg, aucune pause en
    # memoire), module relu depuis zero — la file, elle, est perdue.
    monkeypatch.setattr(transcode, "_controle", transcode._Controle())
    importlib.reload(api)
    monkeypatch.setattr(api, "get_store", lambda: FauxReglages())
    monkeypatch.setattr(api, "VEILLE_SECONDES", 0.05)
    assert api._jobs == {}

    etat = api._state()
    assert etat["paused"] is True
    assert etat["paused_since"]
    assert "« Dune »" in etat["note"] and "perdu" in etat["note"], (
        "l'encodage perdu au redemarrage doit etre nomme, pas disparaitre en silence"
    )

    en_file(tmp_path, "Alien")
    api._ensure_worker()
    time.sleep(0.3)
    assert encodages == [], "une file en pause reste en pause apres redemarrage"

    api.resume()
    assert attendre(lambda: encodages == ["Alien"], delai=3.0)
    assert api._state()["note"] is None


def test_une_pause_non_enregistree_reste_active_et_le_dit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le processeur est rendu quoi qu'il arrive au disque de donnees ; seul
    le redemarrage ne la conservera pas, et il faut le dire."""
    bloquant = tmp_path / "pas-un-dossier"
    bloquant.write_text("")
    monkeypatch.setattr(deps, "DATA_DIR", bloquant)
    en_file(tmp_path, "Dune")

    etat = api.pause()
    assert etat["paused"] is True
    assert "n'a pas pu être enregistrée" in etat["note"]


def test_un_etat_de_pause_illisible_ne_bloque_pas_la_file(tmp_path: Path) -> None:
    fichier = tmp_path / transcode.FICHIER_PAUSE
    fichier.write_text("{pas du json", encoding="utf-8")
    assert transcode.lire_pause(fichier) is None


# --- L'extinction ------------------------------------------------------------


def test_l_extinction_degele_ffmpeg_avant_d_arreter(tmp_path: Path) -> None:
    """Gele, ffmpeg ne traiterait jamais le signal de fin et survivrait, fige,
    a Sortilege. La pause, elle, reste enregistree pour le prochain demarrage."""
    en_cours = en_file(tmp_path, "Dune", State.RUNNING)
    ffmpeg = FauxFfmpeg()
    transcode._prendre_en_charge(en_cours, ffmpeg)
    api.pause()

    api.stop_worker()

    assert ffmpeg.signaux == [signal.SIGSTOP, signal.SIGCONT]
    assert api._stop.is_set()
    assert transcode.en_pause()
    assert transcode.lire_pause(deps.DATA_DIR / transcode.FICHIER_PAUSE) is not None


# --- Le contrat HTTP ---------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    from sortilege.main import app

    with TestClient(app) as c:
        reponse = c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"})
        assert reponse.status_code == 200
        yield c


def test_le_contrat_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    # Desactive : la reprise ne consomme pas la file, et la seconde reprise
    # trouve encore un travail en attente — le test ne depend d'aucune course.
    monkeypatch.setattr(api, "get_store", lambda: FauxReglages(enabled=False))
    refus = client.post("/api/transcode/pause")
    assert refus.status_code == 409
    assert "Rien à mettre en pause" in refus.json()["detail"]
    assert client.post("/api/transcode/resume").status_code == 409

    etat = client.get("/api/transcode").json()
    assert etat["paused"] is False
    assert etat["paused_since"] is None
    assert etat["note"] is None

    en_file(tmp_path, "Dune")
    etat = client.post("/api/transcode/pause").json()
    assert etat["paused"] is True
    assert etat["paused_since"]
    assert "Aucun nouveau fichier ne démarre." in etat["note"]
    assert etat["jobs"][0]["paused"] is False, "rien ne tournait : rien n'est suspendu"
    assert etat["jobs"][0]["paused_seconds"] == 0.0

    assert client.post("/api/transcode/pause").status_code == 200, "idempotent"

    etat = client.post("/api/transcode/resume").json()
    assert etat["paused"] is False
    assert etat["note"] is None
    assert client.post("/api/transcode/resume").status_code == 200, "idempotent"


def test_une_pause_retrouvee_file_vide_peut_etre_levee(client: TestClient) -> None:
    """Apres un redemarrage, la file est vide mais la pause enregistree : la
    reprise doit rester possible, sinon rien ne pourrait plus la lever."""
    transcode.ecrire_pause(
        deps.DATA_DIR / transcode.FICHIER_PAUSE,
        transcode.PauseEnregistree("2026-09-18T21:00:00+00:00"),
    )

    assert client.get("/api/transcode").json()["paused"] is True
    reponse = client.post("/api/transcode/resume")
    assert reponse.status_code == 200
    assert reponse.json()["paused"] is False


def test_jeter_un_encodage_en_cours_est_refuse(tmp_path) -> None:
    """Le fichier etait supprime pendant que ffmpeg y ecrivait encore, puis la
    fin de l'encodage remettait le travail « termine » sur un fichier absent."""
    from fastapi import HTTPException

    from sortilege.api import transcode as api
    from sortilege.core.transcode import State, new_job

    source = tmp_path / "film.mkv"
    source.write_bytes(b"x")
    job = new_job(source, "film.mkv", "Film", "1080p", 1)
    job.state = State.RUNNING
    api._jobs[job.id] = job
    try:
        try:
            api.discard(job.id)
        except HTTPException as exc:
            assert exc.status_code == 409
        else:
            raise AssertionError("jeter un encodage en cours doit etre refuse")
        assert job.state is State.RUNNING, "rien n'a bouge"
    finally:
        api._jobs.pop(job.id, None)
