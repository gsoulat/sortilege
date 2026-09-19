"""Sessions HLS du lecteur : les garde-fous d'un NAS, et aucun chemin venu du client.

Sans ffmpeg reel : les processus sont factices, les segments sont des fichiers
ecrits par le test. Ce qu'on verifie, c'est ce qui protege la machine et les
donnees — une seule conversion a la fois, rien qui survive a un lecteur ferme,
aucun fichier servi hors de sa session.
"""

from __future__ import annotations

import shutil
import signal
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import media, review
from sortilege.api import transcode as transcode_api
from sortilege.core import lecture
from sortilege.core import transcode as reencodage
from sortilege.core.lecture import EtatControle, Mode, RaisonFin
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision
from sortilege.main import app

LISTE = (
    "#EXTM3U\n#EXT-X-VERSION:7\n#EXT-X-TARGETDURATION:6\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
    '#EXT-X-MAP:URI="init.mp4"\n#EXTINF:6.000000,\nseg_00000.m4s\n'
)


class Processus:
    """Un ffmpeg factice : vit jusqu'a ce qu'on le tue, et note ce qu'on lui envoie."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.code: int | None = None
        self.signaux: list[int] = []
        self.tue = False
        self.pid = 4242

    def poll(self) -> int | None:
        return self.code

    def kill(self) -> None:
        self.tue = True
        self.code = -9

    def wait(self, timeout: float | None = None) -> int | None:
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signaux.append(sig)


class Horloge:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def horloge() -> Horloge:
    return Horloge()


@pytest.fixture
def lances() -> list[Processus]:
    return []


@pytest.fixture
def sessions(tmp_path: Path, horloge: Horloge, lances: list[Processus]) -> lecture.Sessions:
    def lancer(argv, _journal):
        processus = Processus(argv)
        lances.append(processus)
        return processus

    return lecture.Sessions(
        tmp_path / "lecture", lancer=lancer, horloge=horloge, entretien_auto=False
    )


def _analyse(codec: str = "hevc", pix_fmt: str = "yuv420p10le") -> lecture.Analyse:
    return lecture.Analyse(
        lue=True,
        nom="film.mkv",
        extension=".mkv",
        duree=7200.0,
        video_codec=codec,
        video_profil="Main 10" if codec == "hevc" else "High",
        pix_fmt=pix_fmt,
        profondeur=10 if "10" in pix_fmt else 8,
        largeur=3840,
        hauteur=2160,
    )


def _ouvrir(sessions: lecture.Sessions, cle: str, *, remux: bool = False, at: float = 0.0):
    analyse = _analyse("h264", "yuv420p") if remux else _analyse()
    decision = lecture.decider(analyse)
    assert decision.mode is (Mode.REMUX if remux else Mode.TRANSCODE)
    return sessions.ouvrir(
        cle=cle,
        source=Path(f"/films/{cle}.mkv"),
        analyse=analyse,
        decision=decision,
        at=at,
        ffmpeg="/usr/bin/ffmpeg",
    )


def _produire(session: lecture.Session, segments: int) -> None:
    """Ce qu'ecrirait ffmpeg : l'initialisation, la liste, les segments."""
    (session.dossier / "init.mp4").write_bytes(b"init")
    (session.dossier / "index.m3u8").write_text(LISTE, encoding="utf-8")
    for i in range(segments):
        (session.dossier / f"seg_{i:05d}.m4s").write_bytes(bytes(range(256)) * 4)


# --- Une seule conversion a la fois -------------------------------------------


def test_une_nouvelle_conversion_tue_la_precedente(sessions, lances) -> None:
    """Un 4K converti occupe tout un processeur de NAS : deux, et la machine ne
    sert plus les films qu'on regarde vraiment."""
    premiere = _ouvrir(sessions, "plan:a")
    seconde = _ouvrir(sessions, "plan:b")

    assert lances[0].tue is True
    assert lances[1].tue is False
    assert not premiere.dossier.exists()
    with pytest.raises(lecture.SessionInconnue) as refus:
        sessions.etat(premiere.id)
    assert refus.value.raison is RaisonFin.REMPLACEE
    assert "une autre conversion a démarré" in refus.value.motif
    assert sessions.etat(seconde.id)["mode"] == "transcode"


