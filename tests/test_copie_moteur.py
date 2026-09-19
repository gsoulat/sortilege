"""Copie vers un disque externe : le moteur, un fichier a la fois.

Ce qui est verrouille ici, c'est ce qui ne se voit pas en utilisant l'outil :
un seul fichier ouvert en ecriture a la fois (le port USB d'un NAS), jamais un
fichier tronque sous son nom definitif (le defaut deja corrige ailleurs : des
copies interrompues prises pour des films complets), un ``fsync`` avant la
publication, et un disque arrache qui arrete tout au lieu de laisser la file
continuer sur la partition systeme du NAS.

Et la reprise : seul ce qui precede un point de controle est repute ecrit,
et rien n'est repris sans avoir ete compare a la source.

Pas de vrai disque, pas de ``sleep`` pour mesurer : ``ismount`` est detourne,
l'horloge et ``fsync`` sont simules la ou ils comptent.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import threading
from pathlib import Path

import pytest

from sortilege.core import copie as moteur

BLOC = 1024
MARQUE = "0f0f0f0f-0000-4000-8000-000000000001"
"""Identifiant de disque d'un etat de reprise : sans lui, l'etat est ignore."""


@pytest.fixture(autouse=True)
def petits_blocs(monkeypatch) -> None:
    monkeypatch.setattr(moteur, "TAILLE_BLOC", BLOC)


@pytest.fixture
def montes(monkeypatch) -> set[str]:
    ensemble: set[str] = set()
    monkeypatch.setattr(os.path, "ismount", lambda p: os.path.normpath(str(p)) in ensemble)
    return ensemble


@pytest.fixture
def mediatheque(tmp_path: Path) -> Path:
    dossier = tmp_path / "media"
    dossier.mkdir()
    return dossier


@pytest.fixture
def disque(tmp_path: Path, montes: set[str]) -> moteur.Disque:
    chemin = tmp_path / "externes" / "USB1" / "usbshare"
    chemin.mkdir(parents=True)
    montes.add(os.path.normpath(str(chemin)))
    return moteur.Disque(
        id="USB1/usbshare",
        path=chemin,
        mounted=True,
        fs="ext4",
        free_bytes=10**12,
        total_bytes=10**12,
        writable=True,
        dev=chemin.stat().st_dev,
    )


@pytest.fixture
def etat_reprise(tmp_path: Path) -> Path:
    return tmp_path / "data" / moteur.FICHIER_REPRISE


def contenu(n: int, graine: int = 7) -> bytes:
    """Des octets qui ne se repetent pas par blocs : un decalage se verrait."""
    return bytes((i * graine + i // 251) % 256 for i in range(n))


def poser(racine: Path, relatif: str, donnees: bytes) -> Path:
    chemin = racine / relatif
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(donnees)
    return chemin


def tache(mediatheque: Path, disque: moteur.Disque, relatif: str, cible: str | None = None):
    cible = cible or relatif
    return moteur.Tache(
        source=mediatheque / relatif,
        cible=disque.path / cible,
        source_rel=relatif,
        cible_rel=cible,
        taille=(mediatheque / relatif).stat().st_size,
    )


def copier(disque, taches, **options) -> moteur.Copie:
    """Lance une copie et attend qu'elle finisse."""
    copie = moteur.Copie(disque, taches, **options)
    copie.lancer()
    assert copie.attendre(20), "la copie ne s'est pas terminee"
    return copie


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


# --- Le cas nominal, et ce qu'il garantit -------------------------------------


def test_la_file_copie_tout_a_l_identique(mediatheque, disque) -> None:
    noms = ["Films/A/A.mkv", "Films/B/B.mkv", "Series/C/S01/C.S01E01.mkv"]
    for i, nom in enumerate(noms):
        poser(mediatheque, nom, contenu(5 * BLOC + 17 * i, graine=3 + i))

    copie = copier(disque, [tache(mediatheque, disque, n) for n in noms])

    etat = copie.etat()
    assert etat["files_done"] == 3
    assert etat["errors"] == []
    assert etat["aborted"] is None
    for nom in noms:
        assert (disque.path / nom).read_bytes() == (mediatheque / nom).read_bytes()
    assert list(disque.path.rglob(f"*{moteur.SUFFIXE_PARTIEL}")) == []


def test_un_seul_fichier_ouvert_en_ecriture_a_la_fois(mediatheque, disque, monkeypatch) -> None:
    noms = [f"Films/F{i}/F{i}.mkv" for i in range(4)]
    for nom in noms:
        poser(mediatheque, nom, contenu(6 * BLOC))

    ouverts: set[int] = set()
    chemins_ecrits: list[str] = []
    maximum = [0]
    parts_simultanes = [0]
    vrai_open, vrai_close = os.open, os.close

    def espion_open(chemin, drapeaux, *args, **kwargs):
        fd = vrai_open(chemin, drapeaux, *args, **kwargs)
        # Toute ouverture en ecriture compte : sur le disque, elles se font par
        # descripteur de dossier (``dir_fd``), sous un nom nu.
        if drapeaux & (os.O_WRONLY | os.O_RDWR):
            ouverts.add(fd)
            chemins_ecrits.append(str(chemin))
            maximum[0] = max(maximum[0], len(ouverts))
        return fd

    def espion_close(fd):
        ouverts.discard(fd)
        return vrai_close(fd)

    vraie_progression = moteur.Copie._progression

    def progression(self, octets):
        vraie_progression(self, octets)
        partiels = list(disque.path.rglob(f"*{moteur.SUFFIXE_PARTIEL}"))
        parts_simultanes[0] = max(parts_simultanes[0], len(partiels))

    monkeypatch.setattr(os, "open", espion_open)
    monkeypatch.setattr(os, "close", espion_close)
    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    copier(disque, [tache(mediatheque, disque, n) for n in noms])

    assert maximum[0] == 1
    assert parts_simultanes[0] == 1
    assert len(set(chemins_ecrits)) == 4
    assert all(c.endswith(moteur.SUFFIXE_PARTIEL) for c in chemins_ecrits)


