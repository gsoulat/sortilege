"""Copie vers un disque externe : ce qui est propose comme disque, et ce qui ne l'est pas.

La regle qui prime : quand un disque USB est debranche, son point de montage
reste un dossier VIDE sur la partition systeme du NAS. Le proposer comme
destination, c'est remplir la partition systeme et faire tomber le NAS. Ces
tests verrouillent donc d'abord ce qui n'est JAMAIS propose.

Aucun vrai montage ici (CI non-root) : la detection est detournee —
``os.path.ismount`` et la table ``/proc/self/mountinfo``.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from sortilege.core import copie as moteur


@pytest.fixture
def montes(monkeypatch) -> set[str]:
    """Les chemins que le faux ``ismount`` declare montes."""
    ensemble: set[str] = set()
    monkeypatch.setattr(os.path, "ismount", lambda p: os.path.normpath(str(p)) in ensemble)
    return ensemble


@pytest.fixture
def racine(tmp_path: Path, monkeypatch) -> Path:
    dossier = tmp_path / "externes"
    dossier.mkdir()
    # Pas de table des montages par defaut : le systeme de fichiers est inconnu.
    monkeypatch.setattr(moteur, "MOUNTINFO", tmp_path / "mountinfo-absent")
    return dossier


def monter(montes: set[str], chemin: Path) -> Path:
    chemin.mkdir(parents=True, exist_ok=True)
    montes.add(os.path.normpath(str(chemin)))
    return chemin


def table(tmp_path: Path, monkeypatch, lignes: list[tuple[Path, str]]) -> None:
    """Ecrit une fausse table ``mountinfo`` : (point de montage, systeme de fichiers)."""
    contenu = "".join(
        f"{36 + i} 35 8:{i} / {str(p).replace(' ', chr(92) + '040')} rw,relatime shared:1 - "
        f"{fs} /dev/sd{chr(97 + i)}1 rw\n"
        for i, (p, fs) in enumerate(lignes)
    )
    fichier = tmp_path / "mountinfo"
    fichier.write_text(contenu, encoding="utf-8")
    monkeypatch.setattr(moteur, "MOUNTINFO", fichier)


def par_id(inventaire: moteur.Inventaire) -> dict[str, moteur.Disque]:
    return {d.id: d for d in inventaire.disques}


# --- Ce qui n'est JAMAIS propose ------------------------------------------------


def test_un_dossier_vide_non_monte_n_est_jamais_propose(racine: Path, montes) -> None:
    """Le point de montage d'un disque debranche : un dossier vide, sur le NAS."""
    (racine / "USB1").mkdir()

    disque = par_id(moteur.inventorier(racine))["USB1"]

    assert disque.refusal == moteur.REFUS_SANS_DISQUE
    assert not disque.mounted
    # Surtout pas la place libre de la partition systeme : elle ferait croire
    # qu'il y a de quoi copier.
    assert disque.free_bytes == 0
    assert disque.total_bytes == 0


def test_un_dossier_rempli_mais_non_monte_n_est_pas_un_disque(racine: Path, montes) -> None:
    """Des fichiers laisses dans un point de montage vide ne le rendent pas « branche »."""
    (racine / "USB1" / "Films").mkdir(parents=True)
    (racine / "USB1" / "Films" / "reste.mkv").write_bytes(b"x")

    assert par_id(moteur.inventorier(racine))["USB1"].refusal == moteur.REFUS_SANS_DISQUE


def test_un_montage_direct_vide_est_refuse(racine: Path, montes) -> None:
    monter(montes, racine / "USB1")

    disque = par_id(moteur.inventorier(racine))["USB1"]

    assert disque.refusal == moteur.REFUS_SANS_DISQUE


def test_le_parent_des_disques_sans_disque_branche_le_dit(racine: Path, montes) -> None:
    """Le parent monte en rslave, aucun disque branche : il ne reste que les
    points de montage, vides. Refuse, mais pas sous le motif « montage direct »,
    qui demanderait de faire ce qui est deja fait."""
    parent = monter(montes, racine / "usb")
    (parent / "USB1").mkdir()
    (parent / "USB2").mkdir()

    assert par_id(moteur.inventorier(racine))["usb"].refusal == moteur.REFUS_SANS_DISQUE


def test_un_lien_symbolique_n_est_jamais_un_disque(racine: Path, montes, tmp_path: Path) -> None:
    ailleurs = monter(montes, tmp_path / "ailleurs")
    (ailleurs / "film.mkv").write_bytes(b"x")
    (racine / "USB1").symlink_to(ailleurs, target_is_directory=True)

    assert moteur.inventorier(racine).disques == []


def test_la_mediatheque_n_est_jamais_une_destination(racine: Path, montes) -> None:
    """Une racine externe mal choisie ne doit pas proposer la mediatheque elle-meme."""
    mediatheque = monter(montes, racine / "NAS" / "media")

    disque = par_id(moteur.inventorier(racine, mediatheque=mediatheque))["NAS/media"]

    assert disque.refusal and "médiathèque" in disque.refusal


def test_le_volume_de_la_mediatheque_n_est_jamais_un_disque(
    racine: Path, montes, tmp_path: Path
) -> None:
    """Un montage bind de la mediatheque sous /externes : ``realpath`` n'y voit
    qu'un autre chemin. Le ``st_dev`` est celui de la mediatheque — refuse.

    Ici, le « disque » et la mediatheque sont deux dossiers du meme systeme de
    fichiers (celui des tests) : exactement ce qu'un bind donnerait."""
    mediatheque = tmp_path / "media"
    mediatheque.mkdir()
    monter(montes, racine / "USB1" / "usbshare")

    disque = par_id(moteur.inventorier(racine, mediatheque=mediatheque))["USB1/usbshare"]

    assert disque.refusal == moteur.REFUS_VOLUME_MEDIATHEQUE