def test_un_reemballage_ne_tue_pas_la_conversion_d_un_autre_film(sessions, lances) -> None:
    _ouvrir(sessions, "plan:a")
    _ouvrir(sessions, "plan:b", remux=True)

    assert [p.tue for p in lances] == [False, False]


def test_un_saut_dans_le_meme_film_remplace_la_session(sessions, lances) -> None:
    premiere = _ouvrir(sessions, "plan:a", remux=True)
    _ouvrir(sessions, "plan:a", remux=True, at=3600)

    assert lances[0].tue is True
    with pytest.raises(lecture.SessionInconnue) as refus:
        sessions.etat(premiere.id)
    assert refus.value.raison is RaisonFin.RELANCEE
    assert lances[1].argv[lances[1].argv.index("-ss") + 1] == "3600.000"


def test_le_nombre_de_sessions_est_borne(sessions, lances, horloge) -> None:
    for i in range(lecture.SESSIONS_MAX + 1):
        horloge.t += 1
        _ouvrir(sessions, f"plan:{i}", remux=True)

    assert len(sessions.actives()) == lecture.SESSIONS_MAX
    assert lances[0].tue is True


# --- Rien ne survit a un lecteur ferme -----------------------------------------


def test_une_session_inactive_depuis_une_minute_est_arretee(sessions, lances, horloge) -> None:
    session = _ouvrir(sessions, "plan:a")
    _produire(session, 2)

    horloge.t += lecture.INACTIVITE + 1
    sessions.entretien()

    assert lances[0].tue is True
    assert not session.dossier.exists()
    with pytest.raises(lecture.SessionInconnue) as refus:
        sessions.etat(session.id)
    assert refus.value.raison is RaisonFin.INACTIVE


def test_un_signe_de_vie_garde_la_session(sessions, lances, horloge) -> None:
    session = _ouvrir(sessions, "plan:a")
    horloge.t += 40
    sessions.toucher(session.id)
    horloge.t += 40
    sessions.entretien()

    assert lances[0].tue is False


def test_lire_un_segment_est_un_signe_de_vie(sessions, lances, horloge) -> None:
    session = _ouvrir(sessions, "plan:a")
    _produire(session, 2)
    horloge.t += 50
    sessions.fichier(session.id, "seg_00001.m4s")
    horloge.t += 50
    sessions.entretien()

    assert lances[0].tue is False


def test_fermer_le_lecteur_tue_ffmpeg_tout_de_suite(sessions, lances) -> None:
    session = _ouvrir(sessions, "plan:a")
    assert sessions.arreter(session.id) is True

    assert lances[0].tue is True
    assert not session.dossier.exists()


def test_l_extinction_tue_toutes_les_conversions(sessions, lances) -> None:
    _ouvrir(sessions, "plan:a")
    _ouvrir(sessions, "plan:b", remux=True)
    sessions.arreter_tout()

    assert all(p.tue for p in lances)
    assert sessions.actives() == []


def test_les_dossiers_orphelins_sont_purges_au_demarrage(tmp_path) -> None:
    racine = tmp_path / "lecture"
    orphelin = racine / ("a" * 32)
    orphelin.mkdir(parents=True)
    (orphelin / "seg_00000.m4s").write_bytes(b"x")
    autre = racine / "a-garder"
    autre.mkdir()

    lecture.Sessions(racine, entretien_auto=False)

    assert not orphelin.exists()
    assert autre.exists(), "seuls les dossiers de session sont effaces"