def test_le_fichier_n_apparait_sous_son_nom_qu_une_fois_complet_et_force(
    mediatheque, disque, monkeypatch
) -> None:
    """Jamais visible tronque ; ``fsync`` du ``.part`` AVANT sa publication."""
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(7 * BLOC + 3)
    poser(mediatheque, nom, donnees)
    cible = disque.path / nom
    partiel = cible.with_name(cible.name + moteur.SUFFIXE_PARTIEL)

    evenements: list[tuple[str, int, int]] = []
    vrai_fsync, vrai_rename = os.fsync, os.rename
    vus_pendant: list[tuple[bool, bool]] = []

    def espion_fsync(fd):
        infos = os.fstat(fd)
        evenements.append(("fsync", infos.st_ino, infos.st_size))
        return vrai_fsync(fd)

    def espion_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        infos = os.stat(src, dir_fd=src_dir_fd)
        evenements.append(("replace", infos.st_ino, infos.st_size))
        return vrai_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    vraie_progression = moteur.Copie._progression

    def progression(self, octets):
        vraie_progression(self, octets)
        vus_pendant.append((cible.exists(), partiel.exists()))

    monkeypatch.setattr(os, "fsync", espion_fsync)
    monkeypatch.setattr(os, "rename", espion_rename)
    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    copier(disque, [tache(mediatheque, disque, nom)])

    assert vus_pendant and all(v == (False, True) for v in vus_pendant)
    remplacements = [e for e in evenements if e[0] == "replace"]
    assert len(remplacements) == 1
    _, inode, taille = remplacements[0]
    assert taille == len(donnees)
    avant = evenements[: evenements.index(remplacements[0])]
    assert ("fsync", inode, len(donnees)) in avant
    assert cible.read_bytes() == donnees
    assert not partiel.exists()


def test_une_taille_qui_ne_correspond_pas_n_est_jamais_publiee(
    mediatheque, disque, monkeypatch
) -> None:
    """La source grossit pendant la copie : ce qui est ecrit n'est pas ce qui etait prevu."""
    nom = "Films/Dune/Dune.mkv"
    source = poser(mediatheque, nom, contenu(4 * BLOC))
    vrai_commencer = moteur.Copie._commencer

    def commencer(self, *args, **kwargs):
        vrai_commencer(self, *args, **kwargs)
        with source.open("ab") as ajout:
            ajout.write(b"encore")

    monkeypatch.setattr(moteur.Copie, "_commencer", commencer)

    copie = copier(disque, [tache(mediatheque, disque, nom)])

    assert not (disque.path / nom).exists()
    assert list(disque.path.rglob(f"*{moteur.SUFFIXE_PARTIEL}")) == []
    assert "taille incorrecte" in copie.etat()["errors"][0]["message"]


def test_une_destination_apparue_entre_temps_n_est_jamais_ecrasee(mediatheque, disque) -> None:
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(2 * BLOC))
    t = tache(mediatheque, disque, nom)
    arrivee = poser(disque.path, nom, b"deja la")

    copie = copier(disque, [t])

    assert arrivee.read_bytes() == b"deja la"
    assert "jamais écrasé" in copie.etat()["errors"][0]["message"]


def test_une_erreur_sur_un_fichier_n_arrete_pas_la_file(mediatheque, disque) -> None:
    noms = ["Films/A/A.mkv", "Films/B/B.mkv", "Films/C/C.mkv"]
    for nom in noms:
        poser(mediatheque, nom, contenu(2 * BLOC))
    taches = [tache(mediatheque, disque, n) for n in noms]
    (mediatheque / noms[1]).unlink()  # disparu entre l'analyse et la copie

    copie = copier(disque, taches)

    etat = copie.etat()
    assert etat["files_done"] == 2
    assert etat["aborted"] is None
    assert [e["source"] for e in etat["errors"]] == [noms[1]]
    assert (disque.path / noms[0]).exists()
    assert (disque.path / noms[2]).exists()


def test_un_disque_plein_est_note_et_la_file_continue(mediatheque, disque, monkeypatch) -> None:
    noms = ["Films/A/A.mkv", "Films/B/B.mkv"]
    for nom in noms:
        poser(mediatheque, nom, contenu(2 * BLOC))
    vrai_write = os.write
    appels = [0]

    def plein(fd, donnees):
        appels[0] += 1
        if appels[0] == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return vrai_write(fd, donnees)

    monkeypatch.setattr(os, "write", plein)

    copie = copier(disque, [tache(mediatheque, disque, n) for n in noms])

    etat = copie.etat()
    assert etat["errors"][0]["message"] == "disque plein"
    assert etat["files_done"] == 1
    assert not (disque.path / noms[0]).exists()
    assert list(disque.path.rglob(f"*{moteur.SUFFIXE_PARTIEL}")) == []


def test_un_disque_debranche_arrete_toute_la_file(mediatheque, disque, montes) -> None:
    """Sans cela, la suite irait remplir la partition systeme du NAS."""
    noms = ["Films/A/A.mkv", "Films/B/B.mkv", "Films/C/C.mkv"]
    for nom in noms:
        poser(mediatheque, nom, contenu(2 * BLOC))
    taches = [tache(mediatheque, disque, n) for n in noms]

    vraie = moteur.Copie._copier

    def copier_puis_debrancher(self, t):
        vraie(self, t)
        montes.clear()  # le point de montage redevient un dossier ordinaire

    copie = moteur.Copie(disque, taches)
    copie._copier = copier_puis_debrancher.__get__(copie)
    copie.lancer()
    assert copie.attendre(20)

    etat = copie.etat()
    assert etat["files_done"] == 1
    assert etat["aborted"] and "n'est plus monté" in etat["aborted"]
    assert not (disque.path / noms[1]).exists()
    assert not (disque.path / noms[2]).exists()


# --- Le disque, par descripteur -----------------------------------------------


