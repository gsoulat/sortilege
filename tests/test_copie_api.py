"""Copie vers un disque externe : les routes, telles que l'interface les appelle.

Le contrat est partage avec l'interface, ecrite en parallele : les noms de
champs sont verifies ici un par un, parce qu'un champ renomme ne casse aucun
test cote serveur et laisse un ecran vide cote client.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import collection as index_api
from sortilege.api import copie as api
from sortilege.api import deps
from sortilege.config import get_settings
from sortilege.core import copie as moteur
from sortilege.core.collection import group
from sortilege.core.scanner import ScanRules, scan
from sortilege.main import app

BLOC = 1024
TOUT = ScanRules(min_size_bytes=0)
_PROGRESSION = moteur.Copie._progression
"""La vraie progression, pour la reposer apres un detournement."""

CLES_ETAT = {
    "running",
    "paused",
    "stopping",
    "disk",
    "current",
    "files_done",
    "files_total",
    "bytes_done",
    "bytes_total",
    "speed_bps",
    "eta_s",
    "queue_next",
    "done",
    "errors",
    "stopped_by_user",
    "aborted",
    "started_at",
    "finished_at",
    "resumable",
}
CLES_DISQUE = {
    "id",
    "path",
    "label",
    "mounted",
    "fs",
    "free_bytes",
    "total_bytes",
    "writable",
    "max_file_bytes",
    "refusal",
}


@dataclass
class Banc:
    media: Path
    externes: Path
    usb: Path
    montes: set[str]

    def poser(self, racine: Path, relatif: str, octets: int = 8 * BLOC) -> Path:
        chemin = racine / relatif
        chemin.parent.mkdir(parents=True, exist_ok=True)
        chemin.write_bytes(bytes((i * 13 + len(relatif)) % 256 for i in range(octets)))
        return chemin


@pytest.fixture
def banc(tmp_path: Path, monkeypatch) -> Banc:
    conf = get_settings()
    media = tmp_path / "media"
    media.mkdir()
    externes = tmp_path / "externes"
    usb = externes / "USB1" / "usbshare"
    usb.mkdir(parents=True)
    montes = {os.path.normpath(str(usb))}
    monkeypatch.setattr(conf, "library_root", media)
    monkeypatch.setattr(conf, "external_root", externes)
    monkeypatch.setattr(os.path, "ismount", lambda p: os.path.normpath(str(p)) in montes)
    monkeypatch.setattr(moteur, "MOUNTINFO", tmp_path / "pas-de-table")
    # Sans vrai montage, mediatheque et « disque » partagent le systeme de
    # fichiers des tests : le refus « volume de la mediatheque » les
    # refuserait tous. Il est verifie a part, dans test_copie_disques.
    monkeypatch.setattr(moteur, "_volume_de_la_mediatheque", lambda _chemin: None)
    monkeypatch.setattr(moteur, "TAILLE_BLOC", BLOC)
    monkeypatch.setattr(deps, "scan_rules", lambda: TOUT)
    monkeypatch.setattr(api, "_copie", None)
    monkeypatch.setattr(api, "_demarrage", False)
    monkeypatch.setattr(api, "_disponibilite", None)
    (deps.DATA_DIR / moteur.FICHIER_REPRISE).unlink(missing_ok=True)
    yield Banc(media, externes, usb, montes)
    if api._copie is not None:
        api._copie.arreter()
        api._copie.attendre(10)
    (deps.DATA_DIR / moteur.FICHIER_REPRISE).unlink(missing_ok=True)


def indexer(banc: Banc, monkeypatch) -> None:
    """L'index de la mediatheque, comme l'aurait laisse « Relire la médiathèque »."""
    fichiers = scan([banc.media], deep=False, library_root=banc.media, rules=TOUT).files
    monkeypatch.setattr(index_api._job, "works", group(fichiers))
    monkeypatch.setattr(index_api._job, "built_at", 1.0)


@pytest.fixture
def client(banc) -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


def attendre_fin() -> None:
    assert api._copie is not None
    assert api._copie.attendre(20), "la copie ne s'est pas terminee"


def empreinte(racine: Path) -> dict[str, tuple[int, int, str]]:
    return {
        str(p.relative_to(racine)): (
            p.stat().st_size,
            p.stat().st_mtime_ns,
            hashlib.sha256(p.read_bytes()).hexdigest(),
        )
        for p in sorted(racine.rglob("*"))
        if p.is_file()
    }


# --- Disques ------------------------------------------------------------------


def test_la_route_est_protegee(banc) -> None:
    with TestClient(app) as anonyme:
        assert anonyme.get("/api/copy/disks").status_code == 401


def test_les_disques_selon_le_contrat(client, banc) -> None:
    (banc.externes / "USB2").mkdir()  # debranche : un dossier vide

    corps = client.get("/api/copy/disks").json()

    assert set(corps) == {"root", "root_exists", "disks", "hint"}
    assert corps["root"] == str(banc.externes)
    assert corps["root_exists"] is True
    assert corps["hint"] is None
    disques = {d["id"]: d for d in corps["disks"]}
    assert set(disques) == {"USB1/usbshare", "USB2"}
    for d in disques.values():
        assert set(d) == CLES_DISQUE
    assert disques["USB1/usbshare"]["refusal"] is None
    assert disques["USB2"]["refusal"] == moteur.REFUS_SANS_DISQUE
    assert disques["USB2"]["mounted"] is False


def test_sans_racine_ni_disque_une_aide_est_donnee(client, banc, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(get_settings(), "external_root", tmp_path / "inexistant")
    corps = client.get("/api/copy/disks").json()
    assert corps["root_exists"] is False
    assert "rslave" in corps["hint"]

    banc.montes.clear()
    monkeypatch.setattr(get_settings(), "external_root", banc.externes)
    corps = client.get("/api/copy/disks").json()
    assert corps["root_exists"] is True
    assert "Aucun disque" in corps["hint"]


@pytest.mark.parametrize("ident", ["../media", "/etc", "USB1/../../media", "USB9", "USB1"])
def test_un_disque_inconnu_ou_hors_racine_est_404(client, banc, monkeypatch, ident) -> None:
    indexer(banc, monkeypatch)
    for route in ("/api/copy/analyze", "/api/copy/start"):
        reponse = client.post(route, json={"disk": ident, "works": []})
        assert reponse.status_code == 404, (route, ident)


def test_un_disque_refuse_est_409(client, banc, monkeypatch) -> None:
    indexer(banc, monkeypatch)
    (banc.externes / "USB2").mkdir()
    for route in ("/api/copy/analyze", "/api/copy/start"):
        reponse = client.post(route, json={"disk": "USB2", "works": []})
        assert reponse.status_code == 409
        assert "débranché" in reponse.json()["detail"]


# --- Mediatheque et analyse ---------------------------------------------------


def test_la_mediatheque_non_lue_est_dite(client, banc, monkeypatch) -> None:
    monkeypatch.setattr(index_api._job, "works", [])
    monkeypatch.setattr(index_api._job, "built_at", 0.0)

    assert client.get("/api/copy/library").json() == {"built": False, "works": []}
    reponse = client.post("/api/copy/analyze", json={"disk": "USB1/usbshare", "works": ["x"]})
    assert reponse.status_code == 409


def test_la_mediatheque_selon_le_contrat(client, banc, monkeypatch) -> None:
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv", 3000)
    indexer(banc, monkeypatch)

    corps = client.get("/api/copy/library").json()

    assert corps["built"] is True
    assert corps["works"] == [
        {
            "key": "movie:dune",
            "title": "Dune",
            "year": 2021,
            "kind": "movie",
            "files": 1,
            "bytes": 3000,
            "poster_url": "",
        }
    ]


def test_une_oeuvre_inconnue_est_404(client, banc, monkeypatch) -> None:
    indexer(banc, monkeypatch)
    reponse = client.post(
        "/api/copy/analyze", json={"disk": "USB1/usbshare", "works": ["movie:inexistant"]}
    )
    assert reponse.status_code == 404


def test_l_analyse_du_banc_severance(client, banc, monkeypatch) -> None:
    for n in (1, 2, 3):
        banc.poser(banc.media, f"SerieTV/Severance (2022)/Season 01/Severance - S01E0{n}.mkv")
    banc.poser(banc.usb, "Series/Severance/Saison 1/Severance.S01E01.1080p.mkv", 500)
    indexer(banc, monkeypatch)

    corps = client.post(
        "/api/copy/analyze",
        json={"disk": "USB1/usbshare", "works": ["episode:severance"], "sidecars": True},
    ).json()

    assert set(corps) == {
        "disk",
        "works",
        "totals",
        "fits",
        "missing_bytes",
        "margin_bytes",
        "scan_errors",
    }
    assert set(corps["disk"]) == CLES_DISQUE
    oeuvre = corps["works"][0]
    assert {
        "key",
        "title",
        "year",
        "kind",
        "state",
        "files_total",
        "files_present",
        "files_to_copy",
        "bytes_to_copy",
        "target_dir",
        "existing_dir",
        "issues",
    } <= set(oeuvre)
    assert oeuvre["state"] == "partial"
    assert oeuvre["files_present"] == 1
    assert oeuvre["files_to_copy"] == 2
    assert oeuvre["target_dir"] == oeuvre["existing_dir"] == "Series/Severance"
    assert corps["totals"] == {
        "files_to_copy": 2,
        "bytes_to_copy": 2 * 8 * BLOC,
        "files_present": 1,
    }
    assert corps["fits"] is True


# --- Copie --------------------------------------------------------------------


def test_une_copie_complete_selon_le_contrat(client, banc, monkeypatch) -> None:
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv")
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).fr.srt", 300)
    indexer(banc, monkeypatch)
    avant = empreinte(banc.media)
    en_cours: list[dict] = []

    def progression(self, octets):
        _PROGRESSION(self, octets)
        if not en_cours:
            en_cours.append(self.etat()["current"])

    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    reponse = client.post(
        "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:dune"]}
    )
    assert reponse.status_code == 200
    assert reponse.json()["started"] is True
    attendre_fin()

    etat = client.get("/api/copy/status").json()
    assert set(etat) == CLES_ETAT
    assert etat["running"] is False
    assert etat["files_done"] == etat["files_total"] == 2
    assert etat["bytes_done"] == etat["bytes_total"] == 8 * BLOC + 300
    assert etat["errors"] == []
    assert etat["resumable"] is None
    # Plus aucun fichier en cours : null, et non un objet vide que l'ecran
    # affichait « 0 Mo sur 0 Mo ».
    assert etat["current"] is None
    assert set(en_cours[0]) == {
        "source",
        "target",
        "bytes_done",
        "bytes_total",
        "speed_bps",
        "started_at",
        "resumed_from_bytes",
        "resume_note",
    }
    assert {f["target"] for f in etat["done"]} == {
        "Films/Dune (2021)/Dune (2021).mkv",
        "Films/Dune (2021)/Dune (2021).fr.srt",
    }
    for fait in etat["done"]:
        assert set(fait) == {"target", "bytes", "speed_bps", "duration_s"}
    assert etat["started_at"] and etat["finished_at"]
    for nom in ("Dune (2021).mkv", "Dune (2021).fr.srt"):
        relatif = f"Films/Dune (2021)/{nom}"
        assert (banc.usb / relatif).read_bytes() == (banc.media / relatif).read_bytes()
    assert empreinte(banc.media) == avant


def test_le_demarrage_refait_l_analyse(client, banc, monkeypatch) -> None:
    """Ce qui est arrive sur le disque depuis l'analyse n'est pas recopie."""
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv")
    indexer(banc, monkeypatch)
    client.post("/api/copy/analyze", json={"disk": "USB1/usbshare", "works": ["movie:dune"]})
    shutil.copytree(banc.media / "Films", banc.usb / "Films")

    reponse = client.post(
        "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:dune"]}
    )

    assert reponse.status_code == 409
    assert "Rien à copier" in reponse.json()["detail"]


def test_une_seule_copie_a_la_fois(client, banc, monkeypatch) -> None:
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv")
    banc.poser(banc.media, "Films/Alien (1979)/Alien (1979).mkv")
    indexer(banc, monkeypatch)
    retenue = threading.Event()
    lachee = threading.Event()
    vrai = moteur.Copie._point_de_controle

    def controle(self):
        retenue.set()
        lachee.wait(10)
        vrai(self)

    monkeypatch.setattr(moteur.Copie, "_point_de_controle", controle)
    corps = {"disk": "USB1/usbshare", "works": ["movie:dune"]}
    assert client.post("/api/copy/start", json=corps).status_code == 200
    assert retenue.wait(10)

    second = client.post(
        "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:alien"]}
    )
    assert second.status_code == 409
    assert "déjà en cours" in second.json()["detail"]
    assert client.get("/api/copy/status").json()["running"] is True
    assert client.post("/api/copy/continue").status_code == 409

    lachee.set()
    attendre_fin()


def test_une_copie_qui_ne_tient_pas_est_refusee_avec_le_manque(client, banc, monkeypatch) -> None:
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv")
    indexer(banc, monkeypatch)
    usage = shutil.disk_usage(banc.usb)
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type(usage)(10**9, 10**9 - 1000, 1000))

    reponse = client.post(
        "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:dune"]}
    )

    assert reponse.status_code == 409
    assert "il manque" in reponse.json()["detail"]
    assert api._copie is None
    assert list(banc.usb.iterdir()) == []


def test_un_disque_en_lecture_seule_est_refuse(client, banc, monkeypatch) -> None:
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv")
    indexer(banc, monkeypatch)
    monkeypatch.setattr(os, "access", lambda chemin, mode: False)

    reponse = client.post(
        "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:dune"]}
    )

    assert reponse.status_code == 409
    assert "écriture" in reponse.json()["detail"]


# --- Arret, reprise, abandon --------------------------------------------------


def arreter_au_bloc(monkeypatch, blocs: int) -> None:
    vraie = moteur.Copie._progression

    def progression(self, octets):
        vraie(self, octets)
        if self._octets_fichier == blocs * BLOC:
            self.arreter()

    monkeypatch.setattr(moteur.Copie, "_progression", progression)


def test_arreter_puis_continuer(client, banc, monkeypatch) -> None:
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    banc.poser(banc.media, nom, 8 * BLOC)
    indexer(banc, monkeypatch)
    arreter_au_bloc(monkeypatch, 3)
    corps = {"disk": "USB1/usbshare", "works": ["movie:dune"], "sidecars": False}
    assert client.post("/api/copy/start", json=corps).status_code == 200
    attendre_fin()

    etat = client.get("/api/copy/status").json()
    assert etat["stopped_by_user"] is True
    reprise = etat["resumable"]
    assert reprise == {
        "disk": "USB1/usbshare",
        "saved_at": reprise["saved_at"],
        "works": ["movie:dune"],
        "files_remaining": 1,
        "bytes_remaining": 5 * BLOC,
        "current": {"target": nom, "checkpoint_bytes": 3 * BLOC, "bytes_total": 8 * BLOC},
        "disk_available": True,
        "disk_found": "USB1/usbshare",
    }
    partiel = banc.usb / f"{nom}{moteur.SUFFIXE_PARTIEL}"
    assert partiel.stat().st_size == 3 * BLOC

    monkeypatch.setattr(moteur.Copie, "_progression", _PROGRESSION)
    # L'index n'a pas survecu (redemarrage) : la reprise n'en depend pas.
    monkeypatch.setattr(index_api._job, "works", [])
    monkeypatch.setattr(index_api._job, "built_at", 0.0)
    reponse = client.post("/api/copy/continue")
    assert reponse.status_code == 200, reponse.text
    attendre_fin()

    etat = client.get("/api/copy/status").json()
    assert etat["errors"] == []
    assert (banc.usb / nom).read_bytes() == (banc.media / nom).read_bytes()
    assert not partiel.exists()
    assert api._copie._octets_ecrits == 5 * BLOC
    assert etat["resumable"] is None


def test_continuer_vers_un_disque_absent_est_409(client, banc, monkeypatch) -> None:
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv", 8 * BLOC)
    indexer(banc, monkeypatch)
    arreter_au_bloc(monkeypatch, 3)
    corps = {"disk": "USB1/usbshare", "works": ["movie:dune"], "sidecars": False}
    assert client.post("/api/copy/start", json=corps).status_code == 200
    attendre_fin()

    banc.montes.clear()  # debranche
    monkeypatch.setattr(api, "_disponibilite", None)

    reponse = client.post("/api/copy/continue")
    assert reponse.status_code == 409
    assert "pas branché" in reponse.json()["detail"]
    assert client.get("/api/copy/status").json()["resumable"]["disk_available"] is False


def test_rien_a_continuer_est_409(client, banc) -> None:
    assert client.post("/api/copy/continue").status_code == 409
    assert client.get("/api/copy/status").json()["resumable"] is None


def test_abandonner_ne_supprime_que_le_partiel_designe(client, banc, monkeypatch) -> None:
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    banc.poser(banc.media, nom, 8 * BLOC)
    indexer(banc, monkeypatch)
    arreter_au_bloc(monkeypatch, 3)
    corps = {"disk": "USB1/usbshare", "works": ["movie:dune"], "sidecars": False}
    assert client.post("/api/copy/start", json=corps).status_code == 200
    attendre_fin()
    etranger = banc.poser(banc.usb, f"Films/Autre.mkv{moteur.SUFFIXE_PARTIEL}", 10)
    ordinaire = banc.poser(banc.usb, "Films/Alien.mkv", 10)

    corps = client.post("/api/copy/discard").json()

    assert corps["removed"] == [f"{nom}{moteur.SUFFIXE_PARTIEL}"]
    assert corps["absent"] == []
    assert corps["not_removed"] == []
    assert corps["resumable"] is None
    assert not (banc.usb / f"{nom}{moteur.SUFFIXE_PARTIEL}").exists()
    assert etranger.exists()
    assert ordinaire.exists()
    assert not (deps.DATA_DIR / moteur.FICHIER_REPRISE).exists()


def test_pause_reprise_et_arret_sans_copie_rendent_l_etat(client, banc) -> None:
    for route in ("pause", "resume", "stop"):
        corps = client.post(f"/api/copy/{route}").json()
        assert set(corps) == CLES_ETAT
        assert corps["running"] is False
        assert corps["disk"] is None
        assert corps["current"] is None


# --- Ce qu'une copie interrompue interdit -------------------------------------


def interrompre(client, banc: Banc, monkeypatch, disque: str = "USB1/usbshare") -> str:
    """Une copie arretee au troisieme bloc : un ``.part`` et un etat a reprendre."""
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    banc.poser(banc.media, nom, 8 * BLOC)
    banc.poser(banc.media, "Films/Alien (1979)/Alien (1979).mkv", 4 * BLOC)
    indexer(banc, monkeypatch)
    arreter_au_bloc(monkeypatch, 3)
    corps = {"disk": disque, "works": ["movie:dune"], "sidecars": False}
    assert client.post("/api/copy/start", json=corps).status_code == 200
    attendre_fin()
    monkeypatch.setattr(moteur.Copie, "_progression", _PROGRESSION)
    return f"{nom}{moteur.SUFFIXE_PARTIEL}"


def test_une_copie_interrompue_bloque_toute_nouvelle_copie(client, banc, monkeypatch) -> None:
    """Une nouvelle copie ecrasait l'etat en silence : le ``.part`` devenait un
    orphelin que plus rien ne designait (ou etait supprime, sur le meme disque)."""
    partiel = interrompre(client, banc, monkeypatch)
    avant = client.get("/api/copy/status").json()["resumable"]

    reponse = client.post(
        "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:alien"]}
    )

    assert reponse.status_code == 409
    assert "copie interrompue attend" in reponse.json()["detail"]
    assert (banc.usb / partiel).stat().st_size == 3 * BLOC
    assert client.get("/api/copy/status").json()["resumable"] == avant


def test_abandonner_disque_debranche_est_refuse(client, banc, monkeypatch) -> None:
    """Le serveur gardait le ``.part`` mais effacait l'etat — un orphelin — et
    l'ecran annoncait « supprimé ». On refuse ; « forget » reste l'issue d'un
    disque qui ne reviendra pas, et la reponse dit ce qui reste sur le disque."""
    partiel = interrompre(client, banc, monkeypatch)
    banc.montes.clear()  # debranche

    reponse = client.post("/api/copy/discard", json={})

    assert reponse.status_code == 409
    assert "rebranche le disque" in reponse.json()["detail"]
    assert (deps.DATA_DIR / moteur.FICHIER_REPRISE).exists()
    assert client.get("/api/copy/status").json()["resumable"]["disk_available"] is False

    corps = client.post("/api/copy/discard", json={"forget": True}).json()

    assert corps["removed"] == []
    assert corps["not_removed"] == [partiel]
    assert corps["resumable"] is None
    assert (banc.usb / partiel).exists(), "oublie, pas supprime : la reponse le dit"


def test_l_extinction_arrete_la_copie_a_un_point_de_controle(banc, monkeypatch) -> None:
    """Le fil de copie est un demon : l'arret du conteneur le tuait en plein
    bloc. Le ``lifespan`` l'arrete proprement — dernier point de controle,
    etat a reprendre."""
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    banc.poser(banc.media, nom, 400 * BLOC)
    indexer(banc, monkeypatch)
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        corps = {"disk": "USB1/usbshare", "works": ["movie:dune"], "max_mb_per_s": 0.05}
        assert c.post("/api/copy/start", json=corps).status_code == 200
        fin = time.monotonic() + 10
        while (c.get("/api/copy/status").json()["current"] or {}).get("bytes_done", 0) < 20 * BLOC:
            assert time.monotonic() < fin, "la copie n'avance pas"
            time.sleep(0.02)
    # Sortir du contexte, c'est eteindre l'application.

    assert api._copie is not None and not api._copie.en_cours
    sauve = moteur.lire_reprise(deps.DATA_DIR / moteur.FICHIER_REPRISE)
    assert sauve is not None and sauve["current"]["checkpoint"] >= 20 * BLOC
    partiel = banc.usb / f"{nom}{moteur.SUFFIXE_PARTIEL}"
    assert partiel.stat().st_size == sauve["current"]["checkpoint"]


# --- Montage direct : refuse ----------------------------------------------------


def test_un_montage_direct_est_refuse_a_l_analyse_comme_au_depart(
    client, banc, monkeypatch
) -> None:
    """Le contre-controle : disque monte directement, analyse, disque arrache,
    un tiers remplit le dossier resté a sa place — l'ecran reanalysait apres
    le refus, obtenait le repere de la partition systeme, et le film y etait
    ecrit. Un montage direct n'est plus jamais une destination."""
    direct = banc.externes / "USB2"
    direct.mkdir()
    (direct / "LISEZMOI.txt").write_text("disque")
    banc.montes.add(os.path.normpath(str(direct)))
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv")
    indexer(banc, monkeypatch)
    corps = {"disk": "USB2", "works": ["movie:dune"]}

    for route in ("/api/copy/analyze", "/api/copy/start"):
        reponse = client.post(route, json=corps)
        assert reponse.status_code == 409, route
        assert "montage direct" in reponse.json()["detail"]
    disques = {d["id"]: d for d in client.get("/api/copy/disks").json()["disks"]}
    assert disques["USB2"]["refusal"] == moteur.REFUS_MONTAGE_DIRECT
    assert sorted(p.name for p in direct.iterdir()) == ["LISEZMOI.txt"]
    assert api._copie is None