# --- Le frein -----------------------------------------------------------------


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="signaux POSIX")
def test_ffmpeg_est_suspendu_quand_il_a_trop_d_avance_puis_relance(sessions, lances) -> None:
    """Sans frein, un reemballage produirait tout le film en quelques minutes :
    vingt gigaoctets de cache pour quelqu'un qui voulait voir trois scenes."""
    session = _ouvrir(sessions, "plan:a", remux=True)
    _produire(session, lecture.AVANCE_MAX + 1)

    sessions.entretien()
    assert lances[0].signaux == [signal.SIGSTOP]
    assert sessions.etat(session.id)["suspendue"] is True

    sessions.fichier(session.id, "seg_00006.m4s")
    assert lances[0].signaux == [signal.SIGSTOP, signal.SIGCONT]


def test_le_cache_deborde_libere_d_abord_ce_qui_a_ete_lu(tmp_path, horloge, lances) -> None:
    def lancer(argv, _journal):
        processus = Processus(argv)
        lances.append(processus)
        return processus

    sessions = lecture.Sessions(
        tmp_path / "lecture", lancer=lancer, horloge=horloge, entretien_auto=False, cache_max=40_000
    )
    session = _ouvrir(sessions, "plan:a", remux=True)
    _produire(session, 60)
    sessions.fichier(session.id, "seg_00050.m4s")

    sessions.entretien()

    restants = sorted(p.name for p in session.dossier.glob("seg_*.m4s"))
    assert restants[0] == f"seg_{50 - lecture.RETENUE:05d}.m4s"
    assert "seg_00059.m4s" in restants


# --- Aucun chemin venu du client -----------------------------------------------


@pytest.mark.parametrize(
    "nom",
    [
        "..",
        "../../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "/etc/passwd",
        "ffmpeg.log",
        "seg_00000.m4s.tmp",
        "index.m3u8.tmp",
        "seg_1.m4s",
        "init.mp4/../../x",
        "index.m3u8\x00",
        # Un « $ » accepte un saut de ligne final : les noms se comparent en entier.
        "init.mp4\n",
        "seg_00000.m4s\n",
    ],
)
def test_un_nom_hors_de_la_session_est_refuse(sessions, nom) -> None:
    session = _ouvrir(sessions, "plan:a")
    _produire(session, 1)
    (session.dossier / "ffmpeg.log").write_text("secret", encoding="utf-8")

    with pytest.raises(lecture.NomRefuse):
        sessions.fichier(session.id, nom)


def test_le_nom_est_refuse_avant_meme_de_chercher_la_session(sessions) -> None:
    """Une tentative de traversee ne touche pas au registre des sessions."""
    with pytest.raises(lecture.NomRefuse):
        sessions.fichier("pas-une-session", "../../etc/passwd")


@pytest.mark.parametrize("sid", ["..", "../x", "a" * 31, "A" * 32, "", "a" * 32 + "\n"])
def test_un_identifiant_de_session_invente_ne_donne_rien(sessions, sid) -> None:
    with pytest.raises(lecture.SessionInconnue):
        sessions.fichier(sid, "init.mp4")


def test_un_nom_valide_de_la_session_est_servi(sessions) -> None:
    session = _ouvrir(sessions, "plan:a")
    _produire(session, 1)

    assert sessions.fichier(session.id, "seg_00000.m4s") == session.dossier / "seg_00000.m4s"


def test_la_liste_se_lit_depuis_le_debut(sessions) -> None:
    """Sans EXT-X-START, une liste EVENT se lit « en direct », depuis la fin."""
    session = _ouvrir(sessions, "plan:a")
    _produire(session, 1)

    texte = sessions.liste(session.id, attente=0)
    assert texte.startswith("#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\n")


def test_une_conversion_morte_sans_rien_produire_le_dit(sessions, lances) -> None:
    session = _ouvrir(sessions, "plan:a")
    (session.dossier / "ffmpeg.log").write_text(
        "[hevc @ 0x7f0] Invalid data found\nConversion failed!\n", encoding="utf-8"
    )
    lances[0].code = 1

    with pytest.raises(lecture.ConversionEchouee, match="Conversion failed!"):
        sessions.liste(session.id, attente=0)