def test_un_lien_sur_le_disque_n_est_jamais_suivi(mediatheque, disque) -> None:
    """Un lien « Films -> <mediatheque>/Series » pose sur le disque (un ext4
    venu d'un autre Linux) faisait creer « Dune (2021) » DANS la mediatheque
    avant que le controle du volume ne refuse le fichier."""
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    poser(mediatheque, nom, contenu(3 * BLOC))
    (mediatheque / "Series").mkdir()
    (disque.path / "Films").symlink_to(mediatheque / "Series", target_is_directory=True)
    avant = sorted(str(p.relative_to(mediatheque)) for p in mediatheque.rglob("*"))

    copie = copier(disque, [tache(mediatheque, disque, nom)])

    apres = sorted(str(p.relative_to(mediatheque)) for p in mediatheque.rglob("*"))
    assert apres == avant, "rien ne doit apparaitre dans la mediatheque"
    etat = copie.etat()
    assert moteur.REFUS_LIEN in etat["errors"][0]["message"]
    assert etat["aborted"] is None, "un lien refuse ce fichier, pas toute la file"


@pytest.mark.parametrize("appel", [1, 2])
def test_un_disque_remplace_apres_le_controle_ne_recoit_rien(
    mediatheque, disque, montes, monkeypatch, appel
) -> None:
    """Le disque part juste APRES le controle : son point de montage devient un
    dossier de la partition systeme. Rien ne doit y etre ecrit.

    Appel 1 : pendant l'ouverture de la racine — l'inode ne correspond plus,
    la file s'arrete avant la moindre ecriture. Appel 2 : entre le controle
    d'avant-fichier et la creation des dossiers — tout passe par la racine
    ouverte au depart, le dossier qui a pris sa place reste vide."""
    nom = "Films/Dune (2021)/Dune (2021).mkv"
    poser(mediatheque, nom, contenu(3 * BLOC))
    disque.ino = disque.path.stat().st_ino
    parti = disque.path.with_name("usbshare-parti")
    vrai = moteur.Copie._disque_perdu
    appels = [0]

    def controle_puis_arrachage(self):
        motif = vrai(self)
        appels[0] += 1
        if appels[0] == appel:
            disque.path.rename(parti)
            disque.path.mkdir()  # le dossier vide qui reste sur le NAS
        return motif

    monkeypatch.setattr(moteur.Copie, "_disque_perdu", controle_puis_arrachage)

    copie = copier(disque, [tache(mediatheque, disque, nom)])

    assert list(disque.path.iterdir()) == [], "le dossier resté à la place a reçu une écriture"
    if appel == 1:
        assert "n'est plus le volume détecté" in copie.etat()["aborted"]
        assert list(parti.iterdir()) == []
    else:
        assert (parti / nom).read_bytes() == (mediatheque / nom).read_bytes()


@pytest.mark.parametrize(
    "code", [errno.EROFS, errno.ENOENT, errno.EIO], ids=["EROFS", "ENOENT", "EIO"]
)
def test_un_disque_qui_decroche_arrete_la_file_et_garde_la_reprise(
    mediatheque, disque, monkeypatch, etat_reprise, code
) -> None:
    """Un exFAT qui repasse en lecture seule apres une erreur (le reglage par
    defaut sous Linux) echouait fichier par fichier, et chaque echec effacait
    le ``.part`` et son point de controle. Toute la file s'arrete, reprenable."""
    monkeypatch.setattr(moteur, "INTERVALLE_POINT_DE_CONTROLE", BLOC)
    noms = ["Films/A/A.mkv", "Films/B/B.mkv"]
    for nom in noms:
        poser(mediatheque, nom, contenu(6 * BLOC))
    vrai_write = os.write
    ecritures = [0]

    def decroche(fd, donnees):
        ecritures[0] += 1
        if ecritures[0] == 3:
            raise OSError(code, os.strerror(code))
        return vrai_write(fd, donnees)

    monkeypatch.setattr(os, "write", decroche)

    copie = copier(
        disque,
        [tache(mediatheque, disque, n) for n in noms],
        fichier_reprise=etat_reprise,
        instantane={"disk": disque.id, "disk_mark": MARQUE, "works": [], "sidecars": False},
    )

    etat = copie.etat()
    assert etat["aborted"], "toute la file doit s'arreter"
    assert etat["files_done"] == 0
    assert not (disque.path / noms[1]).exists(), "le fichier suivant n'est pas tente"
    partiel = disque.path / f"{noms[0]}{moteur.SUFFIXE_PARTIEL}"
    assert partiel.stat().st_size >= 2 * BLOC, "le fichier partiel est garde"
    sauve = moteur.lire_reprise(etat_reprise)
    assert sauve is not None and sauve["current"]["checkpoint"] == 2 * BLOC
    if code == errno.EROFS:
        assert "lecture seule" in etat["aborted"]


def test_en_root_ce_qui_est_cree_prend_l_identite_du_dossier_d_accueil(
    mediatheque, disque, monkeypatch
) -> None:
    """Par descripteur (``fchown``) : le dossier cree prend l'identite de son
    parent, le fichier celle de son dossier ; un dossier existant n'est pas
    touche."""
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(2 * BLOC))
    (disque.path / "Films").mkdir()
    changes: list[int] = []
    monkeypatch.setattr(moteur, "tourne_en_root", lambda: True)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: changes.append(os.fstat(fd).st_ino))

    copier(disque, [tache(mediatheque, disque, nom)])

    assert sorted(changes) == sorted(
        [(disque.path / "Films/Dune").stat().st_ino, (disque.path / nom).stat().st_ino]
    )


def test_current_est_nul_hors_d_un_fichier(mediatheque, disque, monkeypatch) -> None:
    """Entre deux fichiers, et apres la fin, aucun fichier n'est en cours : un
    objet vide s'affichait « 0 Mo sur 0 Mo » sous un nom vide."""
    noms = ["Films/A/A.mkv", "Films/B/B.mkv"]
    for nom in noms:
        poser(mediatheque, nom, contenu(2 * BLOC))
    vrai = moteur.Copie._disque_perdu
    entre: list[object] = []

    def controle(self):
        entre.append(self.etat()["current"])
        return vrai(self)

    monkeypatch.setattr(moteur.Copie, "_disque_perdu", controle)

    copie = copier(disque, [tache(mediatheque, disque, n) for n in noms])

    assert entre and all(c is None for c in entre)
    assert copie.etat()["current"] is None
    assert moteur.etat_au_repos()["current"] is None