# --- L'identite du disque : son fichier-marqueur --------------------------------


def lire_marqueur(dossier: Path) -> str:
    return (dossier / moteur.MARQUEUR).read_text().strip()


def test_le_depart_pose_le_marqueur_et_l_etat_l_enregistre(client, banc, monkeypatch) -> None:
    partiel = interrompre(client, banc, monkeypatch)

    marque = lire_marqueur(banc.usb)
    assert moteur.marque_valide(marque)
    sauve = moteur.lire_reprise(deps.DATA_DIR / moteur.FICHIER_REPRISE)
    assert sauve["disk_mark"] == marque
    assert (banc.usb / partiel).exists()

    # Une seconde copie vers le meme disque garde le meme identifiant.
    assert client.post("/api/copy/continue").status_code == 200
    attendre_fin()
    assert (
        client.post(
            "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:alien"]}
        ).status_code
        == 200
    )
    attendre_fin()
    assert lire_marqueur(banc.usb) == marque


def test_un_disque_qui_refuse_son_marqueur_ne_recoit_rien(client, banc, monkeypatch) -> None:
    banc.poser(banc.media, "Films/Dune (2021)/Dune (2021).mkv")
    indexer(banc, monkeypatch)

    def refuse(racine, disque):
        raise OSError(errno.EROFS, os.strerror(errno.EROFS))

    monkeypatch.setattr(moteur, "_poser_marque", refuse)

    reponse = client.post(
        "/api/copy/start", json={"disk": "USB1/usbshare", "works": ["movie:dune"]}
    )

    assert reponse.status_code == 409
    assert moteur.MARQUEUR in reponse.json()["detail"]
    assert "lecture seule" in reponse.json()["detail"]
    assert list(banc.usb.iterdir()) == []
    assert api._copie is None


