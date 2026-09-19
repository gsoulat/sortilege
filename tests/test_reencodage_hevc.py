"""Reencodage en HEVC 10 bits : ce que devient chaque source, et ce qui est refuse.

Le cas qui a impose ce format : des films 4K HDR de 21 Go reencodes en libx264
CRF 21 sont sortis en H.264 10 bits, que ni la television, ni l'iPad, ni
l'iPhone, ni Firefox ne decodent par le materiel — Jellyfin convertissait tout
a la volee, et le HDR etait perdu. Tous ces appareils lisent le HEVC 10 bits.

Ce qui est verifie ici, sans film ni ffmpeg — les analyses sont des sorties
ffprobe simulees, passees par la vraie lecture de ``lecture.lire_sonde`` :

- l'argv de chaque cas (SDR 8 et 10 bits, HDR10 avec ou sans metadonnees,
  HLG, HDR10+, Dolby Vision 7, 8.1 et 8.4) ;
- le refus du Dolby Vision profil 5, AVANT de commencer, et sa memoire : il
  ne doit pas revenir chaque soir parmi les candidats ;
- le plafond de debit pour le wifi, le mode budget, l'option audio ;
- ce qui doit etre vrai de TOUTE commande : 10 bits, etiquette hvc1, et
  jamais la source en sortie ;
- la migration du reglage CRF ecrit du temps du H.264.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps
from sortilege.api import transcode as api
from sortilege.core import lecture, transcode
from sortilege.core.preferences import (
    Preferences,
    PreferenceStore,
    TranscodeSettings,
    lire_reencodage,
)
from sortilege.core.probe import FileProbe
from sortilege.core.reencode import Candidate
from sortilege.core.transcode import (
    DESENTRELACEMENT,
    MOTIF_ANALYSE_INCOMPLETE,
    MOTIF_DOLBY_VISION_5,
    MOTIF_SANS_LIBX265,
    ImageJointe,
    PlanEncodage,
    Preparation,
    State,
    build_command,
    ligne_d_erreur,
    lire_encodeurs,
    lire_jointes,
    planifier,
)

TOUS = frozenset({"libx264", "libx265", "eac3", "aac"})

MASTERING = {
    "side_data_type": "Mastering display metadata",
    "red_x": "34000/50000",
    "red_y": "16000/50000",
    "green_x": "13250/50000",
    "green_y": "34500/50000",
    "blue_x": "7500/50000",
    "blue_y": "3000/50000",
    "white_point_x": "15635/50000",
    "white_point_y": "16450/50000",
    "min_luminance": "50/10000",
    "max_luminance": "10000000/10000",
}
MASTER_X265 = "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
LUMIERE = {
    "side_data_type": "Content light level metadata",
    "max_content": 1000,
    "max_average": 400,
}
HDR10PLUS = {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}


def dovi(profil: int, compatibilite: int) -> dict[str, Any]:
    return {
        "side_data_type": "DOVI configuration record",
        "dv_version_major": 1,
        "dv_profile": profil,
        "dv_level": 6,
        "rpu_present_flag": 1,
        "el_present_flag": int(profil == 7),
        "bl_present_flag": 1,
        "dv_bl_signal_compatibility_id": compatibilite,
    }


def piste(codec: str, canaux: int, profil: str = "", langue: str = "fre") -> dict[str, Any]:
    dispositions = {1: "mono", 2: "stereo", 6: "5.1(side)", 8: "7.1"}
    return {
        "codec_type": "audio",
        "codec_name": codec,
        "channels": canaux,
        "channel_layout": dispositions.get(canaux, ""),
        "profile": profil,
        "tags": {"language": langue},
    }


def analyse(
    *,
    codec: str = "hevc",
    profil: str = "Main 10",
    pix_fmt: str = "yuv420p10le",
    largeur: int = 3840,
    hauteur: int = 2160,
    primaires: str = "",
    transfert: str = "",
    matrice: str = "",
    image: tuple[dict[str, Any], ...] = (),
    flux: tuple[dict[str, Any], ...] = (),
    audio: tuple[dict[str, Any], ...] = (),
    sous_titres: tuple[dict[str, Any], ...] = (),
) -> lecture.Analyse:
    """Une analyse telle que ``lecture`` la tire de ffprobe (deux passes)."""
    sortie_flux = {
        "format": {"format_name": "matroska,webm", "duration": "7200.0"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "profile": profil,
                "pix_fmt": pix_fmt,
                "width": largeur,
                "height": hauteur,
                "side_data_list": list(flux),
            },
            *audio,
            *sous_titres,
        ],
    }
    sortie_image = {
        "frames": [
            {
                "pix_fmt": pix_fmt,
                "color_primaries": primaires,
                "color_transfer": transfert,
                "color_space": matrice,
                "side_data_list": list(image),
            }
        ]
    }
    return lecture.lire_sonde(sortie_flux, sortie_image, nom="Film.mkv")


def hdr10(*annexes: dict[str, Any], flux: tuple[dict[str, Any], ...] = ()) -> lecture.Analyse:
    return analyse(
        primaires="bt2020",
        transfert="smpte2084",
        matrice="bt2020nc",
        image=annexes,
        flux=flux,
    )


def commande(
    source: lecture.Analyse | None, hauteur: int = 1080, **options: Any
) -> tuple[list[str], PlanEncodage]:
    compresser = options.pop("compresser_audio", False)
    encodeurs = options.pop("encodeurs", TOUS)
    plan = planifier(source, encodeurs=encodeurs, compresser_audio=compresser)
    assert not plan.refus, plan.refus
    argv = build_command(
        Path("/films/Film.mkv"), Path("/sortie/abc.mkv"), hauteur, plan=plan, **options
    )
    return argv, plan


def valeur(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


def x265(argv: list[str]) -> list[str]:
    return valeur(argv, "-x265-params").split(":")


# --- SDR ----------------------------------------------------------------------


def test_une_source_sdr_8_bits_devient_du_hevc_10_bits_en_bt709() -> None:
    """Les 10 bits servent aussi en SDR : ils evitent les bandes dans les
    degrades (ciel, fondus au noir) qu'un 8 bits reencode fait apparaitre."""
    argv, plan = commande(
        analyse(
            codec="h264",
            profil="High",
            pix_fmt="yuv420p",
            largeur=1920,
            hauteur=1080,
            primaires="bt709",
            transfert="bt709",
            matrice="bt709",
        ),
        720,
    )

    assert valeur(argv, "-c:v") == "libx265"
    assert valeur(argv, "-pix_fmt") == "yuv420p10le"
    assert valeur(argv, "-profile:v") == "main10"
    assert valeur(argv, "-crf") == "21"
    assert valeur(argv, "-color_primaries") == "bt709"
    assert valeur(argv, "-color_trc") == "bt709"
    assert valeur(argv, "-colorspace") == "bt709"
    parametres = x265(argv)
    assert "colorprim=bt709" in parametres
    assert "hdr10=1" not in parametres, "pas de SEI HDR sur une source SDR"
    assert "-dolbyvision" not in argv
    assert plan.notes == []


