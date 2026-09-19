"""Copie vers un disque externe : qu'est-ce qui y est deja, et ou va le reste ?

L'analyse n'ecrit rien. Elle decide pourtant de tout ce qui sera ecrit : une
erreur ici copie un film en double, le range a cote du bon dossier, ou — pire —
le declare present alors qu'il ne l'est pas, et il ne sera jamais copie.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sortilege.core import copie as moteur
from sortilege.core.collection import Work, group
from sortilege.core.scanner import ScanRules, scan

TOUT = ScanRules(min_size_bytes=0)
"""Les fichiers de test pesent quelques octets : pas de plancher."""


def poser(racine: Path, relatif: str, octets: int = 1000, contenu: bytes = b"v") -> Path:
    chemin = racine / relatif
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes((contenu * octets)[:octets])
    return chemin


def index(mediatheque: Path) -> dict[str, Work]:
    """L'index de la mediatheque, construit comme ``api/collection._build``."""
    fichiers = scan([mediatheque], deep=False, library_root=mediatheque, rules=TOUT).files
    return {w.key: w for w in group(fichiers)}


@pytest.fixture
def mediatheque(tmp_path: Path) -> Path:
    dossier = tmp_path / "media"
    dossier.mkdir()
    return dossier


@pytest.fixture
def usb(tmp_path: Path) -> Path:
    dossier = tmp_path / "externes" / "USB1" / "usbshare"
    dossier.mkdir(parents=True)
    return dossier


def disque(chemin: Path, *, fs: str | None = "ext4", libre: int = 10**12) -> moteur.Disque:
    return moteur.Disque(
        id="USB1/usbshare",
        path=chemin,
        mounted=True,
        fs=fs,
        free_bytes=libre,
        total_bytes=2 * 10**12,
        writable=True,
        dev=chemin.stat().st_dev,
    )


def analyser(mediatheque: Path, usb: Path, cles: list[str], **options) -> moteur.Analyse:
    fs = options.pop("fs", "ext4")
    libre = options.pop("libre", 10**12)
    oeuvres = [index(mediatheque)[c] for c in cles]
    return moteur.analyser(
        disque(usb, fs=fs, libre=libre), oeuvres, mediatheque, regles=TOUT, **options
    )


def cibles(analyse: moteur.Analyse) -> list[str]:
    return [t.cible_rel for t in analyse.taches]


# --- Absent, present, partiel -------------------------------------------------


def test_une_oeuvre_absente_reproduit_la_mediatheque(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 1234)

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    oeuvre = analyse.oeuvres[0]
    assert oeuvre["state"] == "absent"
    assert oeuvre["files_to_copy"] == 1
    assert oeuvre["bytes_to_copy"] == 1234
    assert oeuvre["target_dir"] == "Films/Dune (2021)"
    assert oeuvre["existing_dir"] is None
    assert cibles(analyse) == ["Films/Dune (2021)/Dune (2021).mkv"]
    assert analyse.to_dict()["totals"] == {
        "files_to_copy": 1,
        "bytes_to_copy": 1234,
        "files_present": 0,
    }


def test_une_oeuvre_deja_copiee_est_presente(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 1234)
    poser(usb, "Films/Dune (2021)/Dune (2021).mkv", 1234)

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    oeuvre = analyse.oeuvres[0]
    assert oeuvre["state"] == "present"
    assert oeuvre["files_present"] == 1
    assert analyse.taches == []


def test_un_film_present_sous_un_autre_nom_n_est_pas_recopie(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 1234)
    poser(usb, "Mes films/Dune/Dune.2021.1080p.mkv", 999)

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    oeuvre = analyse.oeuvres[0]
    assert oeuvre["state"] == "present"
    assert analyse.taches == []
    detail = oeuvre["details"][0]
    assert detail["status"] == "present_elsewhere"
    assert detail["found"] == "Mes films/Dune/Dune.2021.1080p.mkv"
    # Le film existe : c'est SON dossier qui compte, pas celui de la mediatheque.
    assert oeuvre["existing_dir"] == "Mes films/Dune"
    assert oeuvre["target_dir"] == "Mes films/Dune"


def test_un_lien_sur_le_disque_n_est_jamais_pris_pour_une_copie(
    mediatheque: Path, usb: Path
) -> None:
    """Un lien « Films -> <mediatheque>/Films » pose sur le disque : ``lstat``
    sur le chemin entier le traversait et trouvait le film de la mediatheque
    elle-meme — meme taille, donc « déjà présent », et jamais copie."""
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 1234)
    (usb / "Films").symlink_to(mediatheque / "Films", target_is_directory=True)

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    oeuvre = analyse.oeuvres[0]
    assert oeuvre["state"] == "absent"
    assert oeuvre["files_present"] == 0
    assert analyse.taches == []
    assert [i["reason"] for i in oeuvre["issues"]] == ["symlink"]
    assert moteur.REFUS_LIEN in oeuvre["issues"][0]["message"]
    assert "« Films »" in oeuvre["issues"][0]["message"]