def debrancher_puis_ailleurs(banc: Banc, nouveau: str) -> Path:
    """Le disque de la copie est debranche puis rebranche sous un autre nom ; le
    dossier de l'ancien emplacement devient celui d'un AUTRE disque, monte, vide."""
    ailleurs = banc.externes / nouveau
    ailleurs.parent.mkdir(parents=True, exist_ok=True)
    banc.usb.rename(ailleurs)
    banc.montes.add(os.path.normpath(str(ailleurs)))
    banc.usb.mkdir()
    return ailleurs


def test_la_reprise_retrouve_le_disque_rebranche_sur_un_autre_port(
    client, banc, monkeypatch
) -> None:
    """Rebranche ailleurs, le meme disque change de nom de montage et de
    numero de peripherique : le repere « st_dev:inode » le refusait. Le
    marqueur le retrouve, et l'autre disque arrive a sa place ne recoit rien."""
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    interrompre(client, banc, monkeypatch)
    ailleurs = debrancher_puis_ailleurs(banc, "USB2/usbshare")
    monkeypatch.setattr(api, "_disponibilite", None)

    reprise = client.get("/api/copy/status").json()["resumable"]
    assert reprise["disk"] == "USB1/usbshare"
    assert reprise["disk_available"] is True
    assert reprise["disk_found"] == "USB2/usbshare"

    reponse = client.post("/api/copy/continue")
    assert reponse.status_code == 200, reponse.text
    attendre_fin()

    assert (ailleurs / nom).read_bytes() == (banc.media / nom).read_bytes()
    assert api._copie._octets_ecrits == 5 * BLOC, "repris a son point de controle"
    assert list(banc.usb.iterdir()) == [], "l'autre disque n'a rien recu"