def test_l_effacement_refuse_tout_dossier_hors_du_cache(sessions, tmp_path) -> None:
    precieux = tmp_path / "bibliotheque" / ("b" * 32)
    precieux.mkdir(parents=True)
    sessions._effacer(precieux)
    assert precieux.exists()


# --- A travers l'API ----------------------------------------------------------


@pytest.fixture
def client():
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


@pytest.fixture
def via_api(sessions, monkeypatch):
    """Les routes utilisent le gestionnaire de test, et ffmpeg « existe »."""
    monkeypatch.setattr(media, "_sessions", sessions)
    monkeypatch.setattr(media, "_ffmpeg_available", lambda: True)
    monkeypatch.setattr(media.lecture, "filtres_ffmpeg", lambda: frozenset())
    monkeypatch.setattr(media.shutil, "which", lambda nom: f"/usr/bin/{nom}")
    return sessions


@pytest.fixture
def plan(tmp_path: Path, monkeypatch):
    source = tmp_path / "Kandahar.2023.mkv"
    source.write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 2048)
    p = Plan(
        id="lecture-hls",
        source=source,
        destination=tmp_path / "lib" / "x.mkv",
        kind="movie",
        score=0.8,
        decision=Decision.REVIEW,
        title="Kandahar",
    )
    review._plans[p.id] = p
    analyses = {source: _analyse("h264", "yuv420p10le")}
    monkeypatch.setattr(media.lecture, "analyser", lambda chemin: analyses[chemin])
    yield p
    review._plans.pop(p.id, None)


def test_le_plan_de_lecture_dit_pourquoi_il_convertit(client, via_api, plan) -> None:
    r = client.get(f"/api/media/plan/{plan.id}/lecture")

    assert r.status_code == 200
    assert r.json()["mode"] == "transcode"
    assert r.json()["motif"] == (
        "vidéo H.264 10 bits : aucun navigateur ne la décode, conversion de l'aperçu en 720p."
    )


def test_une_session_se_lit_segment_par_segment_avec_plages(client, via_api, plan) -> None:
    r = client.post(f"/api/media/plan/{plan.id}/session", params={"at": 600})
    assert r.status_code == 200
    sid = r.json()["session"]
    session = via_api.actives()[0]
    _produire(session, 2)

    liste = client.get(f"/api/media/lecture/{sid}/index.m3u8")
    assert liste.status_code == 200
    assert liste.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "#EXT-X-START" in liste.text

    segment = client.get(f"/api/media/lecture/{sid}/seg_00001.m4s", headers={"Range": "bytes=4-13"})
    assert segment.status_code == 206
    assert segment.headers["content-type"] == "video/mp4"
    assert segment.headers["content-length"] == "10"
    assert segment.headers["accept-ranges"] == "bytes"
    assert segment.content == bytes(range(4, 14))


@pytest.mark.parametrize("nom", ["ffmpeg.log", "index.m3u8.tmp", "seg_00000.m4s.tmp", "%2E%2E"])
def test_la_route_de_segments_refuse_tout_nom_hors_de_la_session(client, via_api, plan, nom):
    sid = client.post(f"/api/media/plan/{plan.id}/session").json()["session"]
    session = via_api.actives()[0]
    _produire(session, 1)
    (session.dossier / "ffmpeg.log").write_text("journal", encoding="utf-8")

    r = client.get(f"/api/media/lecture/{sid}/{nom}")
    assert r.status_code == 404
    assert b"journal" not in r.content