def test_un_film_homonyme_d_une_autre_annee_n_est_pas_pris_pour_lui(
    mediatheque: Path, usb: Path
) -> None:
    """« Dune (1984) » sur le disque ne rend pas « Dune (2021) » present."""
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 1234)
    poser(usb, "Films/Dune (1984)/Dune (1984).mkv", 999)

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    assert analyse.oeuvres[0]["state"] == "absent"
    assert cibles(analyse) == ["Films/Dune (2021)/Dune (2021).mkv"]


def test_les_compagnons_d_un_film_vont_dans_son_dossier_existant(
    mediatheque: Path, usb: Path
) -> None:
    """Un film deja la : ce qui manque se range dans SON dossier du disque."""
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 1234)
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).fr.srt", 50)
    poser(usb, "Cinéma/Dune (2021)/Dune (2021).mkv", 1234)

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    oeuvre = analyse.oeuvres[0]
    assert oeuvre["existing_dir"] == "Cinéma/Dune (2021)"
    assert oeuvre["target_dir"] == "Cinéma/Dune (2021)"
    assert oeuvre["state"] == "partial"
    assert cibles(analyse) == ["Cinéma/Dune (2021)/Dune (2021).fr.srt"]


def test_severance_les_episodes_manquants_vont_dans_la_saison_du_disque(
    mediatheque: Path, usb: Path
) -> None:
    """Le cas du banc : « Saison 1 » sur le disque, « Season 01 » dans la mediatheque.

    Sans la regle des saisons, E02 et E03 iraient dans un second dossier
    « Season 01 » a cote de « Saison 1 » — exactement ce qu'on ne veut pas.
    """
    for n in (1, 2, 3):
        poser(mediatheque, f"SerieTV/Severance (2022)/Season 01/Severance - S01E0{n}.mkv", 1000)
    poser(usb, "Series/Severance/Saison 1/Severance.S01E01.1080p.mkv", 777)

    analyse = analyser(mediatheque, usb, ["episode:severance"])

    oeuvre = analyse.oeuvres[0]
    assert oeuvre["state"] == "partial"
    assert oeuvre["files_present"] == 1
    assert oeuvre["files_to_copy"] == 2
    assert oeuvre["existing_dir"] == "Series/Severance"
    assert oeuvre["target_dir"] == "Series/Severance"
    assert cibles(analyse) == [
        "Series/Severance/Saison 1/Severance - S01E02.mkv",
        "Series/Severance/Saison 1/Severance - S01E03.mkv",
    ]
    ailleurs = [d for d in oeuvre["details"] if d["status"] == "present_elsewhere"]
    assert [d["found"] for d in ailleurs] == [
        "Series/Severance/Saison 1/Severance.S01E01.1080p.mkv"
    ]


def test_une_saison_absente_du_disque_prend_le_nom_de_la_mediatheque(
    mediatheque: Path, usb: Path
) -> None:
    poser(mediatheque, "SerieTV/Severance (2022)/Season 01/Severance - S01E01.mkv")
    poser(mediatheque, "SerieTV/Severance (2022)/Season 02/Severance - S02E01.mkv")
    poser(usb, "Series/Severance/Saison 1/Severance.S01E01.1080p.mkv", 777)

    analyse = analyser(mediatheque, usb, ["episode:severance"])

    assert cibles(analyse) == ["Series/Severance/Season 02/Severance - S02E01.mkv"]


def test_un_dossier_de_saison_vide_du_disque_est_reconnu_par_son_numero(
    mediatheque: Path, usb: Path
) -> None:
    poser(mediatheque, "SerieTV/Severance (2022)/Season 01/Severance - S01E01.mkv")
    poser(mediatheque, "SerieTV/Severance (2022)/Season 02/Severance - S02E01.mkv")
    poser(usb, "Series/Severance/Saison 1/Severance.S01E01.1080p.mkv", 777)
    (usb / "Series" / "Severance" / "S2").mkdir()

    analyse = analyser(mediatheque, usb, ["episode:severance"])

    assert cibles(analyse) == ["Series/Severance/S2/Severance - S02E01.mkv"]


# --- Ce qui n'est jamais copie ------------------------------------------------


def test_un_conflit_de_taille_n_est_jamais_ecrase(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 1234)
    sur_disque = poser(usb, "Films/Dune (2021)/Dune (2021).mkv", 99, b"d")

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    oeuvre = analyse.oeuvres[0]
    assert analyse.taches == []
    assert [i["reason"] for i in oeuvre["issues"]] == ["conflict"]
    assert "jamais écrasé" in oeuvre["issues"][0]["message"]
    assert sur_disque.read_bytes() == b"d" * 99