def test_la_reprise_refuse_un_autre_disque_au_meme_endroit(client, banc, monkeypatch) -> None:
    """Le disque de la copie est parti ; un autre occupe son emplacement. Rien
    ne doit y etre ecrit, et aucune reanalyse ne doit en faire la cible."""
    partiel = interrompre(client, banc, monkeypatch)
    parti = banc.usb.with_name("usbshare-parti")
    banc.usb.rename(parti)  # debranche, avec son marqueur et son .part
    banc.usb.mkdir()  # un autre disque, monte au meme endroit
    (banc.usb / "LISEZMOI.txt").write_text("un autre disque")
    monkeypatch.setattr(api, "_disponibilite", None)

    assert client.get("/api/copy/status").json()["resumable"]["disk_available"] is False
    reponse = client.post("/api/copy/continue")
    assert reponse.status_code == 409
    assert "pas branché" in reponse.json()["detail"]
    assert moteur.MARQUEUR in reponse.json()["detail"]
    abandon = client.post("/api/copy/discard", json={})
    assert abandon.status_code == 409
    assert "rebranche le disque" in abandon.json()["detail"]

    assert sorted(p.name for p in banc.usb.iterdir()) == ["LISEZMOI.txt"]
    assert (parti / partiel).exists()
    assert (deps.DATA_DIR / moteur.FICHIER_REPRISE).exists()