def test_une_traversee_encodee_n_atteint_aucun_fichier(client, via_api, plan) -> None:
    """« %2F » decode en « / » : la route de segments n'est meme pas atteinte (un
    nom ne contient pas de barre). Quoi qu'il reponde d'autre, ce n'est jamais un
    fichier du disque."""
    sid = client.post(f"/api/media/plan/{plan.id}/session").json()["session"]
    session = via_api.actives()[0]
    (session.dossier / "ffmpeg.log").write_text("journal", encoding="utf-8")

    for nom in ("..%2F..%2F..%2Fetc%2Fpasswd", "..%2Fffmpeg.log", "%2Fetc%2Fpasswd"):
        r = client.get(f"/api/media/lecture/{sid}/{nom}")
        assert r.headers.get("content-type", "").split(";")[0] != "video/mp4"
        assert b"root:" not in r.content
        assert b"journal" not in r.content


def test_le_navigateur_qui_decode_le_hevc_recoit_l_image_intacte(
    client, via_api, plan, monkeypatch
) -> None:
    """Firefox sur Mac declare « main10 » : un reencodage HEVC SDR se reemballe au
    lieu d'etre converti. Sans declaration, il est converti."""
    hevc = _analyse()
    monkeypatch.setattr(media.lecture, "analyser", lambda _chemin: hevc)

    sans = client.post(f"/api/media/plan/{plan.id}/session").json()
    avec = client.post(f"/api/media/plan/{plan.id}/session", params={"hevc": "main10"}).json()

    assert sans["mode"] == "transcode"
    assert avec["mode"] == "remux"
    argv = via_api.actives()[-1].processus.argv
    assert argv[argv.index("-tag:v") + 1] == "hvc1"
    assert (
        client.get(f"/api/media/plan/{plan.id}/lecture", params={"hevc": "main10"}).json()["mode"]
        == "remux"
    )


def test_une_declaration_hevc_inventee_est_refusee(client, via_api, plan) -> None:
    r = client.get(f"/api/media/plan/{plan.id}/lecture", params={"hevc": "../../x"})
    assert r.status_code == 422


def test_une_session_finie_dit_pourquoi(client, via_api, plan) -> None:
    sid = client.post(f"/api/media/plan/{plan.id}/session").json()["session"]
    assert client.post(f"/api/media/lecture/{sid}/stop").json() == {"arrete": True}

    r = client.get(f"/api/media/lecture/{sid}")
    assert r.status_code == 410
    assert r.json() == {"detail": "Aperçu fermé.", "raison": "fermee"}
    assert client.get(f"/api/media/lecture/{'f' * 32}").status_code == 404


def test_les_sessions_exigent_une_connexion(via_api) -> None:
    with TestClient(app) as anonyme:
        assert anonyme.get(f"/api/media/lecture/{'a' * 32}/index.m3u8").status_code == 401
        assert anonyme.post("/api/media/plan/x/session").status_code == 401


def test_la_fiche_comparee_d_un_reencodage_10_bits(client, tmp_path, monkeypatch) -> None:
    original = tmp_path / "Kandahar.2023.2160p.mkv"
    original.write_bytes(b"o")
    sortie = tmp_path / "reencode.mkv"
    sortie.write_bytes(b"r")
    job = reencodage.new_job(original, "Films/Kandahar.mkv", "Kandahar", "1080p", 21_500_000_000)
    job.state = reencodage.State.DONE
    job.output = sortie
    job.source_duration = 7200.0
    transcode_api._jobs[job.id] = job

    hdr10 = _analyse()
    hdr10.transfert = "smpte2084"
    dix_bits = _analyse("h264", "yuv420p10le")
    dix_bits.transfert = "smpte2084"
    monkeypatch.setattr(
        media.lecture, "analyser", lambda chemin: {original: hdr10, sortie: dix_bits}[chemin]
    )
    try:
        r = client.get(f"/api/media/transcode/{job.id}/comparaison")
    finally:
        transcode_api._jobs.pop(job.id, None)

    assert r.status_code == 200
    corps = r.json()
    assert corps["original"]["hdr"]["type"] == "hdr10"
    assert corps["reencode"]["video"]["libelle"] == "H.264 10 bits"
    assert corps["gravite"] == "grave"
    assert corps["verdicts"][0]["code"] == "h264_10_bits"
    assert corps["controle"]["etat"] == "aucun"


