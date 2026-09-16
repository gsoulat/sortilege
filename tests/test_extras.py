"""Nettoyage des fichiers annexes de la bibliotheque.

Le depot d'affiches et de fiches remplit la bibliotheque de petits fichiers.
L'utilisateur veut les retirer par categorie. Les tests portent surtout sur ce
que le nettoyage REFUSE de toucher : videos, livres, pistes audio, fichiers
non reconnus, structures de disque, corbeille, dossiers systeme, liens
symboliques, racines — et sur l'annulation d'un rangement, qui ne doit pas se
mettre a echouer parce qu'une affiche a ete retiree entre-temps.

La regression principale rejoue une passe reelle : « autres » prenait tout ce
qui n'etait ni image, ni fiche, ni sous-titre, et un nettoyage « supprimer »
de toutes les categories a detruit 82 fichiers precieux (videos .ogm et .mk3d,
pistes TrueHD, livres .azw, DVD aplatis, HD DVD).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PosixPath

import pytest
from fastapi.testclient import TestClient

from sortilege.api import library
from sortilege.config import get_settings
from sortilege.core import nfo
from sortilege.core.companions import (
    DECHETS_EXTENSIONS,
    DECHETS_NOMS,
    EXTRA_CATEGORIES,
    TRASH_DIRNAME,
    find_extras,
    free_trash_destination,
)
from sortilege.core.journal import Journal, apply_plan, undo_plans
from sortilege.core.nfo import LocalMetadataSettings
from sortilege.core.planner import Plan
from sortilege.core.preferences import PreferenceStore
from sortilege.core.scoring import Decision
from sortilege.main import app

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
TOUT = {"categories": list(EXTRA_CATEGORIES), "mode": "delete", "confirm": True}


def _ecrire(chemin: Path, contenu: bytes = b"x") -> Path:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(contenu)
    return chemin


def _relatifs(racine: Path, cle: str) -> set[str]:
    return {f.relative_to(racine).as_posix() for f in find_extras(racine).categories[cle].files}


def _proposes(racine: Path) -> set[str]:
    return {
        f.relative_to(racine).as_posix()
        for c in find_extras(racine).categories.values()
        for f in c.files
    }


def _empreinte(chemin: Path) -> str:
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


@pytest.fixture
def media(tmp_path: Path) -> Path:
    racine = tmp_path / "media"
    racine.mkdir()
    return racine


@pytest.fixture
def client(media: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Client connecte, dont la bibliotheque est detournee vers ``media``."""
    conf = get_settings().model_copy(update={"library_root": media})
    monkeypatch.setattr(library, "get_settings", lambda: conf)
    with TestClient(app) as c:
        connexion = c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"})
        assert connexion.status_code == 200
        yield c


# --- Regression : les 82 fichiers detruits ---------------------------------------

# Chaque chemin est relatif a la racine. Liste reprise telle quelle du controle
# qui a revele le defaut : tous ont ete proposes dans « autres », puis supprimes.
DETRUITS_PAR_LE_CONTROLE = [
    *(
        f"VideosRares/Film{e}"
        for e in (
            ".ogm .mk3d .evo .m2t .trp .tp .mod .tod .dv .mxf .wtv .dvr-ms .3g2 .qt .h264 "
            ".hevc .264 .vro .m1v .mpv .y4m .amv .nsv .img .mdf .nrg .bin .rec .xvid .mp2v "
            ".mpe .ogx"
        ).split()
    ),
    "Decoupe/Film.mkv.001",
    "Decoupe/Film.mkv.002",
    *(
        f"AudioRare/Piste{e}"
        for e in (
            ".aiff .aif .ape .wv .alac .dsf .dff .aax .aa .thd .truehd .dtshd .mlp .mp2 .mpa "
            ".mpc .amr .caf .tta .oga .weba .spx .m4r"
        ).split()
    ),
    *(
        f"LivresRares/Livre{e}"
        for e in (
            ".azw .kfx .cb7 .cbt .cba .lit .prc .pdb .chm .rtf .doc .docx .odt .lrf .ibooks "
            ".xps .kepub .djv .txt"
        ).split()
    ),
    "DVDplat/VIDEO_TS.IFO",
    "DVDplat/VTS_01_0.IFO",
    "DVDplat/VTS_01_0.BUP",
    "HDDVD/HVDVD_TS/FEATURE_1.EVO",
    "HDDVD/HVDVD_TS/HV000I01.IFO",
    "HDDVD/HVDVD_TS/FEATURE_1.MAP",
]