def test_la_mediatheque_n_est_jamais_modifiee(mediatheque, disque) -> None:
    noms = ["Films/A/A.mkv", "Films/A/A.fr.srt", "Series/B/Season 01/B.S01E01.mkv"]
    for nom in noms:
        poser(mediatheque, nom, contenu(3 * BLOC))
    avant = empreinte(mediatheque)

    copier(disque, [tache(mediatheque, disque, n) for n in noms])

    assert empreinte(mediatheque) == avant


# --- Pause, arret, debit ------------------------------------------------------


def test_pause_puis_reprise(mediatheque, disque, monkeypatch) -> None:
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(8 * BLOC)
    poser(mediatheque, nom, donnees)
    en_pause = threading.Event()
    vraie_progression = moteur.Copie._progression

    def progression(self, octets):
        vraie_progression(self, octets)
        if self._octets_fichier == 2 * BLOC:
            self.pause()
            en_pause.set()

    monkeypatch.setattr(moteur.Copie, "_progression", progression)
    copie = moteur.Copie(disque, [tache(mediatheque, disque, nom)])
    copie.lancer()
    assert en_pause.wait(10)

    assert not copie.attendre(0.3)  # toujours la, immobile
    etat = copie.etat()
    assert etat["paused"] is True
    assert etat["running"] is True
    assert etat["current"]["bytes_done"] == 2 * BLOC
    assert etat["speed_bps"] == 0
    assert etat["eta_s"] is None

    copie.reprendre()
    assert copie.attendre(10)
    assert (disque.path / nom).read_bytes() == donnees
    assert copie.etat()["paused"] is False


def test_un_arret_garde_le_fichier_partiel_et_son_point_de_controle(
    mediatheque, disque, monkeypatch, etat_reprise
) -> None:
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(8 * BLOC))
    vraie_progression = moteur.Copie._progression

    def progression(self, octets):
        vraie_progression(self, octets)
        if self._octets_fichier == 3 * BLOC:
            self.arreter()

    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    copie = copier(
        disque,
        [tache(mediatheque, disque, nom)],
        fichier_reprise=etat_reprise,
        instantane={"disk": disque.id, "disk_mark": MARQUE, "works": [], "sidecars": True},
    )

    etat = copie.etat()
    assert etat["stopped_by_user"] is True
    assert etat["running"] is False
    cible = disque.path / nom
    partiel = cible.with_name(cible.name + moteur.SUFFIXE_PARTIEL)
    assert not cible.exists()
    assert partiel.stat().st_size == 3 * BLOC
    sauve = moteur.lire_reprise(etat_reprise)
    assert sauve is not None
    assert sauve["current"]["checkpoint"] == 3 * BLOC
    assert sauve["current"]["part"] == f"{nom}{moteur.SUFFIXE_PARTIEL}"
    assert sauve["files_remaining"] == 1
    assert sauve["bytes_remaining"] == 5 * BLOC


def test_le_debit_se_mesure_sur_une_horloge_simulee(mediatheque, disque, monkeypatch) -> None:
    """Deux blocs par seconde : 2048 o/s, pour le fichier comme pour la file."""
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(12 * BLOC))
    horloge = [100.0]
    releves: list[dict] = []
    vraie_progression = moteur.Copie._progression

    def progression(self, octets):
        horloge[0] += 0.5
        vraie_progression(self, octets)
        releves.append(self.etat())

    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    copie = copier(disque, [tache(mediatheque, disque, nom)], horloge=lambda: horloge[0])

    # Premier bloc : une demi-seconde de mesure. Aucun debit n'est annonce
    # sous une seconde — le premier bloc part dans le cache en quelques
    # millisecondes, et l'annoncer donnait « 600 Mo/s » sur un disque a 4.
    assert releves[0]["eta_s"] is None
    assert releves[0]["current"]["speed_bps"] == 0
    assert releves[0]["speed_bps"] == 0
    # Des la deuxieme mesure (1 s), le debit est la.
    assert releves[1]["current"]["speed_bps"] == 2 * BLOC
    # Au dixieme bloc (5 s), fenetre pleine : debit instantane et global stables.
    dixieme = releves[9]
    assert dixieme["current"]["speed_bps"] == 2 * BLOC
    assert dixieme["speed_bps"] == 2 * BLOC
    assert dixieme["eta_s"] == 1  # deux blocs restants a 2 blocs/s
    assert dixieme["current"]["bytes_done"] == 10 * BLOC
    assert [q["target"] for q in dixieme["queue_next"]] == []
    fait = copie.etat()["done"][0]
    assert fait["bytes"] == 12 * BLOC
    assert fait["duration_s"] == pytest.approx(6.0)
    assert fait["speed_bps"] == 2 * BLOC


def test_le_debit_instantane_oublie_un_demarrage_lent(mediatheque, disque, monkeypatch) -> None:
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(12 * BLOC))
    horloge = [0.0]
    releves: list[int] = []
    vraie_progression = moteur.Copie._progression

    def progression(self, octets):
        # Le premier bloc prend 10 s (disque qui sort de veille), les autres 0,5 s.
        horloge[0] += 10.0 if self._octets_fichier == 0 else 0.5
        vraie_progression(self, octets)
        releves.append(self.etat()["current"]["speed_bps"])

    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    copier(disque, [tache(mediatheque, disque, nom)], horloge=lambda: horloge[0])

    assert releves[-1] == 2 * BLOC


def test_la_limite_de_debit_est_respectee(mediatheque, disque) -> None:
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(4 * BLOC))
    horloge = [0.0]

    def dormir(secondes: float) -> None:
        horloge[0] += secondes

    copier(
        disque,
        [tache(mediatheque, disque, nom)],
        max_mb_per_s=BLOC / (1024 * 1024),  # un bloc par seconde
        horloge=lambda: horloge[0],
        dormir=dormir,
    )

    assert horloge[0] == pytest.approx(4.0)