# --- Preparation, cache, extinction -------------------------------------------


def test_attendre_la_preparation_est_un_signe_de_vie(sessions, lances, horloge) -> None:
    """Un 4K converti peut mettre plus d'une minute a sortir son premier
    segment : le lecteur qui interroge l'etat en l'attendant n'est pas parti."""
    session = _ouvrir(sessions, "plan:a")
    for _ in range(3):
        horloge.t += 45
        sessions.etat(session.id)
        sessions.entretien()
    assert not lances[0].tue

    horloge.t += lecture.INACTIVITE + 1
    sessions.entretien()
    assert lances[0].tue


def test_un_saut_au_dela_de_la_fin_reelle_le_dit(sessions, lances) -> None:
    """ffmpeg s'arrete normalement sans une image : ce n'est pas « a echoue »,
    c'est un fichier qui s'arrete avant l'endroit demande."""
    session = _ouvrir(sessions, "plan:a", at=6800.0)
    lances[0].code = 0

    with pytest.raises(lecture.ConversionEchouee) as erreur:
        sessions.liste(session.id, attente=1)
    assert str(erreur.value) == (
        "aucune image à partir de 1:53:20 : le fichier s'arrête avant, alors qu'il annonce 2:00:00"
    )


def _produire_gros(session: lecture.Session, segments: int, taille: int = 10_000) -> None:
    (session.dossier / "init.mp4").write_bytes(b"init")
    (session.dossier / "index.m3u8").write_text(LISTE, encoding="utf-8")
    for i in range(segments):
        (session.dossier / f"seg_{i:05d}.m4s").write_bytes(b"\0" * taille)


@pytest.fixture
def petit_cache(tmp_path, horloge, lances) -> lecture.Sessions:
    def lancer(argv, _journal):
        processus = Processus(argv)
        lances.append(processus)
        return processus

    return lecture.Sessions(
        tmp_path / "lecture", lancer=lancer, horloge=horloge, entretien_auto=False, cache_max=40_000
    )


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="signaux POSIX")
def test_le_plafond_tient_aussi_pour_la_derniere_session(petit_cache, lances) -> None:
    """Six segments de 10 Ko pour un plafond de 40 Ko, et rien de lu : bien
    moins que les huit segments d'avance permis, mais le cache deborde. Avant,
    la derniere session n'etait jamais freinee — 272 Mo pour un plafond de 60."""
    session = _ouvrir(petit_cache, "plan:a", remux=True)
    _produire_gros(session, 6)

    petit_cache.entretien()
    assert lances[0].signaux == [signal.SIGSTOP]

    # Le lecteur avance : ce qu'il a lu est libere, et la production reprend
    # une fois le cache revenu sous 80 % du plafond.
    for i in range(5):
        petit_cache.fichier(session.id, f"seg_{i:05d}.m4s")
    assert lances[0].signaux == [signal.SIGSTOP]
    petit_cache.entretien()

    restants = sorted(p.name for p in session.dossier.glob("seg_*.m4s"))
    assert restants == ["seg_00004.m4s", "seg_00005.m4s"]
    assert lances[0].signaux == [signal.SIGSTOP, signal.SIGCONT]


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="signaux POSIX")
def test_un_cache_plein_ne_bloque_pas_le_lecteur_qui_attend(petit_cache, lances) -> None:
    """Le lecteur a tout lu et attend le suivant : suspendre alors bloquerait la
    lecture pour de bon. Le plafond cede du seul segment attendu."""
    session = _ouvrir(petit_cache, "plan:a", remux=True)
    _produire_gros(session, 6, taille=40_000)
    petit_cache.fichier(session.id, "seg_00005.m4s")

    petit_cache.entretien()
    assert signal.SIGSTOP not in lances[0].signaux