# Detruits eux aussi, et tout aussi precieux : telechargements en cours,
# archives, sauvegardes posees par Sortilege, base Calibre.
DETRUITS_DIVERS = [
    "Partiels/Film.mkv.part",
    "Partiels/Film.mkv.!qB",
    "Partiels/Film.mp4.crdownload",
    "Partiels/Film.mkv.!ut",
    "Archives/Film.rar",
    "Archives/Film.r00",
    "Archives/Film.part1.rar",
    "Archives/Film.7z",
    "Archives/Film.zip",
    "Bak/Film.nfo.bak",
    "Bak/movie.nfo.20260101T101010.bak",
    "Calibre/metadata.db",
    "Calibre/metadata_db_prefs_backup.json",
    "Comics/ComicInfo.xml",
]


def test_la_liste_du_controle_est_complete() -> None:
    assert len(DETRUITS_PAR_LE_CONTROLE) == len(set(DETRUITS_PAR_LE_CONTROLE)) == 82


def test_les_82_fichiers_detruits_survivent_a_un_nettoyage_complet(
    client: TestClient, media: Path
) -> None:
    precieux = DETRUITS_PAR_LE_CONTROLE + DETRUITS_DIVERS
    for rel in precieux:
        _ecrire(media / rel, f"contenu unique de {rel}".encode())
    # De vraies annexes, pour que la passe ait quelque chose a faire.
    annexes = [
        "Film/poster.jpg",
        "Film/Film.nfo",
        "Film/Film.fr.srt",
        "Film/Film.sfv",
        "Film/Film.trickplay/320 - 10x10/0.jpg",
    ]
    for rel in annexes:
        _ecrire(media / rel)
    avant = {rel: _empreinte(media / rel) for rel in precieux}

    assert _proposes(media) == set(annexes)
    corps = client.post("/api/library/extras/prune", json=TOUT).json()

    assert corps["removed"] == len(annexes) and corps["failed_count"] == 0
    apres = {rel: _empreinte(media / rel) for rel in precieux if (media / rel).is_file()}
    assert apres == avant


# --- Inventaire ---------------------------------------------------------------


def test_chaque_fichier_tombe_dans_une_seule_categorie(media: Path) -> None:
    film = media / "Film (2020)"
    _ecrire(film / "Film (2020).mkv")
    _ecrire(film / "poster.jpg")
    _ecrire(film / "fanart.jpg")
    _ecrire(film / "Film (2020).nfo")
    _ecrire(film / "Film (2020).fr.srt")
    _ecrire(film / "Film (2020).sfv")
    _ecrire(film / "Thumbs.db")
    _ecrire(film / "Film (2020).trickplay" / "320 - 10x10" / "0.jpg")
    _ecrire(film / "notes.txt")
    _ecrire(film / "sans_extension")
    livre = media / "Livres" / "Auteur"
    _ecrire(livre / "Livre.epub")
    _ecrire(livre / "metadata.opf")
    _ecrire(livre / "cover.jpg")

    extras = find_extras(media)

    assert _relatifs(media, "trickplay") == {"Film (2020)/Film (2020).trickplay/320 - 10x10/0.jpg"}
    assert _relatifs(media, "images") == {
        "Film (2020)/poster.jpg",
        "Film (2020)/fanart.jpg",
        "Livres/Auteur/cover.jpg",
    }
    assert _relatifs(media, "fiches") == {
        "Film (2020)/Film (2020).nfo",
        "Livres/Auteur/metadata.opf",
    }
    assert _relatifs(media, "sous_titres") == {"Film (2020)/Film (2020).fr.srt"}
    assert _relatifs(media, "autres") == {"Film (2020)/Film (2020).sfv", "Film (2020)/Thumbs.db"}
    tous = [f for c in extras.categories.values() for f in c.files]
    assert len(tous) == len(set(tous)) == 9
    assert extras.total_bytes == 9
    # « notes.txt » est un livre possible, « sans_extension » n'est pas reconnu.
    assert (extras.books, extras.unknown) == (2, 1)