def test_une_source_sdr_10_bits_reste_en_10_bits_mais_en_hevc() -> None:
    """LE cas reel : un H.264 High 10, illisible par le materiel de la maison.
    Il en sort un HEVC Main 10, que tous ses appareils decodent."""
    argv, _ = commande(
        analyse(
            codec="h264",
            profil="High 10",
            pix_fmt="yuv420p10le",
            largeur=1920,
            hauteur=1080,
            primaires="bt709",
            transfert="bt709",
            matrice="bt709",
        )
    )

    assert valeur(argv, "-c:v") == "libx265"
    assert valeur(argv, "-pix_fmt") == "yuv420p10le"
    assert valeur(argv, "-profile:v") == "main10"


def test_une_source_hd_non_etiquetee_est_declaree_bt709_des_l_entree() -> None:
    """Un 1080p sans etiquette est lu en BT.709 par tout lecteur. Le declarer
    en sortie SEULEMENT ferait convertir l'image par ffmpeg (qui suppose
    BT.601 en entree) : il est donc declare aussi en entree, par setparams."""
    argv, _ = commande(analyse(codec="h264", pix_fmt="yuv420p", largeur=1920, hauteur=1080), 720)

    assert valeur(argv, "-colorspace") == "bt709"
    assert valeur(argv, "-vf").startswith(
        f"{DESENTRELACEMENT},setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709,"
        "scale="
    )


def test_une_source_sd_non_etiquetee_n_est_pas_repeinte() -> None:
    """En definition standard, le lecteur supposera ce qu'il supposait deja."""
    argv, _ = commande(analyse(codec="mpeg4", pix_fmt="yuv420p", largeur=720, hauteur=576), 576)

    assert "-colorspace" not in argv
    assert "setparams" not in valeur(argv, "-vf")


def test_une_etiquette_sd_declaree_est_gardee() -> None:
    argv, _ = commande(
        analyse(
            codec="mpeg2video",
            pix_fmt="yuv420p",
            largeur=720,
            hauteur=576,
            primaires="bt470bg",
            transfert="smpte170m",
            matrice="bt470bg",
        ),
        576,
    )

    assert valeur(argv, "-color_primaries") == "bt470bg"
    assert valeur(argv, "-colorspace") == "bt470bg"


# --- HDR ----------------------------------------------------------------------


def test_hdr10_avec_mastering_et_maxcll() -> None:
    """Sans ces metadonnees, le televiseur ne sait pas doser la luminosite :
    l'image sort terne ou brulee. Elles sont recopiees de la source."""
    argv, plan = commande(hdr10(MASTERING, LUMIERE))

    assert valeur(argv, "-color_primaries") == "bt2020"
    assert valeur(argv, "-color_trc") == "smpte2084"
    assert valeur(argv, "-colorspace") == "bt2020nc"
    assert valeur(argv, "-x265-params").startswith(
        "hdr10=1:repeat-headers=1:colorprim=bt2020:transfer=smpte2084:colormatrix=bt2020nc"
        f":master-display={MASTER_X265}:max-cll=1000,400"
    )
    assert valeur(argv, "-vf").startswith(
        f"{DESENTRELACEMENT},setparams=color_primaries=bt2020:color_trc=smpte2084"
        ":colorspace=bt2020nc,"
    ), "declare en entree : aucune conversion de couleurs"
    assert plan.notes == []


def test_hdr10_sans_metadonnees_n_en_invente_pas() -> None:
    argv, plan = commande(hdr10())

    parametres = x265(argv)
    assert "hdr10=1" in parametres
    assert "transfer=smpte2084" in parametres
    assert not any(p.startswith(("master-display=", "max-cll=")) for p in parametres)
    assert any("rien n'est inventé" in note for note in plan.notes)


def test_hdr10_avec_mastering_seul_ne_fabrique_pas_de_maxcll() -> None:
    argv, plan = commande(hdr10(MASTERING))

    parametres = x265(argv)
    assert f"master-display={MASTER_X265}" in parametres
    assert not any(p.startswith("max-cll=") for p in parametres)
    assert "MaxCLL" in plan.notes[0]


def test_hlg() -> None:
    argv, _ = commande(analyse(primaires="bt2020", transfert="arib-std-b67", matrice="bt2020nc"))

    assert valeur(argv, "-color_trc") == "arib-std-b67"
    assert "transfer=arib-std-b67" in x265(argv)
    assert "hdr10=1" in x265(argv)


def test_hdr10_plus_garde_sa_base_hdr10_et_le_dit() -> None:
    """ffmpeg ne recopie pas les metadonnees dynamiques : on garde la base
    HDR10, qui reste du HDR partout, et la note le dit avant qu'on remplace."""
    argv, plan = commande(hdr10(MASTERING, LUMIERE, HDR10PLUS))

    assert "transfer=smpte2084" in x265(argv)
    assert f"master-display={MASTER_X265}" in x265(argv)
    assert len(plan.notes) == 1
    assert "HDR10+" in plan.notes[0] and "ne sont pas conservées" in plan.notes[0]


# --- Dolby Vision ---------------------------------------------------------------


def test_dolby_vision_8_1_encode_sa_base_hdr10() -> None:
    argv, plan = commande(hdr10(MASTERING, LUMIERE, flux=(dovi(8, 1),)))

    assert valeur(argv, "-color_trc") == "smpte2084"
    assert f"master-display={MASTER_X265}" in x265(argv)
    assert valeur(argv, "-dolbyvision") == "0", (
        "ffmpeg ne doit pas recoller de metadonnees Dolby Vision perimees"
    )
    assert "Dolby Vision profil 8.1" in plan.notes[0]
    assert "couche de base HDR10" in plan.notes[0]