def test_la_file_a_venir_est_une_liste_d_objets(mediatheque, disque, monkeypatch) -> None:
    noms = [f"Films/F{i:02d}/F{i:02d}.mkv" for i in range(25)]
    for nom in noms:
        poser(mediatheque, nom, contenu(BLOC))
    releves: list[dict] = []
    vraie_progression = moteur.Copie._progression

    def progression(self, octets):
        vraie_progression(self, octets)
        if not releves:
            releves.append(self.etat())

    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    copier(disque, [tache(mediatheque, disque, n) for n in noms])

    file = releves[0]["queue_next"]
    assert len(file) == 20
    assert file[0] == {"target": noms[1], "bytes": BLOC}


# --- Reprise d'un fichier interrompu ------------------------------------------


def preparer_reprise(mediatheque, disque, nom, donnees, partiel_donnees, point, **surcharge):
    """Une copie precedente interrompue : son ``.part`` et son etat enregistre."""
    source = poser(mediatheque, nom, donnees)
    cible = disque.path / nom
    partiel = poser(disque.path, f"{nom}{moteur.SUFFIXE_PARTIEL}", partiel_donnees)
    infos = source.stat()
    reprise = {
        "source": nom,
        "target": nom,
        "part": f"{nom}{moteur.SUFFIXE_PARTIEL}",
        "checkpoint": point,
        "source_size": infos.st_size,
        "source_mtime_ns": infos.st_mtime_ns,
        "source_ino": infos.st_ino,
        "source_ctime_ns": infos.st_ctime_ns,
    }
    reprise.update(surcharge)
    return cible, partiel, reprise


@pytest.fixture
def fsync_simule(monkeypatch) -> list[int]:
    appels: list[int] = []
    monkeypatch.setattr(os, "fsync", lambda fd: appels.append(fd))
    return appels


def reprendre(disque, mediatheque, nom, reprise) -> moteur.Copie:
    releves: list[dict] = []
    copie = moteur.Copie(disque, [tache(mediatheque, disque, nom)], reprise=reprise)
    vraie = copie._progression

    def progression(octets):
        vraie(octets)
        if not releves:
            releves.append(copie.etat())

    copie._progression = progression
    copie.lancer()
    assert copie.attendre(20)
    copie.releve_en_cours = releves[0] if releves else None
    return copie


def test_une_copie_reprend_a_son_point_de_controle(mediatheque, disque, fsync_simule) -> None:
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(10 * BLOC)
    cible, _, reprise = preparer_reprise(
        mediatheque, disque, nom, donnees, donnees[: 6 * BLOC], 6 * BLOC
    )

    copie = reprendre(disque, mediatheque, nom, reprise)

    assert cible.read_bytes() == donnees
    assert copie._octets_ecrits == 4 * BLOC  # seulement ce qui manquait
    assert copie.releve_en_cours["current"]["resumed_from_bytes"] == 6 * BLOC
    assert copie.releve_en_cours["current"]["resume_note"] is None


def test_un_partiel_plus_long_que_son_point_de_controle_est_tronque(
    mediatheque, disque, fsync_simule
) -> None:
    """Ce qui suit le point de controle n'est pas repute ecrit : cache perdu, peut-etre."""
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(10 * BLOC)
    cible, _, reprise = preparer_reprise(
        mediatheque, disque, nom, donnees, donnees[: 4 * BLOC] + b"\0" * (3 * BLOC), 4 * BLOC
    )

    copie = reprendre(disque, mediatheque, nom, reprise)

    assert cible.read_bytes() == donnees
    assert copie.releve_en_cours["current"]["resumed_from_bytes"] == 4 * BLOC


def test_des_octets_corrompus_avant_le_point_de_controle_font_repartir_de_zero(
    mediatheque, disque, fsync_simule
) -> None:
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(10 * BLOC)
    abime = bytearray(donnees[: 6 * BLOC])
    abime[3 * BLOC + 5] ^= 0xFF
    cible, _, reprise = preparer_reprise(mediatheque, disque, nom, donnees, bytes(abime), 6 * BLOC)

    copie = reprendre(disque, mediatheque, nom, reprise)

    assert cible.read_bytes() == donnees
    assert copie._octets_ecrits == 10 * BLOC
    courant = copie.releve_en_cours["current"]
    assert courant["resumed_from_bytes"] == 0
    assert "ne correspond pas" in courant["resume_note"]


@pytest.mark.parametrize(
    "champ", ["source_size", "source_mtime_ns", "source_ino", "source_ctime_ns"]
)
def test_une_source_modifiee_fait_repartir_de_zero(
    mediatheque, disque, fsync_simule, champ
) -> None:
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(10 * BLOC)
    cible, _, reprise = preparer_reprise(
        mediatheque, disque, nom, donnees, donnees[: 6 * BLOC], 6 * BLOC
    )
    reprise[champ] += 1

    copie = reprendre(disque, mediatheque, nom, reprise)

    assert cible.read_bytes() == donnees
    assert copie._octets_ecrits == 10 * BLOC
    assert "a changé" in copie.releve_en_cours["current"]["resume_note"]


def test_un_partiel_plus_court_que_son_point_de_controle_fait_repartir_de_zero(
    mediatheque, disque, fsync_simule
) -> None:
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(10 * BLOC)
    cible, _, reprise = preparer_reprise(
        mediatheque, disque, nom, donnees, donnees[: 2 * BLOC], 6 * BLOC
    )

    copie = reprendre(disque, mediatheque, nom, reprise)

    assert cible.read_bytes() == donnees
    assert copie._octets_ecrits == 10 * BLOC


def test_sans_etat_un_partiel_est_recopie_depuis_le_debut(
    mediatheque, disque, fsync_simule
) -> None:
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(10 * BLOC)
    cible, _, _ = preparer_reprise(mediatheque, disque, nom, donnees, donnees[: 6 * BLOC], 0)

    copie = reprendre(disque, mediatheque, nom, None)

    assert cible.read_bytes() == donnees
    assert copie._octets_ecrits == 10 * BLOC
    assert "sans point de contrôle" in copie.releve_en_cours["current"]["resume_note"]