def test_videos_livres_audio_et_corbeille_ne_sont_jamais_proposes(media: Path) -> None:
    _ecrire(media / "Film" / "Film.mkv")
    _ecrire(media / "Film" / "Film.fr.mka")
    _ecrire(media / "Film" / "Bonus.webm")
    _ecrire(media / "Livres" / "Livre.pdf")
    _ecrire(media / TRASH_DIRNAME / "2026-01-01" / "poster.jpg")
    _ecrire(media / "Film" / "@eaDir" / "vignette.jpg")
    _ecrire(media / "Disque" / "VIDEO_TS" / "VTS_01_0.IFO")
    _ecrire(media / "Disque" / "VIDEO_TS" / "VTS_01_1.VOB")

    extras = find_extras(media)

    assert all(not c.files for c in extras.categories.values())
    assert (extras.videos, extras.books, extras.audio) == (3, 1, 1)


def test_un_lien_symbolique_n_est_jamais_propose(media: Path, tmp_path: Path) -> None:
    """Un lien peut designer un fichier HORS de la bibliotheque."""
    dehors = _ecrire(tmp_path / "ailleurs" / "precieux.jpg", b"precieux")
    (media / "Film").mkdir()
    (media / "Film" / "lien.jpg").symlink_to(dehors)
    (media / "Film" / "lien.sfv").symlink_to(dehors)
    (media / "dossier-lien").symlink_to(dehors.parent, target_is_directory=True)

    extras = find_extras(media)

    assert all(not c.files for c in extras.categories.values())


def test_les_dechets_reconnus_sont_proposes_dans_autres(media: Path) -> None:
    attendus = {f"Film/release{e}" for e in DECHETS_EXTENSIONS}
    # Autre radical : le systeme de fichiers de macOS ignore la casse.
    attendus |= {"Film/majuscules.SFV", "Film/majuscules.Nzb"}
    for rel in attendus:
        _ecrire(media / rel)
    for nom in ("Thumbs.db", "EHTHUMBS.DB", "Desktop.ini", ".DS_Store"):
        _ecrire(media / "Film" / nom)
        attendus.add(f"Film/{nom}")

    assert _relatifs(media, "autres") == attendus
    assert DECHETS_NOMS == {"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"}


@pytest.mark.parametrize(
    "nom",
    [
        "notes.txt",  # un livre possible
        "ComicInfo.xml",
        "Film.nfo.bak",  # l'original qu'une fiche a remplace
        "rip.log",  # journal d'extraction d'un CD
        "rip.cue",
        "sans_extension",
        "Film.mkv.part",
        "Film.mkv.!qB",
        "Film.rar",
        "metadata.db",
        "reglages.json",
        ".cache-cache",  # fichier cache : ni propose, ni compte
    ],
)
def test_un_fichier_non_reconnu_n_est_jamais_propose(media: Path, nom: str) -> None:
    _ecrire(media / "Film" / nom)

    assert _proposes(media) == set()


def test_les_inconnus_sont_comptes_avec_huit_exemples_au_plus(
    client: TestClient, media: Path
) -> None:
    for i in range(11):
        _ecrire(media / "Film" / f"inconnu-{i:02}.xyz")
    _ecrire(media / "Film" / ".cache")  # cache : pas un inconnu

    protege = client.get("/api/library/extras").json()["protected"]

    assert protege["unknown"] == 11
    assert protege["unknown_samples"] == [f"Film/inconnu-{i:02}.xyz" for i in range(8)]