def test_dolby_vision_et_hdr10_plus_disent_les_deux_pertes() -> None:
    _argv, plan = commande(hdr10(MASTERING, LUMIERE, HDR10PLUS, flux=(dovi(8, 1),)))

    assert len(plan.notes) == 2
    assert "Dolby Vision profil 8.1" in plan.notes[0]
    assert "HDR10+" in plan.notes[1]


def test_dolby_vision_7_encode_sa_base_hdr10_sans_la_couche_d_amelioration() -> None:
    _argv, plan = commande(hdr10(MASTERING, flux=(dovi(7, 6),)))

    assert plan.couleurs == transcode.PQ
    assert "Dolby Vision profil 7" in plan.notes[0]
    assert "couche d'amélioration" in plan.notes[0]


def test_dolby_vision_8_4_encode_sa_base_hlg() -> None:
    argv, plan = commande(
        analyse(
            primaires="bt2020", transfert="arib-std-b67", matrice="bt2020nc", flux=(dovi(8, 4),)
        )
    )

    assert "transfer=arib-std-b67" in x265(argv)
    assert "couche de base HLG" in plan.notes[0]


def test_dolby_vision_5_est_refuse_definitivement() -> None:
    plan = planifier(hdr10(flux=(dovi(5, 0),)), encodeurs=TOUS)

    assert plan.refus == MOTIF_DOLBY_VISION_5
    assert plan.definitif


def test_un_dolby_vision_de_profil_inconnu_est_refuse() -> None:
    """Des donnees RPU sans enregistrement de configuration : on ne sait pas
    ce que voit un appareil sans Dolby Vision. Ne pas deviner."""
    plan = planifier(hdr10({"side_data_type": "Dolby Vision RPU Data"}), encodeurs=TOUS)

    assert "profil inconnu" in plan.refus
    assert plan.definitif


# --- Refus avant de commencer ---------------------------------------------------


@pytest.fixture
def sans_processus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Un ffmpeg « present », mais qu'aucun refus ne doit lancer."""

    def interdit(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ffmpeg lance malgre le refus")

    monkeypatch.setattr(transcode.shutil, "which", lambda nom: f"/faux/{nom}")
    monkeypatch.setattr(transcode.subprocess, "Popen", interdit)


def un_travail(racine: Path, nom: str = "Film.mkv") -> transcode.Job:
    source = racine / "media" / "Films" / nom
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"x" * 100)
    return transcode.new_job(source, f"Films/{nom}", "Film", "1080p", 21 * 1024**3)


def test_dolby_vision_5_ne_lance_rien(tmp_path: Path, sans_processus: None) -> None:
    job = un_travail(tmp_path)
    preparation = Preparation(analyse=hdr10(flux=(dovi(5, 0),)), encodeurs=TOUS)

    transcode.run(job, tmp_path / "media", preparation=preparation)

    assert job.state is State.FAILED
    assert job.error == MOTIF_DOLBY_VISION_5
    assert job.definitif
    assert job.output is None, "aucun fichier de sortie n'a ete prevu"
    assert not job.started_at


def test_sans_libx265_le_travail_est_refuse(tmp_path: Path, sans_processus: None) -> None:
    job = un_travail(tmp_path)
    preparation = Preparation(analyse=hdr10(), encodeurs=frozenset({"libx264", "aac"}))

    transcode.run(job, tmp_path / "media", preparation=preparation)

    assert job.state is State.FAILED
    assert job.error == MOTIF_SANS_LIBX265
    assert not job.definitif, "un ffmpeg se remplace : le fichier reste candidat"


def test_une_source_illisible_n_est_pas_encodee_a_l_aveugle(
    tmp_path: Path, sans_processus: None
) -> None:
    """Sans analyse, impossible de savoir si c'est un Dolby Vision profil 5."""
    job = un_travail(tmp_path)
    illisible = lecture.Analyse(motif_illisible="ffprobe ne parvient pas à lire ce fichier")

    transcode.run(job, tmp_path / "media", preparation=Preparation(analyse=illisible))

    assert job.state is State.FAILED
    assert "ffprobe ne parvient pas" in job.error
    assert not job.definitif


SORTIE_ENCODEURS = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = Frame-level multithreading
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC (codec h264)
 V....D libx265              libx265 H.265 / HEVC (codec hevc)
 A....D eac3                 ATSC A/52 E-AC-3
 S..... srt                  SubRip subtitle