def test_l_extinction_du_serveur_tue_les_conversions(via_api, plan, lances) -> None:
    """``atexit`` ne passe pas quand uvicorn est interrompu hors de Docker : c'est
    le cycle de vie de l'application qui arrete ffmpeg."""
    with TestClient(app) as c:
        # Le demarrage de l'application peut recharger les plans : on remet le
        # notre apres lui.
        review._plans[plan.id] = plan
        assert c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code
        assert c.post(f"/api/media/plan/{plan.id}/session").status_code == 200
        assert not lances[0].tue
    assert lances[0].tue
    assert via_api.actives() == []


def test_un_arret_avec_un_identifiant_a_saut_de_ligne_n_arrete_rien(client, via_api, plan):
    sid = client.post(f"/api/media/plan/{plan.id}/session").json()["session"]

    assert client.post(f"/api/media/lecture/{sid}%0A/stop").json() == {"arrete": False}
    assert via_api.actives()[0].id == sid


# --- La mediatheque decide avec les capacites du navigateur -------------------


def test_la_mediatheque_decide_avec_les_capacites_du_navigateur(
    client, via_api, plan, monkeypatch
) -> None:
    """Un WebM VP9 que Firefox lit tel quel etait converti par le NAS ; un HEVC
    en MKV n'avait plus de lecteur. La mediatheque pose maintenant la meme
    question que le lecteur, avec les memes capacites."""
    webm = lecture.Analyse(
        lue=True,
        nom="f.webm",
        extension=".webm",
        duree=60.0,
        video_codec="vp9",
        pix_fmt="yuv420p",
        profondeur=8,
        audio=[lecture.PisteAudio("opus")],
    )
    monkeypatch.setattr(media.lecture, "analyser", lambda _chemin: webm)
    monkeypatch.setattr(media, "_duration_seconds", lambda _chemin: 60.0)
    route = f"/api/media/plan/{plan.id}/positions"

    sans = client.get(route).json()["remux"]
    avec = client.get(route, params={"vp9": "8", "av1": "10", "hevc": "main10"}).json()["remux"]
    assert (sans["possible"], sans["mode"]) == (False, "transcode")
    assert (avec["possible"], avec["mode"]) == (True, "direct")

    hevc = _analyse()
    monkeypatch.setattr(media.lecture, "analyser", lambda _chemin: hevc)
    assert client.get(route).json()["remux"]["possible"] is False
    assert client.get(route, params={"hevc": "main10"}).json()["remux"]["mode"] == "remux"
    # Le lecteur, lui, recoit la meme decision.
    lecture_plan = client.get(f"/api/media/plan/{plan.id}/lecture", params={"hevc": "main10"})
    assert lecture_plan.json()["mode"] == "remux"


@pytest.mark.parametrize("parametres", [{"vp9": "9"}, {"av1": "10\n"}, {"vp9": "evil"}])
def test_une_capacite_inventee_est_refusee(client, via_api, plan, parametres) -> None:
    assert client.get(f"/api/media/plan/{plan.id}/lecture", params=parametres).status_code == 422
    assert client.get(f"/api/media/plan/{plan.id}/positions", params=parametres).status_code == 422


# --- Avec le vrai ffmpeg ------------------------------------------------------
#
# Des fichiers minuscules fabriques a la volee, qui reproduisent chacun un defaut
# releve par le controle : saut au-dela de la fin d'un fichier tronque, paquet
# AC-3 coupe au saut dans un VOB sain, fenetre sans image clé dans un MPEG-TS.

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
reel = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg absent")

MIRE = [
    "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24:duration={d}",
    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration={d}",
    "-map", "0", "-map", "1",
]  # fmt: skip


def _fabriquer(sortie: Path, duree: int, *options: str) -> Path:
    mire = [x.format(d=duree) for x in MIRE]
    subprocess.run(  # noqa: S603 - argv fixe, pas de shell
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", *mire, *options, str(sortie)],
        check=True,
        timeout=120,
    )
    return sortie