def test_un_disque_aplati_est_protege_ou_qu_il_soit(media: Path) -> None:
    for nom in (
        "VIDEO_TS.IFO",
        "VTS_01_0.BUP",
        "index.bdmv",
        "00000.mpls",
        "00000.clpi",
        "FEATURE_1.EVO",
    ):
        _ecrire(media / "Plat" / nom)
    # HD DVD : tout son dossier, meme ce qui ressemble a une annexe.
    _ecrire(media / "HDDVD" / "HVDVD_TS" / "FEATURE_1.MAP")
    _ecrire(media / "HDDVD" / "hvdvd_ts" / "cover.jpg")
    _ecrire(media / "BR" / "BDMV" / "META" / "DL" / "bdmt_fra.xml")
    _ecrire(media / "BR" / "BDMV" / "META" / "DL" / "cover.jpg")

    extras = find_extras(media)

    assert _proposes(media) == set()
    assert extras.unknown == 0  # une structure de disque n'est pas « inconnue »


@pytest.mark.parametrize(
    "dossier",
    [
        "$RECYCLE.BIN",
        "$Recycle.Bin",
        "System Volume Information",
        "SYSTEM VOLUME INFORMATION",
        "System\u00a0Volume  Information",  # espace insecable, blancs doubles
        "lost+found",
        "Lost+Found",
        "@__thumb",
        "@__Thumb",
        "Network Trash Folder",
        "Temporary Items",
        "temporary items",
        "@eaDir",
        "#recycle",
        "@SynoResource",
        "@Recycle.Bin",
    ],
)
def test_un_dossier_systeme_n_est_jamais_parcouru(media: Path, dossier: str) -> None:
    _ecrire(media / dossier / "S-1-5-21" / "poster.jpg")
    _ecrire(media / dossier / "Film.nfo")
    _ecrire(media / dossier / "desktop.ini")

    extras = find_extras(media)

    assert _proposes(media) == set()
    assert extras.unknown == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root lit tous les dossiers")
def test_les_dossiers_illisibles_sont_comptes(client: TestClient, media: Path) -> None:
    _ecrire(media / "Prive" / "poster.jpg")
    _ecrire(media / "Ouvert" / "poster.jpg")
    os.chmod(media / "Prive", 0o000)
    try:
        corps = client.get("/api/library/extras").json()
    finally:
        os.chmod(media / "Prive", 0o700)

    assert corps["skipped_dirs"] == 1
    assert corps["categories"][1]["samples"] == ["Ouvert/poster.jpg"]


# --- Trickplay -------------------------------------------------------------------


def test_les_vignettes_trickplay_ont_leur_propre_categorie(media: Path) -> None:
    film = media / "Film (2020)"
    _ecrire(film / "Film (2020).mkv")
    _ecrire(film / "poster.jpg")
    _ecrire(film / "Film (2020).trickplay" / "320 - 10x10" / "0.jpg")
    _ecrire(film / "Film (2020).trickplay" / "320 - 10x10" / "1.jpg")
    # Non reconnu ailleurs, propose ici : ce dossier est un cache regenere.
    _ecrire(film / "Bonus.TrickPlay" / "index.bif")  # casse indifferente
    _ecrire(film / "Film (2020).trickplay" / "Thumbs.db")

    extras = find_extras(media)

    assert _relatifs(media, "trickplay") == {
        "Film (2020)/Film (2020).trickplay/320 - 10x10/0.jpg",
        "Film (2020)/Film (2020).trickplay/320 - 10x10/1.jpg",
        "Film (2020)/Bonus.TrickPlay/index.bif",
        "Film (2020)/Film (2020).trickplay/Thumbs.db",
    }
    # Les images d'un .trickplay ne comptent pas parmi les images.
    assert _relatifs(media, "images") == {"Film (2020)/poster.jpg"}
    assert _relatifs(media, "autres") == set()
    assert extras.unknown == 0


def test_une_video_dans_un_trickplay_reste_protegee(media: Path) -> None:
    dossier = media / "Film" / "Film.trickplay"
    for nom in ("Film.mkv", "Film.ogm", "Livre.azw", "Piste.thd", "VTS_01_0.IFO"):
        _ecrire(dossier / nom)
    _ecrire(dossier / "VIDEO_TS" / "cover.jpg")
    _ecrire(dossier / "0.jpg")

    extras = find_extras(media)

    assert _proposes(media) == {"Film/Film.trickplay/0.jpg"}
    assert (extras.videos, extras.books, extras.audio) == (2, 1, 1)