"""


def test_la_liste_des_encodeurs_est_lue() -> None:
    assert lire_encodeurs(SORTIE_ENCODEURS) == frozenset({"libx264", "libx265", "eac3", "srt"})


def test_une_sortie_inconnue_ne_prouve_pas_l_absence_de_libx265() -> None:
    """Refuser sur une sortie qu'on ne comprend pas bloquerait toute la file
    pour une supposition : ffmpeg dira lui-meme ce qui lui manque."""
    assert lire_encodeurs("out_time_us=1000000\n") is None
    assert planifier(hdr10(), encodeurs=None).refus == ""


# --- Memoire des refus definitifs ---------------------------------------------


@pytest.fixture
def file_isolee(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """File vide, pause levee, dossier de donnees et bibliotheque a ce test."""
    racine = tmp_path / "media"
    racine.mkdir()
    monkeypatch.setattr(deps, "DATA_DIR", tmp_path / "donnees")
    monkeypatch.setattr(transcode, "_controle", transcode._Controle())
    monkeypatch.setattr(api, "_pause_relue", False)
    monkeypatch.setattr(api, "_perdu", "")
    monkeypatch.setattr(api, "_jobs", {})
    monkeypatch.setattr(api, "_worker", None)
    monkeypatch.setattr(api, "_stop", threading.Event())
    monkeypatch.setattr(api, "_reveil", threading.Event())
    monkeypatch.setattr(api, "get_settings", lambda: SimpleNamespace(library_root=racine))
    reglages = Preferences(transcode=TranscodeSettings(enabled=True, start_hour=0, end_hour=0))
    monkeypatch.setattr(api, "get_store", lambda: SimpleNamespace(load=lambda: reglages))
    monkeypatch.setattr(api.collection, "current_works", list)
    # Ces tests appellent la boucle eux-memes. Le vrai fil de fond, que la
    # mise en file demarre, traitait « Autre.mkv » avec l'analyse truquee du
    # test (un Dolby Vision 5) et retenait son refus : selon le minutage, ce
    # refus retombait sur un test voisin, qui echouait une fois sur quatre.
    monkeypatch.setattr(api, "_ensure_worker", lambda: None)
    yield tmp_path
    # La mise en file demarre le fil de reencodage. Laisse vivant, il
    # survivait au test et ecrivait, dans le dossier de donnees du test
    # SUIVANT, le refus d'un « Films/Autre.mkv » de meme taille et de meme
    # seconde : ce test-la echouait alors une fois sur quatre, selon le
    # minutage. On l'arrete et on l'attend avant de rendre la main.
    api._stop.set()
    api._reveil.set()
    fil = api._worker
    if fil is not None:
        fil.join(timeout=5)
        assert not fil.is_alive(), "le fil de reencodage survit au test"


def candidat(chemin: str) -> Candidate:
    return Candidate(
        relative_path=chemin,
        title="Film",
        kind="movie",
        resolution="2160p",
        target="1080p",
        codec="hevc",
        size_bytes=21 * 1024**3,
        estimated_bytes=7 * 1024**3,
        strategy_label="Équilibré",
    )


def test_un_dolby_vision_5_n_est_plus_propose_en_boucle(
    file_isolee: Path, sans_processus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuse une nuit, il ne doit pas revenir le soir suivant parmi les
    candidats — ni repartir par « tout mettre en file »."""
    job = un_travail(file_isolee)
    autre = un_travail(file_isolee, "Autre.mkv")
    monkeypatch.setattr(
        api.reencode,
        "audit",
        lambda _oeuvres, _reglage: [candidat(job.relative_path), candidat(autre.relative_path)],
    )
    monkeypatch.setattr(
        transcode,
        "preparer",
        lambda _source: Preparation(analyse=hdr10(flux=(dovi(5, 0),)), encodeurs=TOUS),
    )
    api._jobs[job.id] = job

    api._boucle()  # refuse, retient, puis rend la main : la file est vide

    assert job.state is State.FAILED
    assert set(api._candidates()) == {autre.relative_path}
    reponse = api.enqueue(api.QueueRequest(paths=[job.relative_path]))
    assert reponse["queued"] == 0
    assert reponse["rejected"] == [{"path": job.relative_path, "message": MOTIF_DOLBY_VISION_5}]
    assert api.enqueue(api.QueueRequest(all=True))["queued"] == 1, "seul l'autre part"


def test_un_fichier_remplace_redevient_candidat(file_isolee: Path) -> None:
    """Le refus vaut pour CE fichier : une autre version (un vrai HDR10) doit
    pouvoir etre reencodee."""
    job = un_travail(file_isolee)
    job.state, job.error, job.definitif = State.FAILED, MOTIF_DOLBY_VISION_5, True
    api._retenir_refus(job)
    assert job.relative_path in api._refus()

    job.source.write_bytes(b"autre version, autre taille")

    assert api._refus() == {}


def test_un_refus_passager_n_est_pas_retenu(file_isolee: Path) -> None:
    job = un_travail(file_isolee)
    job.state, job.error = State.FAILED, MOTIF_SANS_LIBX265

    api._retenir_refus(job)

    assert not (deps.DATA_DIR / transcode.FICHIER_REFUS).exists()


def test_un_etat_de_refus_illisible_ne_bloque_rien(tmp_path: Path) -> None:
    fichier = tmp_path / transcode.FICHIER_REFUS
    fichier.write_text("{pas du json", encoding="utf-8")

    assert transcode.lire_refus(fichier) == {}


# --- Debit plafonne, budget -------------------------------------------------------


@pytest.mark.parametrize(
    ("hauteur", "crf", "maximum", "tampon"),
    [
        (720, "21", "20000", "40000"),
        (1080, "21", "20000", "40000"),
        (2160, "22", "40000", "80000"),
    ],
)
def test_le_debit_est_plafonne_selon_la_definition(
    hauteur: int, crf: str, maximum: str, tampon: str
) -> None:
    """CRF « plafonne » : les pointes d'une scene de pluie ne doivent pas
    depasser ce que le wifi tient."""
    argv, _ = commande(hdr10(), hauteur)

    assert valeur(argv, "-crf") == crf
    assert f"vbv-maxrate={maximum}" in x265(argv)
    assert f"vbv-bufsize={tampon}" in x265(argv)


def test_le_crf_regle_decale_aussi_le_4k() -> None:
    argv, _ = commande(hdr10(), 2160, crf=19)

    assert valeur(argv, "-crf") == "20"


def test_le_mode_budget_vise_un_debit_et_borne_ses_pointes() -> None:
    argv, _ = commande(hdr10(MASTERING), 1080, bitrate_kbps=4000)

    parametres = x265(argv)
    assert "-crf" not in argv, "les deux modes s'excluent"
    assert "bitrate=4000" in parametres
    assert "vbv-maxrate=6000" in parametres
    assert "vbv-bufsize=8000" in parametres
    assert "vbv-maxrate=20000" not in parametres
    assert f"master-display={MASTER_X265}" in parametres, "le HDR tient aussi en mode budget"


def test_un_film_en_scope_ne_deborde_pas_de_la_boite_1080p() -> None:
    """3840x1600 ramene a « 1080 de haut » faisait 2592x1080 : un tiers de
    pixels de trop. Borne aussi en largeur, il sort en 1920x800."""
    filtre = transcode.filtre_echelle(1080)

    assert "min(iw,1920)" in filtre
    assert "min(ih,1080)" in filtre
    assert "force_original_aspect_ratio=decrease" in filtre


# --- Audio --------------------------------------------------------------------------


PISTES = (
    piste("truehd", 8),  # 0 : converti, replie en 5.1
    piste("dts", 6, "DTS-HD MA"),  # 1 : converti
    piste("dts", 6, "DTS"),  # 2 : converti
    piste("dts", 2, "DTS Express"),  # 3 : copie (plus leger que l'E-AC-3)
    piste("eac3", 6, "Dolby Digital Plus + Dolby Atmos"),  # 4 : copie, Atmos garde
    piste("ac3", 6),  # 5 : copie
    piste("aac", 2, "LC", "eng"),  # 6 : copie
    piste("pcm_s24le", 2),  # 7 : converti
    piste("flac", 2),  # 8 : copie (stereo, rien a gagner)
    piste("flac", 6),  # 9 : converti (multicanal)
)