# --- Abandonner, oublier ---------------------------------------------------------


def test_oublier_est_refuse_quand_le_disque_est_branche(client, banc, monkeypatch) -> None:
    """« Oublier » promet de laisser le fichier partiel sur un disque absent. Le
    disque est la : le serveur le dit et propose de reprendre, sans rien faire."""
    partiel = interrompre(client, banc, monkeypatch)

    reponse = client.post("/api/copy/discard", json={"forget": True})

    assert reponse.status_code == 409
    assert "est branché" in reponse.json()["detail"]
    assert "Reprends" in reponse.json()["detail"]
    assert (banc.usb / partiel).exists()
    reprise = client.get("/api/copy/status").json()["resumable"]
    assert reprise is not None and reprise["disk_available"] is True


def test_abandonner_garde_l_etat_si_le_partiel_ne_peut_pas_etre_retire(
    client, banc, monkeypatch
) -> None:
    """Disque present, mais repasse en lecture seule : l'etat etait efface
    quand meme, et un fichier de plusieurs Go restait sans que rien ne le
    designe."""
    partiel = interrompre(client, banc, monkeypatch)
    vrai = os.unlink

    def lecture_seule(chemin, *args, **kwargs):
        if str(chemin).endswith(moteur.SUFFIXE_PARTIEL):
            raise OSError(errno.EROFS, os.strerror(errno.EROFS))
        return vrai(chemin, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", lecture_seule)

    reponse = client.post("/api/copy/discard", json={})

    assert reponse.status_code == 409
    assert "lecture seule" in reponse.json()["detail"]
    assert "gardée" in reponse.json()["detail"]
    assert (banc.usb / partiel).exists()
    assert (deps.DATA_DIR / moteur.FICHIER_REPRISE).exists()
    assert client.get("/api/copy/status").json()["resumable"] is not None


def test_rien_a_reprendre_garde_l_etat_si_le_partiel_reste(client, banc, monkeypatch) -> None:
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    partiel = interrompre(client, banc, monkeypatch)
    shutil.copyfile(banc.media / nom, banc.usb / nom)  # termine ailleurs entre-temps
    vrai = os.unlink

    def refuse(chemin, *args, **kwargs):
        if str(chemin).endswith(moteur.SUFFIXE_PARTIEL):
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))
        return vrai(chemin, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)

    reponse = client.post("/api/copy/continue")

    assert reponse.status_code == 409
    assert "n'a pas pu être retiré" in reponse.json()["detail"]
    assert (banc.usb / partiel).exists()
    assert (deps.DATA_DIR / moteur.FICHIER_REPRISE).exists()