def test_une_racine_au_nom_trompeur_ne_fait_pas_tout_basculer(tmp_path: Path) -> None:
    racine = tmp_path / "Films.trickplay"
    _ecrire(racine / "Film" / "Film.mkv")
    _ecrire(racine / "Film" / "poster.jpg")
    _ecrire(racine / "Film" / "inconnu.xyz")

    extras = find_extras(racine)

    assert _relatifs(racine, "trickplay") == set()
    assert _relatifs(racine, "images") == {"Film/poster.jpg"}
    assert extras.unknown == 1


def test_retirer_les_trickplay_vide_leurs_dossiers_sans_toucher_l_affiche(
    client: TestClient, media: Path
) -> None:
    film = media / "Film"
    video = _ecrire(film / "Film.mkv")
    affiche = _ecrire(film / "poster.jpg")
    _ecrire(film / "Film.trickplay" / "320 - 10x10" / "0.jpg")
    _ecrire(film / "Film.trickplay" / "320 - 10x10" / "1.jpg")
    _ecrire(film / "Film.trickplay" / "640 - 10x10" / "0.jpg")

    corps = client.post(
        "/api/library/extras/prune",
        json={"categories": ["trickplay"], "mode": "delete", "confirm": True},
    ).json()

    assert (corps["removed"], corps["failed_count"]) == (3, 0)
    assert not (film / "Film.trickplay").exists()
    assert corps["removed_dirs"] == 3
    assert video.is_file() and affiche.is_file()


# --- GET /api/library/extras ---------------------------------------------------


def test_les_cinq_categories_sont_toujours_rendues_dans_l_ordre(client: TestClient) -> None:
    corps = client.get("/api/library/extras").json()

    assert [c["key"] for c in corps["categories"]] == [
        "trickplay",
        "images",
        "fiches",
        "sous_titres",
        "autres",
    ]
    assert [c["key"] for c in corps["categories"]] == list(EXTRA_CATEGORIES)
    assert [c["label"] for c in corps["categories"]] == [
        "Vignettes Jellyfin (.trickplay)",
        "Images",
        "Fiches .nfo et .opf",
        "Sous-titres",
        "Autres restes reconnus",
    ]
    assert all(
        c["count"] == 0 and c["bytes"] == 0 and c["samples"] == [] for c in corps["categories"]
    )
    assert corps["protected"] == {
        "videos": 0,
        "audio": 0,
        "books": 0,
        "unknown": 0,
        "unknown_samples": [],
    }
    assert corps["skipped_dirs"] == 0
    assert corps["total_bytes"] == 0
    assert set(corps["redeposit"]) == {"artwork", "nfo", "opf"}
    assert corps["trash_path"].endswith(TRASH_DIRNAME)


def test_les_exemples_sont_bornes_a_huit_et_relatifs(client: TestClient, media: Path) -> None:
    for i in range(12):
        _ecrire(media / "Film" / f"image-{i:02}.jpg", b"12345")

    images = client.get("/api/library/extras").json()["categories"][1]

    assert images["key"] == "images"
    assert images["count"] == 12
    assert images["bytes"] == 60
    assert len(images["samples"]) == 8
    assert all(s.startswith("Film/") for s in images["samples"])