def pistes_converties(argv: list[str]) -> list[int]:
    return [int(a.split(":")[-1]) for a in argv if a.startswith("-c:a:") and a != "-c:a"]


def test_par_defaut_l_audio_est_copie_a_l_identique() -> None:
    argv, plan = commande(analyse(audio=PISTES))

    assert valeur(argv, "-c:a") == "copy"
    assert pistes_converties(argv) == []
    assert plan.audio == []


def test_l_option_audio_convertit_les_pistes_sans_perte_et_copie_le_reste() -> None:
    argv, plan = commande(analyse(audio=PISTES), compresser_audio=True)

    assert valeur(argv, "-c:a") == "copy", "le reste est copie"
    assert pistes_converties(argv) == [0, 1, 2, 7, 9]
    for indice in (0, 1, 2, 7, 9):
        assert valeur(argv, f"-c:a:{indice}") == "eac3"
        assert valeur(argv, f"-b:a:{indice}") == "640k"
    assert valeur(argv, "-ac:a:0") == "6", "le 7.1 est replie en 5.1"
    assert "-ac:a:1" not in argv
    note = plan.notes[-1]
    assert "TrueHD 7.1" in note and "PCM stéréo" in note
    assert "Atmos" in note


def test_sans_encodeur_e_ac3_l_audio_reste_copie_et_c_est_dit() -> None:
    argv, plan = commande(
        analyse(audio=PISTES), compresser_audio=True, encodeurs=frozenset({"libx265"})
    )

    assert pistes_converties(argv) == []
    assert "E-AC-3" in plan.notes[-1]


def test_les_statistiques_perimees_sont_effacees_des_pistes_reencodees() -> None:
    """« BPS=60000000 » sur un fichier de 8 Mbit/s ferait croire a Jellyfin
    que le fichier ne passe pas en wifi."""
    argv, _ = commande(analyse(audio=PISTES), compresser_audio=True)

    assert "-metadata:s:v:0" in argv
    assert "BPS=" in argv and "BPS-eng=" in argv
    assert "-metadata:s:a:0" in argv
    assert "-metadata:s:a:5" not in argv, "une piste copiee garde ses statistiques justes"
    assert not any(a.startswith("-map_metadata") for a in argv), (
        "couperait la recopie des langues de toutes les pistes"
    )


# --- Ce qui vaut pour TOUTE commande ------------------------------------------------


PLANS = {
    "sans analyse": None,
    "sdr": analyse(codec="h264", pix_fmt="yuv420p", largeur=1920, hauteur=1080),
    "hdr10": hdr10(MASTERING, LUMIERE),
    "dolby vision 8.1": hdr10(MASTERING, flux=(dovi(8, 1),)),
    "audio": analyse(audio=PISTES),
}


@pytest.mark.parametrize("nom", list(PLANS))
@pytest.mark.parametrize("budget", [0, 3000])
def test_toujours_10_bits_hvc1_et_jamais_la_source_en_sortie(nom: str, budget: int) -> None:
    argv, _ = commande(PLANS[nom], 1080, bitrate_kbps=budget, compresser_audio=True)

    assert valeur(argv, "-pix_fmt") == "yuv420p10le"
    assert valeur(argv, "-tag:v") == "hvc1"
    assert valeur(argv, "-c:v") == "libx265"
    assert valeur(argv, "-map") == "0:V:0", "la video principale, jamais une couverture"
    assert valeur(argv, "-vf").startswith(DESENTRELACEMENT)
    assert valeur(argv, "-c:s") == "copy", "sous-titres copies"
    assert "0:t?" in argv, "pieces jointes (polices ASS) copiees"
    assert argv[-1] == "/sortie/abc.mkv"
    assert argv.count("/films/Film.mkv") == 1
    assert valeur(argv, "-i") == "/films/Film.mkv"


def test_la_sortie_ne_peut_pas_etre_la_source(tmp_path: Path) -> None:
    """``-y`` ecraserait l'original : aucune combinaison ne doit y mener."""
    source = tmp_path / "Film.mkv"
    with pytest.raises(ValueError, match="sa source"):
        build_command(source, tmp_path / "." / "Film.mkv", 1080)


def test_la_sortie_est_a_cote_jamais_a_la_place(tmp_path: Path) -> None:
    job = un_travail(tmp_path)

    sortie = transcode.output_path(job, tmp_path / "media")

    assert sortie != job.source
    assert sortie.parent == transcode.staging_root(tmp_path / "media")
    assert sortie.suffix == ".mkv"


# --- Sources entrelacees --------------------------------------------------------------


def test_toute_source_est_desentrelacee_en_tete_de_chaine() -> None:
    """x265 n'encode que du progressif : une source 1080i gardait ses
    « peignes » sur chaque mouvement. ``deint=interlaced`` ne touche que les
    images marquees entrelacees — une source progressive ressort identique —
    et la sortie est etiquetee progressive."""
    for source, hauteur in ((PLANS["sdr"], 720), (PLANS["hdr10"], 1080), (None, 0)):
        argv, _ = commande(source, hauteur)

        filtres = valeur(argv, "-vf").split(",")
        assert filtres[:2] == ["bwdif=mode=send_frame:deint=interlaced", "setfield=prog"]


# --- Analyse partielle ------------------------------------------------------------------


def sans_premiere_image(**video: Any) -> lecture.Analyse:
    """ffprobe a lu le conteneur, pas la premiere image (echec ou delai depasse)."""
    flux = {
        "format": {"format_name": "matroska,webm", "duration": "7200.0"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "profile": "Main 10",
                "pix_fmt": "yuv420p10le",
                "width": 3840,
                "height": 2160,
                **video,
            }
        ],
    }
    return lecture.lire_sonde(flux, None, nom="Film.mkv")