def test_un_disque_muet_ne_supprime_jamais_apres_la_reponse(client, banc, monkeypatch) -> None:
    """Le delai depasse, la requete repondait « non retiré »... et son fil,
    toujours la, finissait par supprimer. Ce qui n'a pas commence ne commence
    plus."""
    partiel = interrompre(client, banc, monkeypatch)
    monkeypatch.setattr(moteur, "DELAI_VOLUME", 0.2)
    lache = threading.Event()
    vrai = os.stat

    def fige(chemin, *args, **kwargs):
        if kwargs.get("dir_fd") is not None and str(chemin).endswith(moteur.SUFFIXE_PARTIEL):
            lache.wait(10)
        return vrai(chemin, *args, **kwargs)

    monkeypatch.setattr(os, "stat", fige)
    try:
        reponse = client.post("/api/copy/discard", json={})
    finally:
        lache.set()

    assert reponse.status_code == 409
    assert "n'a pas répondu" in reponse.json()["detail"]
    time.sleep(0.5)  # le fil, libere, a eu tout le temps de supprimer
    assert (banc.usb / partiel).exists()
    assert (deps.DATA_DIR / moteur.FICHIER_REPRISE).exists()


def test_une_suppression_commencee_est_attendue(client, banc, monkeypatch) -> None:
    """L'inverse : commencee avant le delai, la suppression est attendue, et la
    reponse dit ce qu'elle a fait."""
    partiel = interrompre(client, banc, monkeypatch)
    monkeypatch.setattr(moteur, "DELAI_VOLUME", 0.2)
    vrai = os.unlink

    def lent(chemin, *args, **kwargs):
        if str(chemin).endswith(moteur.SUFFIXE_PARTIEL):
            time.sleep(0.6)
        return vrai(chemin, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", lent)

    corps = client.post("/api/copy/discard", json={}).json()

    assert corps["removed"] == [partiel]
    assert not (banc.usb / partiel).exists()
    assert corps["resumable"] is None


def test_relire_les_disques_rafraichit_la_disponibilite(client, banc, monkeypatch) -> None:
    """« Relire les disques » apres avoir rebranche : l'etat disait encore
    « pas branché » pendant cinq secondes, bouton « Reprendre » grise."""
    interrompre(client, banc, monkeypatch)
    montes = set(banc.montes)
    banc.montes.clear()  # debranche
    monkeypatch.setattr(api, "_disponibilite", None)
    assert client.get("/api/copy/status").json()["resumable"]["disk_available"] is False

    banc.montes.update(montes)  # rebranche
    assert client.get("/api/copy/status").json()["resumable"]["disk_available"] is False, (
        "sans relecture, le sondage garde sa derniere recherche quelques secondes"
    )
    client.get("/api/copy/disks")
    assert client.get("/api/copy/status").json()["resumable"]["disk_available"] is True