def test_une_racine_absente_est_dite(tmp_path: Path) -> None:
    inventaire = moteur.inventorier(tmp_path / "nulle-part")

    assert not inventaire.existe
    assert inventaire.disques == []


# --- Ce qui est propose -------------------------------------------------------


def test_un_montage_imbrique_est_un_disque(racine: Path, montes, tmp_path, monkeypatch) -> None:
    """Le cas recommande : le parent des disques monte en rslave."""
    usb = monter(montes, racine / "USB1" / "usbshare")
    table(tmp_path, monkeypatch, [(usb, "exfat")])

    inventaire = moteur.inventorier(racine)

    assert inventaire.existe
    disque = par_id(inventaire)["USB1/usbshare"]
    assert disque.refusal is None
    assert disque.mounted
    assert disque.fs == "exfat"
    assert disque.writable
    assert disque.total_bytes > 0
    assert disque.max_file_bytes is None
    assert disque.to_dict()["path"] == str(usb)
    assert disque.to_dict()["label"] == "USB1/usbshare"
    # L'identite d'un disque ne sort jamais vers le client : ni repere de
    # volume (il changeait a chaque branchement), ni marqueur.
    assert "volume" not in disque.to_dict()
    assert disque.marque is None
    # Le dossier parent n'est pas propose en plus : c'est son contenu qui compte.
    assert "USB1" not in par_id(inventaire)


def test_les_montages_sont_cherches_sur_deux_niveaux(racine: Path, montes) -> None:
    monter(montes, racine / "sdb" / "part1" / "data")
    monter(montes, racine / "sdc" / "a" / "b" / "c")

    disques = par_id(moteur.inventorier(racine))

    assert disques["sdb/part1/data"].refusal is None
    # Trois niveaux sous « sdc » : au-dela de la recherche, donc pas de disque.
    assert disques["sdc"].refusal == moteur.REFUS_SANS_DISQUE
    assert "sdc/a/b/c" not in disques


def test_on_ne_cherche_pas_de_disque_dans_un_disque(racine: Path, montes) -> None:
    usb = monter(montes, racine / "USB1" / "usbshare")
    monter(montes, usb / "Films")

    assert set(par_id(moteur.inventorier(racine))) == {"USB1/usbshare"}


def test_un_montage_direct_non_vide_est_refuse(racine: Path, montes) -> None:
    """Un disque monte directement sur un dossier de la racine : liste, jamais
    choisi. Debranche, son montage reste, et un dossier qu'un tiers a rempli
    y passait pour lui — le contre-controle y a ecrit un film sur la partition
    systeme en deux clics. Seuls les montages imbriques sont des disques."""
    disque_usb = monter(montes, racine / "USB1")
    (disque_usb / "Films").mkdir()
    (disque_usb / "Films" / "Dune.mkv").write_bytes(b"x")

    disque = par_id(moteur.inventorier(racine))["USB1"]

    assert disque.refusal == moteur.REFUS_MONTAGE_DIRECT
    assert "PARENT" in disque.refusal and ":rslave" in disque.refusal
    assert not disque.mounted
    # Surtout pas la place du volume : elle ferait croire qu'on peut y copier.
    assert disque.free_bytes == 0


def test_fat32_limite_la_taille_d_un_fichier(racine: Path, montes, tmp_path, monkeypatch) -> None:
    usb = monter(montes, racine / "USB1" / "usbshare")
    table(tmp_path, monkeypatch, [(usb, "vfat")])

    disque = par_id(moteur.inventorier(racine))["USB1/usbshare"]

    assert disque.max_file_bytes == 4 * 1024**3 - 1


def test_la_table_des_montages_desechappe_les_espaces(tmp_path: Path, monkeypatch) -> None:
    point = tmp_path / "Mon disque"
    table(tmp_path, monkeypatch, [(point, "ntfs3")])

    montages = moteur.lire_montages()

    assert montages[os.path.normpath(str(point))].fs == "ntfs3"


def test_une_table_des_montages_absente_ne_cache_pas_le_disque(racine: Path, montes) -> None:
    """Hors Linux, pas de /proc : le disque reste propose, systeme de fichiers inconnu."""
    monter(montes, racine / "USB1" / "usbshare")

    disque = par_id(moteur.inventorier(racine))["USB1/usbshare"]

    assert disque.refusal is None
    assert disque.fs is None


def test_un_volume_fige_ne_bloque_pas_la_detection(racine: Path, monkeypatch) -> None:
    """Un disque qui s'endort, ou un montage fige, rend la main avec un refus."""
    (racine / "USB1").mkdir()
    liberation = threading.Event()

    def fige(chemin) -> bool:
        liberation.wait(10)
        return False

    monkeypatch.setattr(os.path, "ismount", fige)
    monkeypatch.setattr(moteur, "DELAI_VOLUME", 0.2)
    try:
        debut = time.monotonic()
        disque = par_id(moteur.inventorier(racine))["USB1"]
        duree = time.monotonic() - debut
    finally:
        liberation.set()

    assert duree < 2
    assert disque.refusal and "ne répond pas" in disque.refusal


# --- Identifiants venus du client -----------------------------------------------


@pytest.mark.parametrize(
    "ident",
    ["", "/etc", "../etc", "USB1/../../etc", "USB1/./usbshare", "USB1\\usbshare", "USB1//x"],
)
def test_un_identifiant_hors_de_la_racine_est_refuse(ident: str) -> None:
    assert not moteur.identifiant_valide(ident)


def test_un_identifiant_ordinaire_est_accepte() -> None:
    assert moteur.identifiant_valide("USB1/usbshare")