def test_une_bibliotheque_absente_dit_pourquoi(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conf = get_settings().model_copy(update={"library_root": tmp_path / "nulle-part"})
    monkeypatch.setattr(library, "get_settings", lambda: conf)

    reponse = client.get("/api/library/extras")

    assert reponse.status_code == 404
    assert "Bibliothèque introuvable" in reponse.json()["detail"]


def test_l_interface_liste_les_memes_dechets_que_le_serveur() -> None:
    """L'ecran recopie la liste de « autres » : elle ne doit pas deriver."""
    vue = Path(__file__).parents[1] / "ui" / "src" / "components" / "ExtrasCleanup.vue"
    source = vue.read_text(encoding="utf-8")

    for extension in DECHETS_EXTENSIONS:
        assert f"'{extension}'" in source, extension
    for nom in DECHETS_NOMS:
        assert f"'{nom}'" in source.lower(), nom


# --- Noms non UTF-8 --------------------------------------------------------------

# Sur un NAS Linux, un nom en latin-1 est valide : Python le rend avec des
# « surrogates ». APFS le refuse, on le simule donc au plus pres.
NOM_LATIN1 = os.fsdecode(b"caf\xe9.jpg")


def test_un_nom_non_utf8_devient_affichable() -> None:
    affiche = library.displayable(f"Film/{NOM_LATIN1}")

    assert affiche == "Film/caf�.jpg"
    json.dumps(affiche, ensure_ascii=False).encode("utf-8")
    assert library.displayable("Été/Amélie.jpg") == "Été/Amélie.jpg"


def _chemin_latin1(racine: Path, vrai: Path) -> Path:
    """Un chemin au nom latin-1, qui se comporte comme le fichier ordinaire ``vrai``."""

    class CheminLatin1(PosixPath):
        def lstat(self):
            return os.lstat(vrai)

        def resolve(self, strict=False):
            return racine.resolve() / "F" / NOM_LATIN1

    return CheminLatin1(racine / "F" / NOM_LATIN1)


def test_un_nom_non_utf8_ne_fait_pas_echouer_le_releve(
    client: TestClient, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vrai = _ecrire(media / "F" / "vrai.jpg")
    vrai_find = library.find_extras

    def avec_nom_latin1(racine):
        extras = vrai_find(racine)
        extras.categories["images"].files.insert(0, _chemin_latin1(racine, vrai))
        extras.unknown_samples.append(racine / "F" / os.fsdecode(b"r\xe9sum\xe9.xyz"))
        extras.unknown += 1
        return extras

    monkeypatch.setattr(library, "find_extras", avec_nom_latin1)

    reponse = client.get("/api/library/extras")

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["categories"][1]["samples"][0] == "F/caf�.jpg"
    assert corps["protected"]["unknown_samples"] == ["F/r�sum�.xyz"]


def test_un_nom_non_utf8_echoue_seul_et_la_passe_continue(
    client: TestClient, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vrai = _ecrire(media / "F" / "vrai.jpg", b"vrai")
    autre = _ecrire(media / "F" / "autre.jpg", b"autre")
    _ecrire(media / "F" / "F.mkv")
    vrai_find = library.find_extras

    def avec_nom_latin1(racine):
        extras = vrai_find(racine)
        extras.categories["images"].files.insert(0, _chemin_latin1(racine, vrai))
        return extras

    monkeypatch.setattr(library, "find_extras", avec_nom_latin1)

    reponse = client.post(
        "/api/library/extras/prune",
        json={"categories": ["images"], "mode": "trash", "confirm": True},
    )

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["failed_count"] == 1
    assert corps["failed"][0].startswith("F/caf�.jpg : ")
    assert corps["removed"] == 2
    assert not autre.exists() and not vrai.exists()
    assert library._nettoyage_annexes.acquire(blocking=False)
    library._nettoyage_annexes.release()


# --- POST /api/library/extras/prune : refus ------------------------------------


@pytest.mark.parametrize(
    ("corps", "attendu"),
    [
        ({"categories": ["images"], "mode": "trash"}, "confirmé"),
        ({"categories": ["images"], "mode": "trash", "confirm": False}, "confirmé"),
        ({"categories": [], "mode": "trash", "confirm": True}, "Aucune catégorie"),
        ({"categories": ["videos"], "mode": "trash", "confirm": True}, "Catégorie inconnue"),
        ({"categories": ["images"], "mode": "broyer", "confirm": True}, "Mode inconnu"),
    ],
)
def test_une_demande_incomplete_est_refusee_et_ne_touche_a_rien(
    client: TestClient, media: Path, corps: dict, attendu: str
) -> None:
    affiche = _ecrire(media / "Film" / "poster.jpg")

    reponse = client.post("/api/library/extras/prune", json=corps)

    assert reponse.status_code == 400
    assert attendu in reponse.json()["detail"]
    assert affiche.is_file()


@pytest.mark.parametrize("confirm", ["yes", "true", 1, "1", None, [True]])
def test_seul_le_booleen_vrai_confirme(client: TestClient, media: Path, confirm: object) -> None:
    affiche = _ecrire(media / "Film" / "poster.jpg")

    reponse = client.post(
        "/api/library/extras/prune",
        json={"categories": ["images"], "mode": "delete", "confirm": confirm},
    )

    assert reponse.status_code == 422
    assert reponse.json()["detail"][0]["loc"] == ["body", "confirm"]
    assert affiche.is_file()


def test_un_nettoyage_en_cours_refuse_le_second(client: TestClient, media: Path) -> None:
    affiche = _ecrire(media / "Film" / "poster.jpg")
    assert library._nettoyage_annexes.acquire(blocking=False)
    try:
        reponse = client.post(
            "/api/library/extras/prune",
            json={"categories": ["images"], "mode": "trash", "confirm": True},
        )
    finally:
        library._nettoyage_annexes.release()

    assert reponse.status_code == 409
    assert "déjà en cours" in reponse.json()["detail"]
    assert affiche.is_file()


# --- POST /api/library/extras/prune : action -----------------------------------


def test_la_corbeille_recoit_les_fichiers_sans_ecraser_un_homonyme(
    client: TestClient, media: Path
) -> None:
    affiche = _ecrire(media / "Film" / "poster.jpg", b"nouvelle")
    _ecrire(media / "Film" / "Film.mkv")
    corbeille = media / TRASH_DIRNAME
    homonyme = _ecrire(free_trash_destination(corbeille, affiche), b"ancienne")

    reponse = client.post(
        "/api/library/extras/prune",
        json={"categories": ["images"], "mode": "trash", "confirm": True},
    )

    corps = reponse.json()
    assert reponse.status_code == 200
    assert corps["mode"] == "trash"
    assert (corps["removed"], corps["bytes"], corps["failed_count"]) == (1, 8, 0)
    assert corps["trash_path"] == str(corbeille)
    assert not affiche.exists()
    assert homonyme.read_bytes() == b"ancienne"
    contenus = sorted(p.read_bytes() for p in corbeille.rglob("*") if p.is_file())
    assert contenus == [b"ancienne", b"nouvelle"]


def test_la_suppression_ne_touche_que_les_categories_demandees(
    client: TestClient, media: Path
) -> None:
    film = media / "Film"
    video = _ecrire(film / "Film.mkv")
    affiche = _ecrire(film / "poster.jpg")
    fiche = _ecrire(film / "Film.nfo")
    sous_titre = _ecrire(film / "Film.fr.srt")
    somme = _ecrire(film / "Film.sfv")
    vignette = _ecrire(film / "Film.trickplay" / "320 - 10x10" / "0.jpg")
    notes = _ecrire(film / "notes.txt")

    corps = client.post(
        "/api/library/extras/prune",
        json={"categories": ["images", "sous_titres"], "mode": "delete", "confirm": True},
    ).json()

    assert corps["mode"] == "delete"
    assert corps["removed"] == 2
    assert corps["trash_path"] is None
    assert not affiche.exists() and not sous_titre.exists()
    assert video.is_file() and fiche.is_file() and somme.is_file()
    assert vignette.is_file() and notes.is_file()
    assert not (media / TRASH_DIRNAME).exists()


def test_les_dossiers_devenus_vides_partent_mais_jamais_la_racine(
    client: TestClient, media: Path
) -> None:
    _ecrire(media / "poster.jpg")
    _ecrire(media / "Orphelin" / "poster.jpg")
    _ecrire(media / "Serie" / "poster.jpg")
    _ecrire(media / "Serie" / "Saison 1" / "fanart.jpg")
    _ecrire(media / "Vivant" / "Vivant.mkv")
    _ecrire(media / "Vivant" / "poster.jpg")

    corps = client.post(
        "/api/library/extras/prune",
        json={"categories": ["images"], "mode": "delete", "confirm": True},
    ).json()

    assert corps["removed"] == 5
    assert media.is_dir()
    assert not (media / "Orphelin").exists()
    assert not (media / "Serie").exists()
    assert (media / "Vivant" / "Vivant.mkv").is_file()
    assert corps["removed_dirs"] == 3


def test_une_racine_de_destination_n_est_jamais_supprimee(
    client: TestClient, media: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """« Films/ » vide de ses dernieres affiches reste : Jellyfin le surveille."""
    store = PreferenceStore(path=tmp_path / "prefs.json", library_root=media, source_roots=[])
    prefs = store.load()
    prefs.oversize.enabled = True
    monkeypatch.setattr(library, "get_store", lambda: store)
    _ecrire(media / "Films" / "poster.jpg")
    _ecrire(media / "Series" / "Serie" / "Saison 1" / "poster.jpg")
    _ecrire(media / "Animes" / "fanart.jpg")
    _ecrire(media / "Livres" / "Auteur" / "cover.jpg")
    _ecrire(media / "Films 4K" / "Film" / "poster.jpg")
    _ecrire(media / "Autre" / "poster.jpg")

    corps = client.post(
        "/api/library/extras/prune",
        json={"categories": ["images"], "mode": "delete", "confirm": True},
    ).json()

    assert corps["removed"] == 6 and corps["failed_count"] == 0
    for destination in ("Films", "Series", "Animes", "Livres", "Films 4K"):
        assert (media / destination).is_dir(), destination
    assert not (media / "Series" / "Serie").exists()
    assert not (media / "Livres" / "Auteur").exists()
    assert not (media / "Films 4K" / "Film").exists()
    assert not (media / "Autre").exists()
    assert corps["removed_dirs"] == 5


def test_un_lien_vers_l_exterieur_survit_au_nettoyage(
    client: TestClient, media: Path, tmp_path: Path
) -> None:
    dehors = _ecrire(tmp_path / "ailleurs" / "precieux.jpg", b"precieux")
    lien = media / "Film" / "lien.jpg"
    lien.parent.mkdir()
    lien.symlink_to(dehors)

    corps = client.post("/api/library/extras/prune", json=TOUT).json()

    assert corps["removed"] == 0
    assert lien.is_symlink()
    assert dehors.read_bytes() == b"precieux"


# --- L'annulation ne casse pas -------------------------------------------------


def test_annuler_un_rangement_dont_les_annexes_ont_ete_retirees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fiche et affiche deposees, sous-titre et jaquette emportes : tous retires
    par le nettoyage, puis le rangement est annule. Aucun echec ne doit remonter,
    et le journal doit se vider — sinon chaque annulation echouerait a jamais."""
    monkeypatch.setattr(nfo, "_telecharger_image", lambda url, http: JPEG)
    source = _ecrire(tmp_path / "dl" / "dune.mkv", b"video")
    sous_titre = _ecrire(tmp_path / "dl" / "dune.fr.srt", b"srt")
    jaquette = _ecrire(tmp_path / "dl" / "dune-poster.png", b"png")
    media = tmp_path / "media"
    destination = media / "Dune (2021)" / "Dune (2021).mkv"
    journal = Journal(tmp_path / "journal.jsonl")
    plan = Plan(
        id="p-dune",
        source=source,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        title="Dune",
        year=2021,
        provider="tmdb",
        external_id="438631",
        poster_url="https://image.tmdb.org/t/p/w185/p.jpg",
        values={"title": "Dune", "year": 2021},
        companions=[
            (sous_titre, destination.with_name("Dune (2021).fr.srt")),
            (jaquette, destination.with_name("Dune (2021)-poster.png")),
        ],
    )

    resultat = apply_plan(
        plan,
        journal,
        dry_run=False,
        trash_root=media / TRASH_DIRNAME,
        local_metadata=LocalMetadataSettings(nfo=True, artwork=True),
    )
    natures = [r.kind for r in journal.read_all()]
    assert resultat.ok
    assert natures.count("metadata") == 2 and natures.count("companion") == 2

    nettoyage = library._prune_extras(media, {"images", "fiches", "sous_titres"}, "delete")
    assert nettoyage["removed"] == 4 and nettoyage["failed_count"] == 0

    resultats = undo_plans(journal, [plan.id])

    assert all(r.ok for r in resultats), [r.message for r in resultats if not r.ok]
    assert source.read_bytes() == b"video"
    assert journal.read_all() == []