def test_arreter_puis_reprendre_donne_le_fichier_octet_pour_octet(
    mediatheque, disque, monkeypatch, etat_reprise, fsync_simule
) -> None:
    """Le parcours complet : points de controle pendant la copie, arret, reprise."""
    monkeypatch.setattr(moteur, "INTERVALLE_POINT_DE_CONTROLE", 2 * BLOC)
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(9 * BLOC + 100)
    poser(mediatheque, nom, donnees)
    instantane = {"disk": disque.id, "disk_mark": MARQUE, "works": [], "sidecars": True}
    vraie_progression = moteur.Copie._progression
    points: list[int] = []
    journal: list[tuple[str, int]] = []
    vrai_ecrire = moteur.ecrire_reprise

    def fsync(fd):
        infos = os.fstat(fd)
        journal.append(("fsync", infos.st_size))
        fsync_simule.append(fd)

    def ecrire(fichier, etat):
        if etat.get("current"):
            points.append(etat["current"]["checkpoint"])
            journal.append(("point", etat["current"]["checkpoint"]))
        vrai_ecrire(fichier, etat)

    monkeypatch.setattr(os, "fsync", fsync)

    def progression(self, octets):
        vraie_progression(self, octets)
        if self._octets_fichier == 5 * BLOC:
            self.arreter()

    monkeypatch.setattr(moteur, "ecrire_reprise", ecrire)
    monkeypatch.setattr(moteur.Copie, "_progression", progression)
    copier(
        disque,
        [tache(mediatheque, disque, nom)],
        fichier_reprise=etat_reprise,
        instantane=instantane,
    )
    # Un point au depart, tous les deux blocs, et un dernier a l'arret.
    assert points == [0, 2 * BLOC, 4 * BLOC, 5 * BLOC]
    # Chaque point de controle au-dela de zero suit IMMEDIATEMENT un fsync du
    # ``.part`` a cette taille : seul ce qui est force compte comme ecrit.
    for i, (quoi, octets) in enumerate(journal):
        if quoi == "point" and octets > 0:
            assert journal[i - 1] == ("fsync", octets), journal[: i + 1]

    monkeypatch.setattr(moteur.Copie, "_progression", vraie_progression)
    sauve = moteur.lire_reprise(etat_reprise)
    copie = copier(
        disque,
        [tache(mediatheque, disque, nom)],
        fichier_reprise=etat_reprise,
        instantane=instantane,
        reprise=sauve["current"],
    )

    assert (disque.path / nom).read_bytes() == donnees
    assert copie._octets_ecrits == len(donnees) - 5 * BLOC
    # Une copie allee au bout n'a plus rien a reprendre.
    assert not etat_reprise.exists()


def test_un_octet_change_dans_le_dernier_segment_est_vu(
    mediatheque, disque, fsync_simule, monkeypatch
) -> None:
    """Le dernier segment avant le point de controle est relu EN ENTIER : c'est
    la que frappe une coupure. Un octet hors des fenetres echantillonnees,
    mais dans ce segment, fait repartir de zero."""
    monkeypatch.setattr(moteur, "INTERVALLE_POINT_DE_CONTROLE", 4 * BLOC)
    monkeypatch.setattr(moteur, "FENETRE_VERIFICATION", 64)
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(16 * BLOC)
    abime = bytearray(donnees[: 12 * BLOC])
    abime[10 * BLOC + 100] ^= 0xFF
    cible, _, reprise = preparer_reprise(mediatheque, disque, nom, donnees, bytes(abime), 12 * BLOC)

    copie = reprendre(disque, mediatheque, nom, reprise)

    assert cible.read_bytes() == donnees
    assert copie.releve_en_cours["current"]["resumed_from_bytes"] == 0
    assert "ne correspond pas" in copie.releve_en_cours["current"]["resume_note"]


def _meme_taille_meme_date(source: Path, remplacer: bool) -> bytes:
    """Une autre version du film, a la meme taille et a la meme date (« rsync -t »,
    « cp -p », « touch -r »), avec une difference qu'aucun echantillon ne voit."""
    infos = source.stat()
    nouveau = bytearray(source.read_bytes())
    nouveau[3 * BLOC + 500] ^= 0xFF
    if remplacer:
        autre = source.with_name(source.name + ".neuf")
        autre.write_bytes(bytes(nouveau))
        os.replace(autre, source)
    else:
        source.write_bytes(bytes(nouveau))
    # Le ctime avance au grain de l'horloge du noyau (quelques ms sous Linux) :
    # on attend qu'il ait bouge, ce qui arrive a toute vraie reecriture.
    for _ in range(200):
        os.utime(source, ns=(infos.st_atime_ns, infos.st_mtime_ns))
        if remplacer or source.stat().st_ctime_ns != infos.st_ctime_ns:
            break
        threading.Event().wait(0.01)
    assert source.stat().st_mtime_ns == infos.st_mtime_ns
    assert source.stat().st_size == infos.st_size
    return bytes(nouveau)


@pytest.mark.parametrize("remplacer", [False, True], ids=["reecrite", "remplacee"])
def test_une_source_changee_a_taille_et_date_identiques_repart_de_zero(
    mediatheque, disque, fsync_simule, monkeypatch, remplacer
) -> None:
    """Taille et ``mtime_ns`` identiques, contenu different la ou aucune fenetre
    ne regarde : le fichier publie etait un melange des deux versions.
    L'inode (remplacee) ou le ``ctime`` (reecrite sur place) le trahit."""
    monkeypatch.setattr(moteur, "INTERVALLE_POINT_DE_CONTROLE", 2 * BLOC)
    monkeypatch.setattr(moteur, "FENETRE_VERIFICATION", 64)
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(12 * BLOC)
    cible, _, reprise = preparer_reprise(
        mediatheque, disque, nom, donnees, donnees[: 8 * BLOC], 8 * BLOC
    )
    nouveau = _meme_taille_meme_date(mediatheque / nom, remplacer)

    copie = reprendre(disque, mediatheque, nom, reprise)

    assert cible.read_bytes() == nouveau
    assert copie.releve_en_cours["current"]["resumed_from_bytes"] == 0
    assert "a changé" in copie.releve_en_cours["current"]["resume_note"]