def test_un_nom_refuse_par_exfat_est_signale_et_pas_renomme(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Star Wars: A New Hope (1977)/Star Wars: A New Hope (1977).mkv")

    analyse = analyser(mediatheque, usb, ["movie:star wars: a new hope"], fs="exfat")

    oeuvre = analyse.oeuvres[0]
    assert analyse.taches == []
    assert [i["reason"] for i in oeuvre["issues"]] == ["name_invalid"]
    # Le meme nom passe sur un systeme de fichiers Linux.
    assert cibles(analyser(mediatheque, usb, ["movie:star wars: a new hope"], fs="ext4")) == [
        "Films/Star Wars: A New Hope (1977)/Star Wars: A New Hope (1977).mkv"
    ]


def test_un_fichier_de_plus_de_4_gio_est_refuse_sur_fat32(mediatheque: Path, usb: Path) -> None:
    film = mediatheque / "Films" / "Dune (2021)" / "Dune (2021).mkv"
    film.parent.mkdir(parents=True)
    film.touch()
    os.truncate(film, moteur.LIMITE_FAT32 + 1)  # creux : ne coute rien

    analyse = analyser(mediatheque, usb, ["movie:dune"], fs="vfat")

    assert analyse.taches == []
    assert [i["reason"] for i in analyse.oeuvres[0]["issues"]] == ["too_large"]


def test_une_source_disparue_est_signalee(mediatheque: Path, usb: Path) -> None:
    film = poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv")
    oeuvres = [index(mediatheque)["movie:dune"]]
    film.unlink()

    analyse = moteur.analyser(disque(usb), oeuvres, mediatheque, regles=TOUT)

    assert [i["reason"] for i in analyse.oeuvres[0]["issues"]] == ["missing_source"]


# --- Compagnons et place ------------------------------------------------------


def test_les_compagnons_suivent_la_video(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv")
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).fr.srt", 10)
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).nfo", 10)
    poser(mediatheque, "Films/Dune (2021)/poster.jpg", 10)

    avec = analyser(mediatheque, usb, ["movie:dune"])
    sans = analyser(mediatheque, usb, ["movie:dune"], avec_compagnons=False)

    assert sorted(cibles(avec)) == [
        "Films/Dune (2021)/Dune (2021).fr.srt",
        "Films/Dune (2021)/Dune (2021).mkv",
        "Films/Dune (2021)/Dune (2021).nfo",
        "Films/Dune (2021)/poster.jpg",
    ]
    assert cibles(sans) == ["Films/Dune (2021)/Dune (2021).mkv"]


def test_les_compagnons_d_un_film_present_sous_un_autre_nom_ne_sont_pas_semes(
    mediatheque: Path, usb: Path
) -> None:
    """Un « Dune (2021).fr.srt » a cote de « Dune.2021.1080p.mkv » ne servirait a rien."""
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv")
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).fr.srt", 10)
    poser(usb, "Films/Dune/Dune.2021.1080p.mkv")

    assert analyser(mediatheque, usb, ["movie:dune"]).taches == []


def test_la_place_insuffisante_est_chiffree(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 5000)
    # Sous la marge : c'est elle qui fait refuser, le fichier ne pese rien.
    libre = 10 * 1024**2

    analyse = analyser(mediatheque, usb, ["movie:dune"], libre=libre)

    marge = 64 * 1024**2 + 1 * 2 * 1024**2
    assert analyse.marge == marge
    assert not analyse.tient
    assert analyse.manque == 5000 + marge - libre
    rendu = analyse.to_dict()
    assert rendu["fits"] is False
    assert rendu["missing_bytes"] == 5000 + marge - libre
    assert rendu["margin_bytes"] == marge
    assert "il manque" in analyse.motif_place()


def test_la_place_suffisante_tient(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 5000)

    analyse = analyser(mediatheque, usb, ["movie:dune"])

    assert analyse.tient
    assert analyse.to_dict()["missing_bytes"] == 0


def test_l_analyse_n_ecrit_rien(mediatheque: Path, usb: Path) -> None:
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv")
    avant_media = sorted(p.relative_to(mediatheque) for p in mediatheque.rglob("*"))

    analyser(mediatheque, usb, ["movie:dune"])

    assert list(usb.iterdir()) == []
    assert sorted(p.relative_to(mediatheque) for p in mediatheque.rglob("*")) == avant_media


def test_racine_d_oeuvre_remonte_d_un_cran_sur_une_saison() -> None:
    assert str(moteur.racine_d_oeuvre(["S/X/Season 01/a.mkv", "S/X/Season 01/b.mkv"])) == "S/X"
    assert str(moteur.racine_d_oeuvre(["S/X/Saison 1/a.mkv", "S/X/Saison 2/b.mkv"])) == "S/X"
    assert str(moteur.racine_d_oeuvre(["Films/Dune/Dune.mkv"])) == "Films/Dune"
    assert moteur.racine_d_oeuvre(["a.mkv"]).parts == ()


def test_la_marge_ne_grossit_pas_avec_le_disque(mediatheque: Path, usb: Path) -> None:
    """1 % d'un disque de 4 To, c'etait 40 Go de marge : une copie de 30 Go
    etait refusee avec 45 Go libres. La marge suit le nombre de fichiers."""
    poser(mediatheque, "Films/Dune (2021)/Dune (2021).mkv", 5000)
    libre = 45 * 1024**3

    analyse = analyser(mediatheque, usb, ["movie:dune"], libre=libre)

    assert analyse.tient
    assert analyse.marge < 100 * 1024**2