@pytest.mark.parametrize(
    "video",
    [
        pytest.param({"color_space": "bt2020nc"}, id="matrice bt2020 seule (MKV ecrit par ffmpeg)"),
        pytest.param({}, id="10 bits sans etiquette"),
        pytest.param(
            {"codec_name": "h264", "profile": "High", "pix_fmt": "yuv420p"},
            id="8 bits, transfert inconnu",
        ),
    ],
)
def test_une_analyse_partielle_qui_laisse_place_au_hdr_est_reportee(video: dict) -> None:
    """Dans un MKV ecrit par ffmpeg, la courbe PQ et les metadonnees HDR10 ne
    vivent souvent que dans la premiere image : sans elle, un HDR passe pour du
    SDR, et l'encodage l'aplatirait sans un mot."""
    plan = planifier(sans_premiere_image(**video), encodeurs=TOUS)

    assert plan.refus == MOTIF_ANALYSE_INCOMPLETE
    assert not plan.definitif, "la lecture de la premiere image peut reussir la nuit suivante"


def test_un_conteneur_qui_exclut_le_hdr_suffit() -> None:
    """Transfert declare, couleurs BT.709, 8 bits : aucun HDR possible."""
    source = sans_premiere_image(
        codec_name="h264",
        profile="High",
        pix_fmt="yuv420p",
        color_primaries="bt709",
        color_transfer="bt709",
        color_space="bt709",
    )

    assert planifier(source, encodeurs=TOUS).refus == ""


def test_un_dolby_vision_5_reste_refuse_pour_de_bon_meme_sans_premiere_image() -> None:
    """L'enregistrement Dolby Vision vit dans le conteneur : le refus definitif
    passe avant le report."""
    source = sans_premiere_image(color_space="bt2020nc", side_data_list=[dovi(5, 0)])

    plan = planifier(source, encodeurs=TOUS)

    assert plan.refus == MOTIF_DOLBY_VISION_5
    assert plan.definitif


def test_le_report_n_est_pas_retenu_comme_un_refus(file_isolee: Path, sans_processus: None) -> None:
    """Retenu, il ferait disparaitre le fichier des candidats pour toujours,
    pour une lecture ratee une fois."""
    job = un_travail(file_isolee)
    partielle = Preparation(analyse=sans_premiere_image(color_space="bt2020nc"), encodeurs=TOUS)

    transcode.run(job, file_isolee / "media", preparation=partielle)
    api._retenir_refus(job)

    assert job.state is State.FAILED
    assert job.error == MOTIF_ANALYSE_INCOMPLETE
    assert not job.definitif
    assert not (deps.DATA_DIR / transcode.FICHIER_REFUS).exists()


# --- Sans preparation ---------------------------------------------------------------------