def test_le_fichier_a_reprendre_passe_en_tete_de_file(mediatheque, disque, fsync_simule) -> None:
    donnees = contenu(4 * BLOC)
    poser(mediatheque, "Films/A/A.mkv", contenu(BLOC))
    _, _, reprise = preparer_reprise(
        mediatheque, disque, "Films/B/B.mkv", donnees, donnees[: 2 * BLOC], 2 * BLOC
    )
    taches = [
        tache(mediatheque, disque, "Films/A/A.mkv"),
        tache(mediatheque, disque, "Films/B/B.mkv"),
    ]

    copie = copier(disque, taches, reprise=reprise)

    assert [f["target"] for f in copie.etat()["done"]] == ["Films/B/B.mkv", "Films/A/A.mkv"]
    assert (disque.path / "Films/B/B.mkv").read_bytes() == donnees


# --- L'etat de reprise --------------------------------------------------------


def test_l_etat_de_reprise_est_ecrit_atomiquement(tmp_path: Path, monkeypatch) -> None:
    fichier = tmp_path / moteur.FICHIER_REPRISE
    moteur.ecrire_reprise(fichier, {"n": 1})
    publications: list[tuple[str, str, object]] = []
    vrai_replace = os.replace

    def espion(src, dst):
        publications.append((Path(src).name, Path(dst).name, json.loads(Path(src).read_text())))
        return vrai_replace(src, dst)

    monkeypatch.setattr(os, "replace", espion)
    moteur.ecrire_reprise(fichier, {"n": 2})
    assert publications == [(f"{moteur.FICHIER_REPRISE}.tmp", moteur.FICHIER_REPRISE, {"n": 2})]

    # Une coupure pendant l'ecriture laisse l'etat precedent intact.
    def panne(fd):
        raise OSError(errno.EIO, "panne")

    monkeypatch.setattr(os, "fsync", panne)
    with pytest.raises(OSError):
        moteur.ecrire_reprise(fichier, {"n": 3})
    assert json.loads(fichier.read_text()) == {"n": 2}


def etat_valide(**surcharge) -> dict:
    etat = {
        "version": moteur.VERSION_REPRISE,
        "disk": "USB1/usbshare",
        "disk_mark": MARQUE,
        "works": [],
        "sidecars": True,
        "max_mb_per_s": None,
        "current": {
            "source": "Films/A.mkv",
            "target": "Films/A.mkv",
            "part": f"Films/A.mkv{moteur.SUFFIXE_PARTIEL}",
            "checkpoint": 0,
            "source_size": 10,
            "source_mtime_ns": 1,
            "source_ino": 2,
            "source_ctime_ns": 3,
        },
    }
    etat.update(surcharge)
    return etat


def test_un_etat_fabrique_ne_designe_jamais_un_fichier_ordinaire(tmp_path: Path) -> None:
    fichier = tmp_path / moteur.FICHIER_REPRISE
    for courant in (
        {"target": "Films/A.mkv", "part": "Films/A.mkv"},
        {"target": "../A.mkv", "part": f"../A.mkv{moteur.SUFFIXE_PARTIEL}"},
        {"target": "/etc/A.mkv", "part": f"/etc/A.mkv{moteur.SUFFIXE_PARTIEL}"},
        {"part": f"Films/B.mkv{moteur.SUFFIXE_PARTIEL}"},
    ):
        etat = etat_valide()
        etat["current"].update(courant)
        fichier.write_text(json.dumps(etat))
        assert moteur.lire_reprise(fichier) is None, courant

    fichier.write_text(json.dumps(etat_valide()))
    assert moteur.lire_reprise(fichier) is not None


def test_un_etat_dont_les_oeuvres_sortent_de_la_mediatheque_est_ignore(tmp_path: Path) -> None:
    """Les chemins de l'instantane redeviennent des sources et des destinations."""
    fichier = tmp_path / moteur.FICHIER_REPRISE
    for chemin in ("../../etc/passwd", "/etc/passwd", "Films/./A.mkv"):
        oeuvre = {"key": "movie:a", "kind": "movie", "title": "A", "year": None}
        oeuvre["slots"] = {"film": [[chemin, 10]]}
        fichier.write_text(json.dumps(etat_valide(works=[oeuvre])))
        assert moteur.lire_reprise(fichier) is None, chemin


def test_seul_le_partiel_designe_est_supprimable(disque, tmp_path: Path) -> None:
    designe = poser(disque.path, f"Films/A.mkv{moteur.SUFFIXE_PARTIEL}", b"a")
    autre_partiel = poser(disque.path, f"Films/B.mkv{moteur.SUFFIXE_PARTIEL}", b"b")
    ordinaire = poser(disque.path, "Films/C.mkv", b"c")
    ailleurs = poser(tmp_path / "hors-disque", f"D.mkv{moteur.SUFFIXE_PARTIEL}", b"d")
    (disque.path / "lien").symlink_to(ailleurs.parent, target_is_directory=True)

    assert not moteur.supprimer_partiel(disque, "Films/C.mkv")
    assert not moteur.supprimer_partiel(disque, f"lien/D.mkv{moteur.SUFFIXE_PARTIEL}")
    assert not moteur.supprimer_partiel(disque, f"../hors-disque/D.mkv{moteur.SUFFIXE_PARTIEL}")
    assert moteur.supprimer_partiel(disque, f"Films/A.mkv{moteur.SUFFIXE_PARTIEL}")

    assert not designe.exists()
    assert autre_partiel.exists()
    assert ordinaire.exists()
    assert ailleurs.exists()


def test_le_debit_mesure_ne_depasse_jamais_le_plafond(mediatheque, disque, monkeypatch) -> None:
    """Le cas vu sur un vrai disque : plafond a 4 Mo/s, debit affiche 590 Mo/s.

    Le bloc partait avant que le plafond ne le retienne, et le debit se
    mesurait des la premiere milliseconde. Avec une ecriture instantanee (le
    pire cas : tout part dans le cache), aucun releve ne doit depasser le
    plafond de plus que l'arrondi."""
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(8 * BLOC))
    horloge = [0.0]
    releves: list[dict] = []
    vraie_progression = moteur.Copie._progression

    def dormir(secondes: float) -> None:
        horloge[0] += secondes

    def progression(self, octets):
        vraie_progression(self, octets)
        releves.append(self.etat())

    monkeypatch.setattr(moteur.Copie, "_progression", progression)

    plafond = BLOC  # un bloc par seconde
    copier(
        disque,
        [tache(mediatheque, disque, nom)],
        max_mb_per_s=plafond / (1024 * 1024),
        horloge=lambda: horloge[0],
        dormir=dormir,
    )

    mesures = [r["current"]["speed_bps"] for r in releves] + [r["speed_bps"] for r in releves]
    assert max(mesures) <= plafond * 1.01
    assert horloge[0] == pytest.approx(8.0), "aucune attente apres le dernier bloc"