@pytest.fixture(scope="module")
def medias(tmp_path_factory) -> dict[str, Path]:
    dossier = tmp_path_factory.mktemp("medias")
    sain = _fabriquer(
        dossier / "sain.mkv", 120, "-c:v", "libx264", "-preset", "ultrafast", "-g", "48",
        "-c:a", "aac",
    )  # fmt: skip
    donnees = sain.read_bytes()
    tronque = dossier / "tronque.mkv"
    tronque.write_bytes(donnees[: int(len(donnees) * 0.6)])
    vob = _fabriquer(
        dossier / "dvd.vob", 40, "-c:v", "mpeg2video", "-b:v", "1M", "-g", "15",
        "-c:a", "ac3", "-f", "vob",
    )  # fmt: skip
    ts = _fabriquer(
        dossier / "tnt.ts", 40, "-c:v", "libx264", "-preset", "veryfast", "-bf", "3",
        "-g", "250", "-c:a", "aac",
    )  # fmt: skip
    donnees = ts.read_bytes()
    ts_tronque = dossier / "tnt_tronque.ts"
    ts_tronque.write_bytes(donnees[: int(len(donnees) * 0.6)])
    return {"tronque": tronque, "vob": vob, "ts": ts, "ts_tronque": ts_tronque}


@reel
def test_sauter_vers_la_fin_d_un_fichier_tronque_dit_ou_il_s_arrete(tmp_path, medias) -> None:
    """Le geste « verifier la fin » : 90 % d'un fichier qui annonce 2:00 et
    s'arrete vers 1:12. La position vient des images reellement montrees, et
    l'ecran dit pourquoi elles ne sont pas a 1:48."""
    sessions = lecture.Sessions(tmp_path / "lecture", entretien_auto=False)
    analyse = lecture.sonder(medias["tronque"])
    decision = lecture.decider(analyse)
    at = analyse.duree * 0.9
    session = sessions.ouvrir(
        cle="plan:t", source=medias["tronque"], analyse=analyse, decision=decision, at=at,
        ffmpeg=FFMPEG,
    )  # fmt: skip
    try:
        sessions.liste(session.id, attente=60)
        session.processus.wait(timeout=60)
        etat = sessions.etat(session.id)
    finally:
        sessions.arreter_tout()

    assert decision.mode is Mode.REMUX
    assert etat["termine"] is True
    # Les images viennent d'avant 1:20, pas de 1:48.
    assert etat["debut"] < 80 and etat["premiere_image"] < 80
    assert etat["fin"] < 80
    assert etat["alerte"].startswith("Rien à 1:48 : le fichier s'arrête vers 1:")
    assert etat["alerte"].endswith("alors qu'il annonce 2:00.")


@reel
def test_un_vob_sain_n_est_pas_declare_abime(medias) -> None:
    """Chaque saut coupe le premier paquet AC-3 : le decodeur le refuse, et
    l'ancien controle declarait ce DVD sain abime aux cinq points."""
    brut = subprocess.run(  # noqa: S603 - argv fixe, pas de shell
        lecture.commande_controle(FFMPEG, medias["vob"], 20.0), capture_output=True, text=True
    )
    assert (
        "Error submitting packet to decoder" in brut.stderr or "Error while decoding" in brut.stderr
    )

    r = lecture.controler(medias["vob"], ffmpeg=FFMPEG)
    assert r.etat is EtatControle.SAIN, r.points


@reel
def test_un_mpeg_ts_a_images_cles_espacees_n_est_pas_declare_abime(medias) -> None:
    r = lecture.controler(medias["ts"], ffmpeg=FFMPEG)
    assert r.etat is EtatControle.SAIN, r.points


@reel
def test_un_mpeg_ts_tronque_reste_abime(medias) -> None:
    r = lecture.controler(medias["ts_tronque"], ffmpeg=FFMPEG)
    assert r.etat is EtatControle.ABIME