def test_run_sans_preparation_analyse_la_source_lui_meme(
    tmp_path: Path, sans_processus: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sans preparation, l'encodage partait sans verification : un Dolby Vision
    profil 5 sortait violet et vert."""
    preparees: list[Path] = []

    def preparer(source: Path) -> Preparation:
        preparees.append(source)
        return Preparation(analyse=hdr10(flux=(dovi(5, 0),)), encodeurs=TOUS)

    monkeypatch.setattr(transcode, "preparer", preparer)
    job = un_travail(tmp_path)

    transcode.run(job, tmp_path / "media")

    assert preparees == [job.source]
    assert job.error == MOTIF_DOLBY_VISION_5
    assert job.definitif


# --- Sous-titres et couvertures --------------------------------------------------------


def sous_titre(codec: str, langue: str = "fre") -> dict[str, Any]:
    return {"codec_type": "subtitle", "codec_name": codec, "tags": {"language": langue}}


def test_les_sous_titres_mov_text_sont_convertis_en_srt_les_autres_copies() -> None:
    """Le Matroska refuse le mov_text du MP4 : copie tel quel, il faisait
    echouer chaque nuit le reencodage de tout MP4 sous-titre."""
    source = analyse(
        sous_titres=(sous_titre("subrip"), sous_titre("mov_text"), sous_titre("mov_text", "eng"))
    )

    argv, plan = commande(source)

    assert valeur(argv, "-c:s") == "copy"
    assert "-c:s:0" not in argv
    assert valeur(argv, "-c:s:1") == "srt"
    assert valeur(argv, "-c:s:2") == "srt"
    assert argv.index("-c:s") < argv.index("-c:s:1"), "la regle d'abord, l'exception ensuite"
    assert any("mov_text" in note and "SRT" in note for note in plan.notes)


SONDE_JOINTES: dict[str, Any] = {
    "streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {}},
        {
            "index": 1,
            "codec_type": "video",
            "codec_name": "mjpeg",
            "disposition": {"attached_pic": 1},
            "tags": {"filename": "cover.jpg", "mimetype": "image/jpeg"},
        },
        {
            "index": 2,
            "codec_type": "attachment",
            "codec_name": "ttf",
            "tags": {"filename": "Verdana.ttf", "mimetype": "application/x-truetype-font"},
        },
        # Couverture d'un MP4 : ni nom ni type declares.
        {
            "index": 3,
            "codec_type": "video",
            "codec_name": "png",
            "disposition": {"attached_pic": 1},
        },
        {
            "index": 4,
            "codec_type": "video",
            "codec_name": "vc1",
            "disposition": {"attached_pic": 1},
        },
    ]
}


def test_les_couvertures_sont_reperees_avec_leur_nom_et_leur_type() -> None:
    images, pieces_jointes = lire_jointes(SONDE_JOINTES)

    assert pieces_jointes == 1
    assert images == (
        ImageJointe(rang=1, nom="cover.jpg", type_mime="image/jpeg", extension=".jpg"),
        ImageJointe(rang=2, nom="cover.png", type_mime="image/png", extension=".png"),
    ), "rang parmi les pistes video ; une image de type inconnu est laissee de cote"


def test_seule_la_video_principale_est_encodee_et_les_couvertures_sont_rejointes() -> None:
    """ffmpeg presente la couverture d'un MKV comme une piste video : copiee
    comme piste, elle devenait un second film MJPEG d'une image. Elle est
    rejointe comme piece jointe, avec son nom et son type."""
    images, pieces_jointes = lire_jointes(SONDE_JOINTES)
    extraites = [
        (image, Path(f"/sortie/abc.couverture-{i}{image.extension}"))
        for i, image in enumerate(images)
    ]

    argv = build_command(
        Path("/films/Film.mkv"),
        Path("/sortie/abc.mkv"),
        1080,
        plan=planifier(analyse(), encodeurs=TOUS),
        images=extraites,
        pieces_jointes=pieces_jointes,
    )

    assert options(argv, "-map") == ["0:V:0", "0:a?", "0:s?", "0:t?"]
    assert options(argv, "-attach") == [
        "/sortie/abc.couverture-0.jpg",
        "/sortie/abc.couverture-1.png",
    ]
    assert options(argv, "-metadata:s:t:1") == ["mimetype=image/jpeg", "filename=cover.jpg"]
    assert options(argv, "-metadata:s:t:2") == ["mimetype=image/png", "filename=cover.png"]
    assert argv[-1] == "/sortie/abc.mkv"


def options(argv: list[str], option: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == option]


# --- Ce que ffmpeg ecrit sur sa sortie d'erreur ----------------------------------------


CASCADE_MOV_TEXT = """\
[matroska @ 0x7c7d010500] Subtitle codec mov_text (94213) is not supported.
[out#0/matroska @ 0x7c7d05c300] Could not write header (incorrect codec parameters ?): \
Function not implemented
[vf#0:0 @ 0x7c7d0b8000] Error sending frames to consumers: Function not implemented
[vf#0:0 @ 0x7c7d0b8000] Task finished with error code: -78 (Function not implemented)
[vf#0:0 @ 0x7c7d0b8000] Terminating thread with return code -78 (Function not implemented)
[out#0/matroska @ 0x7c7d05c300] Nothing was written into output file, because at least one of \
its streams received no packets.
"""
"""Sortie reelle de ffmpeg 9 sur un MP4 sous-titre en mov_text."""


def test_la_vraie_cause_d_un_echec_est_remontee() -> None:
    """« Nothing was written… » cachait la vraie raison, cinq lignes plus haut."""
    assert ligne_d_erreur(CASCADE_MOV_TEXT) == (
        "[matroska] Subtitle codec mov_text (94213) is not supported."
    )


def test_faute_de_cause_la_derniere_ligne_vaut_mieux_que_rien() -> None:
    assert ligne_d_erreur("Conversion failed!\n") == "Conversion failed!"
    assert ligne_d_erreur("\n  \n") == ""


FAUX_FFMPEG = """#!{python}
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["FAUX_FFMPEG_TRACE"], "a", encoding="utf-8") as trace:
    trace.write(json.dumps(argv) + "\\n")
if "image2" in argv:
    if os.environ.get("FAUX_FFMPEG_IMAGE_KO"):
        sys.stderr.write("[mjpeg @ 0x1] Invalid data found when processing input\\n")
        sys.exit(1)
    with open(argv[-1], "wb") as image:
        image.write(b"JPEG")
    sys.exit(0)
for _ in range(int(os.environ.get("FAUX_FFMPEG_BRUIT", "0"))):
    sys.stderr.write("[h264 @ 0x7f0a1c0] error while decoding MB 4 8, bytestream -5\\n")
sys.stderr.write(os.environ.get("FAUX_FFMPEG_FIN", ""))
sys.stderr.flush()
print("out_time_us=1000000", flush=True)
sys.exit(int(os.environ.get("FAUX_FFMPEG_CODE", "0")))
"""
"""Un ffmpeg qui ecrit ce qu'on lui demande sur sa sortie d'erreur, AVANT sa
progression — l'ordre d'une source abimee, ou le blocage se produisait."""


@pytest.fixture
def vrai_processus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``run`` lance le faux ffmpeg ci-dessus, un vrai processus. Rend la trace
    des argv recus, un appel par ligne."""
    if " " in sys.executable:
        pytest.skip("chemin de l'interpreteur inutilisable dans un shebang")
    script = tmp_path / "ffmpeg"
    script.write_text(FAUX_FFMPEG.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    trace = tmp_path / "trace.jsonl"
    monkeypatch.setenv("FAUX_FFMPEG_TRACE", str(trace))
    monkeypatch.setattr(transcode.shutil, "which", lambda _nom: str(script))
    monkeypatch.setattr(transcode, "probe_media", lambda _chemin: FileProbe(duration_seconds=2.0))
    monkeypatch.setattr(transcode, "_controle", transcode._Controle())
    lances: list[subprocess.Popen] = []

    class PopenNote(subprocess.Popen):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            lances.append(self)

    monkeypatch.setattr(transcode.subprocess, "Popen", PopenNote)
    yield trace
    # Un ffmpeg bloque sur sa sortie d'erreur survivrait a la suite.
    for processus in lances:
        if processus.poll() is None:
            processus.kill()
        processus.wait(5)


SDR = Preparation(
    analyse=analyse(
        codec="h264",
        pix_fmt="yuv420p",
        largeur=1920,
        hauteur=1080,
        primaires="bt709",
        transfert="bt709",
        matrice="bt709",
    ),
    encodeurs=TOUS,
)


def encoder(job: transcode.Job, racine: Path, preparation: Preparation = SDR) -> None:
    """``run`` dans un fil, borne dans le temps : un blocage echoue au lieu de
    suspendre la suite."""
    fil = threading.Thread(
        target=transcode.run,
        args=(job, racine),
        kwargs={"preparation": preparation},
        daemon=True,
    )
    fil.start()
    fil.join(30)
    assert not fil.is_alive(), "run bloque : ffmpeg attend qu'on lise sa sortie d'erreur"


def appels(trace: Path) -> list[list[str]]:
    return [json.loads(ligne) for ligne in trace.read_text(encoding="utf-8").splitlines()]


def test_une_source_abimee_ne_bloque_plus_la_file(
    tmp_path: Path, vrai_processus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pres de 2 Mo de messages d'erreur, trente fois ce que tient un tube :
    ffmpeg s'y endormait, le travail restait « en cours » et la file entiere
    attendait un redemarrage."""
    monkeypatch.setenv("FAUX_FFMPEG_BRUIT", "30000")
    job = un_travail(tmp_path)

    encoder(job, tmp_path / "media")

    assert job.state is State.DONE, job.error
    assert job.progress == 1.0
    reste = sorted(p.name for p in transcode.staging_root(tmp_path / "media").iterdir())
    assert reste == [], "le journal de ffmpeg est efface"


def test_un_echec_bavard_remonte_sa_vraie_cause(
    tmp_path: Path, vrai_processus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAUX_FFMPEG_BRUIT", "30000")
    monkeypatch.setenv("FAUX_FFMPEG_FIN", CASCADE_MOV_TEXT)
    monkeypatch.setenv("FAUX_FFMPEG_CODE", "1")
    job = un_travail(tmp_path)

    encoder(job, tmp_path / "media")

    assert job.state is State.FAILED
    assert job.error == "[matroska] Subtitle codec mov_text (94213) is not supported."
    assert list(transcode.staging_root(tmp_path / "media").iterdir()) == [], (
        "ni fichier partiel, ni journal"
    )


def test_la_couverture_est_extraite_rejointe_puis_effacee(
    tmp_path: Path, vrai_processus: Path
) -> None:
    job = un_travail(tmp_path)
    couverture = ImageJointe(rang=1, nom="cover.jpg", type_mime="image/jpeg", extension=".jpg")
    preparation = Preparation(analyse=SDR.analyse, images=(couverture,), pieces_jointes=1)

    encoder(job, tmp_path / "media", preparation)

    extraction, encodage = appels(vrai_processus)
    assert extraction[extraction.index("-map") + 1] == "0:v:1"
    extraite = options(encodage, "-attach")
    assert len(extraite) == 1 and extraite[0].endswith(".couverture-0.jpg")
    assert options(encodage, "-metadata:s:t:1") == ["mimetype=image/jpeg", "filename=cover.jpg"]
    assert job.state is State.DONE
    assert not Path(extraite[0]).exists(), "l'image extraite ne traine pas dans la bibliotheque"


def test_une_couverture_illisible_ne_coute_qu_elle(
    tmp_path: Path, vrai_processus: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAUX_FFMPEG_IMAGE_KO", "1")
    job = un_travail(tmp_path)
    couverture = ImageJointe(rang=1, nom="cover.jpg", type_mime="image/jpeg", extension=".jpg")

    encoder(job, tmp_path / "media", Preparation(analyse=SDR.analyse, images=(couverture,)))

    _extraction, encodage = appels(vrai_processus)
    assert "-attach" not in encodage
    assert job.state is State.DONE
    assert any("cover.jpg" in note and "non conservée" in note for note in job.notes)


# --- Reglages et migration ----------------------------------------------------------


ANCIEN = {
    "enabled": True,
    "start_hour": 23,
    "end_hour": 7,
    "codec": "libx264",
    "crf": 21,
    "preset": "medium",
}


def test_un_ancien_crf_21_en_libx264_reste_21() -> None:
    """Pas de « conversion » d'echelle : x264 21 -> x265 26 degraderait
    l'image. A nombre egal, x265 fait au moins aussi bien, et plus petit."""
    reglages = lire_reencodage(ANCIEN)

    assert reglages.crf == 21
    assert reglages.enabled is True
    assert reglages.compress_audio is False
    assert not hasattr(reglages, "codec")


@pytest.mark.parametrize(("ancien", "attendu"), [(14, 16), (18, 18), (26, 26), (30, 28)])
def test_les_extremes_du_h264_sont_ramenes_dans_les_bornes(ancien: int, attendu: int) -> None:
    assert lire_reencodage({**ANCIEN, "crf": ancien}).crf == attendu


def test_un_choix_hevc_anterieur_est_garde() -> None:
    assert lire_reencodage({**ANCIEN, "codec": "libx265", "crf": 24}).crf == 24


def test_un_reglage_actuel_n_est_pas_touche() -> None:
    reglages = lire_reencodage({"crf": 23, "preset": "slow", "compress_audio": True})

    assert (reglages.crf, reglages.preset, reglages.compress_audio) == (23, "slow", True)


def test_un_reglage_edite_a_la_main_est_borne() -> None:
    reglages = lire_reencodage({"crf": 51, "preset": "placebo"})

    assert (reglages.crf, reglages.preset) == (28, "medium")


def test_des_heures_hors_du_cadran_sont_ramenees_dans_la_journee() -> None:
    """Une heure a 99 ou a -3 n'arrive jamais : le reencodage n'avait tout
    simplement jamais lieu. Bornees comme le fait l'API."""
    reglages = lire_reencodage({"start_hour": 99, "end_hour": -3})

    assert (reglages.start_hour, reglages.end_hour) == (23, 0)
    juste = lire_reencodage({"start_hour": 22, "end_hour": 6})
    assert (juste.start_hour, juste.end_hour) == (22, 6), "une plage correcte n'est pas touchee"


def test_le_fichier_de_reglages_est_migre_puis_reecrit_sans_codec(tmp_path: Path) -> None:
    chemin = tmp_path / "donnees" / "preferences.json"
    chemin.parent.mkdir()
    chemin.write_text(json.dumps({"transcode": ANCIEN}), encoding="utf-8")
    (tmp_path / "media").mkdir()
    store = PreferenceStore(path=chemin, library_root=tmp_path / "media", source_roots=[])

    prefs = store.load()
    assert prefs.transcode.crf == 21
    store.save(prefs)

    ecrit = json.loads(chemin.read_text(encoding="utf-8"))["transcode"]
    assert "codec" not in ecrit
    assert ecrit["crf"] == 21
    assert ecrit["compress_audio"] is False


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from sortilege.main import app

    monkeypatch.setattr(deps, "DATA_DIR", tmp_path)
    (tmp_path / "preferences.json").write_text(json.dumps({"transcode": ANCIEN}), encoding="utf-8")
    deps.get_store.cache_clear()
    try:
        with TestClient(app) as c:
            connexion = c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"})
            assert connexion.status_code == 200
            yield c
    finally:
        deps.get_store.cache_clear()


def test_les_reglages_http_disent_hevc_et_bornent_le_crf(client: TestClient) -> None:
    bloc = client.get("/api/settings/preferences").json()["transcode"]
    assert bloc["format"] == "HEVC 10 bits"
    assert (bloc["crf"], bloc["crf_uhd"]) == (21, 22)
    assert "codec" not in bloc

    # Un ancien client envoie encore « codec » : ignore, pas refuse.
    reponse = client.put(
        "/api/settings/preferences",
        json={"transcode": {"codec": "libx264", "crf": 40, "compress_audio": True}},
    )
    assert reponse.status_code == 200
    bloc = client.get("/api/settings/preferences").json()["transcode"]
    assert (bloc["crf"], bloc["compress_audio"]) == (28, True)


def test_la_pastille_de_la_page_dit_hevc_10_bits(file_isolee: Path) -> None:
    """La page Reencodage affiche « {codec} · CRF {crf} »."""
    reglages = api._state()["settings"]

    assert f"{reglages['codec']} · CRF {reglages['crf']}" == "HEVC 10 bits · CRF 21"
    assert reglages["crf_uhd"] == 22