# --- Erreur de la mediatheque pendant une reprise -------------------------------


def test_une_source_illisible_pendant_la_verification_n_arrete_pas_la_file(
    mediatheque, disque, fsync_simule, monkeypatch
) -> None:
    """Pendant la verification d'une reprise, une EIO de la MEDIATHEQUE etait
    classee erreur du disque : toute la file s'arretait pour un film illisible.
    Ce fichier echoue ; les suivants sont copies."""
    nom = "Films/Dune/Dune.mkv"
    donnees = contenu(10 * BLOC)
    _, partiel, reprise = preparer_reprise(
        mediatheque, disque, nom, donnees, donnees[: 6 * BLOC], 6 * BLOC
    )
    poser(mediatheque, "Films/Alien/Alien.mkv", contenu(3 * BLOC))
    source_ino = (mediatheque / nom).stat().st_ino
    vrai_pread = os.pread

    def pread(fd, n, position):
        if os.fstat(fd).st_ino == source_ino:
            raise OSError(errno.EIO, os.strerror(errno.EIO))
        return vrai_pread(fd, n, position)

    monkeypatch.setattr(os, "pread", pread)

    copie = copier(
        disque,
        [tache(mediatheque, disque, nom), tache(mediatheque, disque, "Films/Alien/Alien.mkv")],
        reprise=reprise,
    )

    etat = copie.etat()
    assert etat["aborted"] is None, "une erreur de la source n'arrete pas la file"
    assert [e["source"] for e in etat["errors"]] == [nom]
    assert "lecture de la source" in etat["errors"][0]["message"]
    assert etat["files_done"] == 1
    assert (disque.path / "Films/Alien/Alien.mkv").exists()
    assert not partiel.exists(), "plus rien ne le reprendra : il est retire"


# --- L'identite du disque -------------------------------------------------------


def test_le_marqueur_est_pose_une_fois_puis_garde(disque, monkeypatch) -> None:
    ecrits: list[str] = []
    vrai_open = os.open

    def espion(chemin, drapeaux, *args, **kwargs):
        if drapeaux & (os.O_WRONLY | os.O_RDWR):
            # Par descripteur de dossier, sous un nom nu : jamais par chemin.
            assert kwargs.get("dir_fd") is not None, chemin
            ecrits.append(str(chemin))
        return vrai_open(chemin, drapeaux, *args, **kwargs)

    monkeypatch.setattr(os, "open", espion)

    marque = moteur.marquer_disque(disque)

    assert moteur.marque_valide(marque)
    assert (disque.path / moteur.MARQUEUR).read_text().strip() == marque
    assert ecrits == [f"{moteur.MARQUEUR}{moteur.SUFFIXE_PARTIEL}"]
    assert moteur.marquer_disque(disque) == marque
    assert moteur.lire_marque(disque) == marque
    assert list(disque.path.iterdir()) == [disque.path / moteur.MARQUEUR]


def test_en_root_le_marqueur_prend_l_identite_de_la_racine(disque, monkeypatch) -> None:
    """Pose en root, il resterait a root : l'utilisateur ne pourrait plus le
    supprimer du disque. Par descripteur, comme le reste."""
    changes: list[int] = []
    monkeypatch.setattr(moteur, "tourne_en_root", lambda: True)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: changes.append(os.fstat(fd).st_ino))

    moteur.marquer_disque(disque)

    assert changes == [(disque.path / moteur.MARQUEUR).stat().st_ino]


def test_un_marqueur_illisible_ou_un_lien_est_remplace_sans_etre_suivi(
    disque, tmp_path: Path
) -> None:
    (disque.path / moteur.MARQUEUR).write_text("pas un identifiant")
    premiere = moteur.marquer_disque(disque)
    assert moteur.marque_valide(premiere)

    ailleurs = tmp_path / "ailleurs.txt"
    ailleurs.write_text("intact")
    (disque.path / moteur.MARQUEUR).unlink()
    (disque.path / moteur.MARQUEUR).symlink_to(ailleurs)

    assert moteur.lire_marque(disque) is None, "un lien n'est jamais un marqueur"
    seconde = moteur.marquer_disque(disque)

    assert seconde != premiere
    assert ailleurs.read_text() == "intact"
    assert not (disque.path / moteur.MARQUEUR).is_symlink()


def test_un_dossier_a_la_place_du_marqueur_empeche_la_copie(disque) -> None:
    (disque.path / moteur.MARQUEUR).mkdir()

    with pytest.raises(moteur.ErreurMarque):
        moteur.marquer_disque(disque)

    assert sorted(p.name for p in disque.path.iterdir()) == [moteur.MARQUEUR]


def test_un_autre_disque_que_celui_de_la_copie_ne_recoit_rien(mediatheque, disque) -> None:
    """Le disque a pris une autre identite entre la recherche et l'ouverture de
    sa racine (un autre disque, au meme endroit) : rien n'est ecrit."""
    nom = "Films/Dune/Dune.mkv"
    poser(mediatheque, nom, contenu(3 * BLOC))
    moteur.marquer_disque(disque)
    disque.marque = "0f0f0f0f-0000-4000-8000-00000000abcd"

    copie = copier(disque, [tache(mediatheque, disque, nom)])

    assert "repère" in copie.etat()["aborted"]
    assert sorted(p.name for p in disque.path.iterdir()) == [moteur.MARQUEUR]
    assert not moteur.partiel_designe(disque, f"{nom}{moteur.SUFFIXE_PARTIEL}")
