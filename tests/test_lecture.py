"""Ce qu'est un fichier, comment le montrer, et s'il est abime.

Le cas qui a motive ces tests : un reencodage de « Kandahar » en H.264 10 bits,
declare « corrompu » par Firefox au moment ou l'utilisateur decidait de jeter
son original. Le fichier etait sain ; le lecteur recopiait un codec que le
navigateur ne sait pas decoder, parce qu'il s'appelait « h264 » comme les
autres.

Tout ici tourne SANS ffmpeg : les sorties de ffprobe sont simulees en JSON, telles
que ffprobe les ecrit (valeurs relevees sur de vrais fichiers), et les
decodages du controle sont des processus factices.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sortilege.core import lecture
from sortilege.core.lecture import EtatControle, Mode, TypeHdr

TOUS_LES_FILTRES = frozenset({"zscale", "tonemap", "scale", "format"})


# --- Sorties de ffprobe simulees ----------------------------------------------


def _video(**champs) -> dict:
    base = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "width": 1920,
        "height": 1080,
        "avg_frame_rate": "24000/1001",
        "disposition": {"default": 1},
    }
    base.update(champs)
    return base


def _audio(codec: str = "aac", langue: str = "fre", canaux: int = 2, **champs) -> dict:
    base = {
        "codec_type": "audio",
        "codec_name": codec,
        "channels": canaux,
        "tags": {"language": langue},
        "disposition": {"default": 0},
    }
    base.update(champs)
    return base


def _sous_titre(codec: str = "subrip", langue: str = "fre", forcee: bool = False) -> dict:
    return {
        "codec_type": "subtitle",
        "codec_name": codec,
        "tags": {"language": langue},
        "disposition": {"forced": int(forcee)},
    }


def _flux(*streams: dict, format_name: str = "matroska,webm", duree: str = "7200.0") -> dict:
    return {"streams": list(streams), "format": {"format_name": format_name, "duration": duree}}


def _image(*donnees: dict, **champs) -> dict:
    return {"frames": [{**champs, "side_data_list": list(donnees)}]}


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
LUMIERE = {
    "side_data_type": "Content light level metadata",
    "max_content": 1000,
    "max_average": 400,
}
HDR10PLUS = {
    "side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)",
    "application version": 1,
    "num_windows": 1,
}


def _dovi(profil: int, compatibilite: int, el: int = 0) -> dict:
    return {
        "side_data_type": "DOVI configuration record",
        "dv_version_major": 1,
        "dv_version_minor": 0,
        "dv_profile": profil,
        "dv_level": 6,
        "rpu_present_flag": 1,
        "el_present_flag": el,
        "bl_present_flag": 1,
        "dv_bl_signal_compatibility_id": compatibilite,
    }


def _hevc(**champs) -> dict:
    base = {"codec_name": "hevc", "profile": "Main 10", "pix_fmt": "yuv420p10le"}
    base.update(champs)
    return _video(**base)


def analyse(*streams: dict, nom: str = "film.mkv", image: dict | None = None, **fmt):
    return lecture.lire_sonde(_flux(*streams, **fmt), image, nom=nom)


# --- La decision de lecture ---------------------------------------------------


def test_h264_8_bits_en_mp4_se_lit_directement() -> None:
    a = analyse(_video(), _audio("aac"), nom="film.mp4", format_name="mov,mp4,m4a,3gp,3g2,mj2")
    d = lecture.decider(a, ffmpeg=True)

    assert d.mode is Mode.DIRECT
    assert d.resume == "lecture directe"
    assert d.motif == "MP4 en H.264 8 bits, audio AAC : le navigateur lit le fichier tel quel."


def test_h264_8_bits_en_mp4_avec_mp3_se_lit_directement() -> None:
    a = analyse(_video(), _audio("mp3"), nom="film.mp4")
    assert lecture.decider(a).mode is Mode.DIRECT


def test_h264_8_bits_en_mkv_est_reemballe_sans_toucher_a_l_image() -> None:
    a = analyse(_video(), _audio("aac"))
    d = lecture.decider(a)

    assert d.mode is Mode.REMUX
    assert d.resume == "réemballage, image intacte"
    assert d.audio_copie is True
    argv = lecture.commande_hls("ffmpeg", Path("/f.mkv"), Path("/s"), a, d, 0)
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "libx264" not in argv


@pytest.mark.parametrize(
    ("codec", "profil", "nom"), [("dts", "DTS-HD MA", "DTS-HD MA"), ("truehd", "", "TrueHD")]
)
def test_un_audio_de_cinema_est_converti_en_aac_stereo(codec, profil, nom) -> None:
    """DTS et TrueHD ne passent dans aucun navigateur : l'image reste copiee, seul
    l'audio est converti — et le motif le dit."""
    a = analyse(_video(), _audio(codec, canaux=8, profile=profil))
    d = lecture.decider(a)

    assert d.mode is Mode.REMUX
    assert d.audio_copie is False
    assert f"audio {nom} 7.1 converti en AAC stéréo" in d.motif
    argv = lecture.commande_hls("ffmpeg", Path("/f.mkv"), Path("/s"), a, d, 0)
    assert argv[argv.index("-c:a") + 1 : argv.index("-c:a") + 4] == ["aac", "-ac", "2"]


def test_un_mp4_avec_de_l_ac3_n_est_pas_lu_directement() -> None:
    """Le conteneur est lisible, pas l'audio : Chrome jouerait l'image sans le son,
    ou refuserait. On reemballe avec un audio AAC."""
    a = analyse(_video(), _audio("ac3", canaux=6), nom="film.mp4")
    d = lecture.decider(a)

    assert d.mode is Mode.REMUX
    assert "AC-3 5.1 converti en AAC stéréo" in d.motif


def test_h264_high_10_est_converti_et_le_motif_le_dit() -> None:
    """Le cas Kandahar : un codec nomme « h264 » qu'aucun navigateur ne decode."""
    a = analyse(_video(profile="High 10", pix_fmt="yuv420p10le"), _audio("aac"))
    d = lecture.decider(a, filtres=TOUS_LES_FILTRES)

    assert d.mode is Mode.TRANSCODE
    assert d.motif == (
        "vidéo H.264 10 bits : aucun navigateur ne la décode, conversion de l'aperçu en 720p."
    )
    assert d.resume == "aperçu converti en 720p — ce n'est pas la qualité du fichier"


def test_h264_10_bits_meme_en_mp4_n_est_jamais_lu_directement() -> None:
    a = analyse(_video(profile="High 10", pix_fmt="yuv420p10le"), _audio("aac"), nom="k.mp4")
    assert lecture.decider(a).mode is Mode.TRANSCODE


def test_le_profil_trahit_le_10_bits_quand_le_format_de_pixel_manque() -> None:
    a = analyse(_video(profile="High 10", pix_fmt=None))

    assert a.profondeur == 10
    assert lecture.decider(a).mode is Mode.TRANSCODE


def test_h264_4_2_2_est_converti() -> None:
    a = analyse(_video(profile="High 4:2:2", pix_fmt="yuv422p"))
    d = lecture.decider(a)

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith("vidéo H.264 4:2:2 : aucun navigateur ne la décode")


def test_la_conversion_de_l_apercu_est_bornee_a_720p_8_bits() -> None:
    a = analyse(_hevc(width=3840, height=2160), _audio("eac3", canaux=6))
    d = lecture.decider(a)
    argv = lecture.commande_hls("/usr/bin/ffmpeg", Path("/f.mkv"), Path("/s"), a, d, 0)

    assert d.hauteur == 720
    assert argv[argv.index("-preset") + 1] == "veryfast"
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"
    assert argv[argv.index("-vf") + 1].startswith("scale=-2:720,")
    assert argv[argv.index("-c:a") + 1 : argv.index("-c:a") + 4] == ["aac", "-ac", "2"]


def test_une_petite_definition_n_est_pas_agrandie() -> None:
    a = analyse(_video(codec_name="mpeg2video", profile="Main", width=720, height=576))
    d = lecture.decider(a)
    argv = lecture.commande_hls("ffmpeg", Path("/f.vob"), Path("/s"), a, d, 0)

    assert d.hauteur == 576
    assert not argv[argv.index("-vf") + 1].startswith("scale")
    assert d.motif.startswith("vidéo MPEG-2 : aucun navigateur ne la lit")


@pytest.mark.parametrize(
    ("codec", "debut"),
    [
        ("hevc", "vidéo HEVC 10 bits : ce navigateur ne la décode pas"),
        ("av1", "vidéo AV1 10 bits : ce navigateur ne la décode pas"),
        ("vc1", "vidéo VC-1 10 bits : aucun navigateur ne la lit"),
    ],
)
def test_les_autres_codecs_sont_convertis(codec, debut) -> None:
    a = analyse(_hevc(codec_name=codec, profile="Main"))
    d = lecture.decider(a)

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith(debut)


# --- HEVC : ce que le navigateur declare savoir decoder ----------------------
#
# Firefox 136+ decode le HEVC par le materiel sur macOS, et le reencodage produit
# desormais du HEVC 10 bits : convertir un apercu que le navigateur lit tres
# bien ferait chauffer le NAS pour montrer une image moins fidele.


def test_hevc_10_bits_sdr_declare_decodable_est_reemballe_en_hvc1() -> None:
    a = analyse(_hevc(color_transfer="bt709"), _audio("eac3", canaux=6))
    d = lecture.decider(a, hevc="main10")
    argv = lecture.commande_hls("ffmpeg", Path("/f.mkv"), Path("/s"), a, d, 0)

    assert d.mode is Mode.REMUX
    assert d.resume == "réemballage, image intacte"
    assert d.motif.startswith("HEVC 10 bits, que ce navigateur décode, en MKV : réemballage")
    assert argv[argv.index("-c:v") + 1] == "copy"
    # Un HEVC « hev1 » dans un MP4 reste un ecran noir dans Safari et Firefox.
    assert argv[argv.index("-tag:v") + 1] == "hvc1"
    assert "libx264" not in argv


def test_hevc_8_bits_passe_avec_un_decodeur_main() -> None:
    a = analyse(_hevc(profile="Main", pix_fmt="yuv420p"))
    assert lecture.decider(a, hevc="main").mode is Mode.REMUX
    assert lecture.decider(a, hevc="main10").mode is Mode.REMUX


def test_hevc_10_bits_ne_passe_pas_avec_un_decodeur_main_seulement() -> None:
    d = lecture.decider(analyse(_hevc()), hevc="main")

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith("vidéo HEVC 10 bits : ce navigateur décode le HEVC 8 bits, pas le 10")


def test_hevc_sans_decodeur_declare_est_converti() -> None:
    d = lecture.decider(analyse(_hevc()), hevc="0")

    assert d.mode is Mode.TRANSCODE
    assert d.motif == (
        "vidéo HEVC 10 bits : ce navigateur ne la décode pas, conversion de l'aperçu en 720p."
    )


def test_hevc_hdr_reste_converti_meme_si_le_navigateur_decode_le_hevc() -> None:
    """Un apercu delave ferait croire a un mauvais encodage, sur l'ecran meme ou
    l'on juge l'encodage."""
    a = analyse(_hevc(), image=_image(MASTERING, color_transfer="smpte2084"))
    d = lecture.decider(a, hevc="main10", filtres=TOUS_LES_FILTRES)

    assert d.mode is Mode.TRANSCODE
    assert d.tonemap is True
    assert d.motif == (
        "vidéo HEVC 10 bits HDR10 : ce navigateur ne rend pas le HDR fidèlement, "
        "conversion de l'aperçu en 720p SDR."
    )


def test_hevc_dolby_vision_reste_converti() -> None:
    a = analyse(_hevc(side_data_list=[_dovi(8, 1)]), image=_image(color_transfer="smpte2084"))
    assert lecture.decider(a, hevc="main10").mode is Mode.TRANSCODE


def test_hevc_4_2_2_est_hors_de_portee_quoi_que_declare_le_navigateur() -> None:
    a = analyse(_hevc(profile="Rext", pix_fmt="yuv422p10le"))
    d = lecture.decider(a, hevc="main10")

    assert d.mode is Mode.TRANSCODE
    assert "hors de portée d'un navigateur" in d.motif


def test_h264_10_bits_reste_converti_quoi_que_declare_le_navigateur() -> None:
    """Aucun navigateur ne decode le H.264 High 10, HEVC ou pas."""
    a = analyse(_video(profile="High 10", pix_fmt="yuv420p10le"))
    d = lecture.decider(a, hevc="main10")

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith("vidéo H.264 10 bits : aucun navigateur ne la décode")


def test_la_conversion_forcee_l_emporte_sur_un_hevc_decodable() -> None:
    d = lecture.decider(analyse(_hevc()), hevc="main10", conversion=True)

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith("le navigateur n'a pas su lire la version non convertie")


def test_hevc_hdr10_est_ramene_en_sdr_quand_zscale_est_la() -> None:
    a = analyse(
        _hevc(color_transfer="smpte2084", color_primaries="bt2020"), image=_image(MASTERING)
    )
    d = lecture.decider(a, filtres=TOUS_LES_FILTRES)
    filtre = lecture.commande_hls("ffmpeg", Path("/f"), Path("/s"), a, d, 0)

    assert d.tonemap is True
    assert d.motif.endswith("conversion de l'aperçu en 720p SDR.")
    vf = filtre[filtre.index("-vf") + 1]
    assert "zscale=tin=smpte2084:pin=bt2020" in vf
    assert "tonemap=tonemap=hable" in vf
    assert vf.endswith("format=yuv420p")
    assert d.avertissements == ()


def test_sans_zscale_le_hdr_se_lit_quand_meme_et_on_previent_du_delave() -> None:
    a = analyse(_hevc(color_transfer="smpte2084"))
    d = lecture.decider(a, filtres={"tonemap"})

    assert d.mode is Mode.TRANSCODE
    assert d.tonemap is False
    assert any("Couleurs délavées dans l'aperçu" in m for m in d.avertissements)
    argv = lecture.commande_hls("ffmpeg", Path("/f"), Path("/s"), a, d, 0)
    assert "zscale" not in argv[argv.index("-vf") + 1]


def test_hlg_est_converti_depuis_sa_propre_courbe() -> None:
    a = analyse(_hevc(color_transfer="arib-std-b67", color_primaries="bt2020"))
    d = lecture.decider(a, filtres=TOUS_LES_FILTRES)
    argv = lecture.commande_hls("ffmpeg", Path("/f"), Path("/s"), a, d, 0)

    assert a.hdr is TypeHdr.HLG
    assert "zscale=tin=arib-std-b67" in argv[argv.index("-vf") + 1]


def test_la_position_de_depart_est_un_saut_avant_l_entree() -> None:
    a = analyse(_hevc())
    d = lecture.decider(a)
    argv = lecture.commande_hls("ffmpeg", Path("/f"), Path("/s"), a, d, 1234.5)

    assert argv.index("-ss") < argv.index("-i")
    assert argv[argv.index("-ss") + 1] == "1234.500"


def test_les_segments_sont_du_fmp4_de_six_secondes() -> None:
    a = analyse(_hevc())
    argv = lecture.commande_hls("ffmpeg", Path("/f"), Path("/s"), a, lecture.decider(a), 0)

    assert argv[argv.index("-hls_segment_type") + 1] == "fmp4"
    assert argv[argv.index("-hls_time") + 1] == "6"
    assert "temp_file" in argv[argv.index("-hls_flags") + 1]


def test_sans_ffmpeg_un_mkv_est_impossible_a_montrer() -> None:
    d = lecture.decider(analyse(_video(), _audio("ac3")), ffmpeg=False)
    assert d.mode is Mode.IMPOSSIBLE
    assert d.motif.startswith("ffmpeg absent")


def test_sans_ffmpeg_un_mp4_lisible_se_lit_encore() -> None:
    d = lecture.decider(analyse(_video(), _audio("aac"), nom="f.mp4"), ffmpeg=False)
    assert d.mode is Mode.DIRECT


def test_un_fichier_que_ffprobe_ne_lit_pas_est_soupconne() -> None:
    a = lecture.Analyse(nom="x.mkv", extension=".mkv", motif_illisible="ffprobe ne lit pas")
    d = lecture.decider(a)
    assert d.mode is Mode.IMPOSSIBLE
    assert "peut-être abîmé" in d.motif


def test_la_conversion_forcee_prend_le_relais_d_une_lecture_directe_ratee() -> None:
    """Le recours quand le navigateur echoue et que le serveur decode sans erreur."""
    a = analyse(_video(), _audio("aac"), nom="f.mp4")
    d = lecture.decider(a, conversion=True)

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith("le navigateur n'a pas su lire la version non convertie")


def test_une_image_de_couverture_n_est_pas_la_video() -> None:
    """Un MP4 porte souvent sa jaquette comme un flux video : la prendre pour le
    film ferait decider sur un JPEG."""
    jaquette = _video(codec_name="mjpeg", profile="", disposition={"attached_pic": 1})
    a = analyse(jaquette, _video(), nom="f.mp4")
    assert a.video_codec == "h264"


# --- HDR : type, metadonnees statiques, Dolby Vision -------------------------


def test_hdr10_avec_maxcll_rend_les_valeurs_pour_x265() -> None:
    a = analyse(
        _hevc(color_primaries="bt2020"),
        image=_image(MASTERING, LUMIERE, color_transfer="smpte2084"),
    )

    assert a.hdr is TypeHdr.HDR10
    assert a.mastering is not None
    assert a.mastering.pour_x265() == (
        "G(13250,34500)B(7500,3000)R(34000,16000)WP(15635,16450)L(10000000,50)"
    )
    assert a.mastering.luminance_max == 1000.0
    assert a.mastering.luminance_min == 0.005
    assert a.lumiere is not None
    assert a.lumiere.pour_x265() == "1000,400"


def test_hdr10_sans_maxcll_ne_l_invente_pas() -> None:
    a = analyse(_hevc(), image=_image(MASTERING, color_transfer="smpte2084"))

    assert a.hdr is TypeHdr.HDR10
    assert a.mastering is not None
    assert a.lumiere is None


def test_la_courbe_pq_lue_sur_la_premiere_image_suffit() -> None:
    """Dans un MKV ecrit par ffmpeg, la fonction de transfert n'est que dans le
    flux video : lire le conteneur seul classerait ce film HDR10 en SDR."""
    a = analyse(_hevc(), image=_image(color_transfer="smpte2084"))
    assert a.hdr is TypeHdr.HDR10


def test_un_maxcll_nul_est_garde_tel_quel() -> None:
    """Zero veut dire « inconnu, declare » dans la norme : ce n'est pas une absence."""
    lumiere = {**LUMIERE, "max_content": 0, "max_average": 0}
    a = analyse(_hevc(), image=_image(lumiere, color_transfer="smpte2084"))
    assert a.lumiere == lecture.LumiereContenu(0, 0)


def test_des_metadonnees_de_mastering_incompletes_ne_sont_pas_completees() -> None:
    partiel = {k: v for k, v in MASTERING.items() if k != "min_luminance"}
    a = analyse(_hevc(), image=_image(partiel, color_transfer="smpte2084"))
    assert a.mastering is not None
    assert a.mastering.luminance_min is None
    assert a.mastering.pour_x265() is None


def test_hlg() -> None:
    a = analyse(_hevc(color_transfer="arib-std-b67", color_primaries="bt2020"))

    assert a.hdr is TypeHdr.HLG
    assert a.libelle_hdr == "HLG"
    assert a.mastering is None
    assert a.lumiere is None


def test_hdr10plus_est_reconnu() -> None:
    a = analyse(_hevc(), image=_image(MASTERING, HDR10PLUS, color_transfer="smpte2084"))

    assert a.hdr is TypeHdr.HDR10PLUS
    assert a.hdr10plus is True
    assert a.libelle_hdr == "HDR10+"


def test_dolby_vision_profil_5_n_a_pas_de_base_exploitable() -> None:
    """Le cas vital : decode comme du HDR10, un profil 5 vire au violet et au vert."""
    a = analyse(_hevc(side_data_list=[_dovi(5, 0)]))
    dv = a.dolby_vision

    assert a.hdr is TypeHdr.DOLBY_VISION
    assert dv is not None
    assert dv.profil == 5
    assert dv.base is None
    assert dv.base_exploitable is False
    assert dv.libelle == "Dolby Vision profil 5"
    d = lecture.decider(a, filtres=TOUS_LES_FILTRES)
    assert any("couleurs fausses" in m for m in d.avertissements)


def test_dolby_vision_profil_8_1_a_une_base_hdr10() -> None:
    a = analyse(
        _hevc(side_data_list=[_dovi(8, 1)]),
        image=_image(MASTERING, LUMIERE, color_transfer="smpte2084"),
    )
    dv = a.dolby_vision

    assert dv is not None
    assert dv.libelle == "Dolby Vision profil 8.1"
    assert dv.base == "hdr10"
    assert dv.base_exploitable is True
    assert a.libelle_hdr == "Dolby Vision profil 8.1"
    # Les metadonnees HDR10 de la couche de base restent lisibles pour x265.
    assert a.mastering is not None and a.mastering.pour_x265() is not None
    assert lecture.decider(a, filtres=TOUS_LES_FILTRES).avertissements == ()


def test_dolby_vision_profil_7_s_appuie_sur_sa_couche_de_base() -> None:
    a = analyse(_hevc(side_data_list=[_dovi(7, 6, el=1)]))
    assert a.dolby_vision is not None
    assert a.dolby_vision.base == "hdr10"
    assert a.dolby_vision.couche_amelioration is True


def test_dolby_vision_profil_8_4_part_d_une_base_hlg() -> None:
    a = analyse(_hevc(color_transfer="arib-std-b67", side_data_list=[_dovi(8, 4)]))
    d = lecture.decider(a, filtres=TOUS_LES_FILTRES)
    argv = lecture.commande_hls("ffmpeg", Path("/f"), Path("/s"), a, d, 0)

    assert a.dolby_vision is not None and a.dolby_vision.base == "hlg"
    assert "zscale=tin=arib-std-b67" in argv[argv.index("-vf") + 1]


def test_sdr_10_bits_n_est_pas_du_hdr() -> None:
    a = analyse(_hevc(color_transfer="bt709", color_primaries="bt709"))

    assert a.profondeur == 10
    assert a.hdr is TypeHdr.SDR
    assert a.est_hdr is False
    assert lecture.decider(a, filtres=TOUS_LES_FILTRES).tonemap is False


def test_la_definition_d_un_film_en_scope_tient_compte_de_la_largeur() -> None:
    assert analyse(_video(width=1920, height=800)).definition == "1080p"
    assert analyse(_video(width=3840, height=1600)).definition == "2160p"


# --- Le controle de decodage --------------------------------------------------


def test_les_bruits_d_un_saut_dans_un_fichier_sain_ne_sont_pas_des_erreurs() -> None:
    """Releves sur un H.264 SAIN en GOP ouvert : juste apres un saut, le decodeur
    reclame des images d'avant le point d'entree. Les compter ferait declarer
    abime un fichier intact — l'erreur exacte a eviter ici."""
    bruits = "\n".join(
        [
            "[h264 @ 0x7bc9019500] Missing reference picture, default is 0",
            "[h264 @ 0x7bc9019c00] mmco: unref short failure",
            "    Last message repeated 1 times",
        ]
    )
    erreurs, compte = lecture.classer_sortie(bruits, images=96, code=0, attendues=96)
    assert erreurs == []
    assert compte == 3


def test_une_erreur_de_conteneur_est_une_vraie_erreur() -> None:
    sortie = (
        "[in#0/matroska,webm @ 0x7894c20000] Element at 0x6da0bc ending at 0xc746e81 "
        "exceeds containing master element ending at 0x6ee9e3\n"
        "[h264 @ 0x789501b800] reference picture missing during reorder\n"
    )
    erreurs, _ = lecture.classer_sortie(sortie, images=97, code=0, attendues=96)
    assert erreurs == [
        "[in#0/matroska,webm] Element at 0x6da0bc ending at 0xc746e81 exceeds containing "
        "master element ending at 0x6ee9e3"
    ]


def test_trop_de_bruits_n_est_plus_un_saut() -> None:
    sortie = "\n".join(["[h264 @ 0x1] Missing reference picture"] * 12)
    erreurs, _ = lecture.classer_sortie(sortie, images=96, code=0, attendues=96)
    assert erreurs


def test_aucune_image_decodee_la_ou_il_en_faut_est_une_erreur() -> None:
    """Un fichier tronque ne se plaint pas toujours : il ne produit rien."""
    erreurs, _ = lecture.classer_sortie("", images=0, code=0, attendues=96)
    assert erreurs == ["aucune image décodée à cet endroit"]


def test_un_arret_en_erreur_sans_message_est_une_erreur() -> None:
    erreurs, _ = lecture.classer_sortie("", images=96, code=1, attendues=96)
    assert erreurs == ["ffmpeg s'est arrêté en erreur (code 1)"]


class _Decodeur:
    """Remplace ffmpeg : une reponse par position, dans l'ordre."""

    def __init__(self, *reponses):
        self.reponses = list(reponses)
        self.appels: list[list[str]] = []

    def __call__(self, argv, **_options):
        self.appels.append(argv)
        reponse = self.reponses.pop(0)
        if isinstance(reponse, BaseException):
            raise reponse
        stderr, images, code = reponse
        return SimpleNamespace(
            stdout=f"frame={images}\nprogress=end\n", stderr=stderr, returncode=code
        )


SAIN = ("", 96, 0)
DUREE_FILM = "7200.0"


def _film() -> lecture.Analyse:
    return analyse(_video(avg_frame_rate="24/1"), _audio("ac3"), duree=DUREE_FILM)


def test_un_fichier_qui_se_decode_aux_cinq_points_est_sain() -> None:
    decodeur = _Decodeur(SAIN, SAIN, SAIN, SAIN, SAIN)
    r = lecture.controler(Path("/f.mkv"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.SAIN
    # Ce qui a ete verifie, et rien de plus : jamais « sain », jamais « intact ».
    assert r.message == ("Aucune erreur de décodage aux 5 points testés (5, 25, 50, 75 et 95 %).")
    assert r.en_dict()["libelle"] == "aucune erreur aux 5 points testés"
    positions = [float(a[a.index("-ss") + 1]) for a in decodeur.appels]
    assert positions == [360.0, 1800.0, 3600.0, 5400.0, 6840.0]
    assert all(a[a.index("-t") + 1] == "4" for a in decodeur.appels)


def test_une_erreur_vers_le_milieu_rend_le_fichier_abime_avec_sa_position() -> None:
    erreur = ("[h264 @ 0x1] error while decoding MB 12 34, bytestream -7", 90, 0)
    decodeur = _Decodeur(SAIN, SAIN, erreur, SAIN, SAIN)
    r = lecture.controler(Path("/f.mkv"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.ABIME
    assert r.position == 0.5
    assert r.message == "Erreur de décodage vers 50 % : le fichier est abîmé."
    assert r.premiere_erreur == "[h264] error while decoding MB 12 34, bytestream -7"


def test_un_delai_depasse_ne_conclut_pas() -> None:
    lent = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=60)
    decodeur = _Decodeur(SAIN, lent, SAIN, SAIN, SAIN)
    r = lecture.controler(Path("/f.mkv"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.INDETERMINE
    assert "Délai dépassé vers 25 %" in r.message


def test_sans_ffmpeg_le_controle_est_indetermine(monkeypatch) -> None:
    monkeypatch.setattr(lecture.shutil, "which", lambda _nom: None)
    r = lecture.controler(Path("/f.mkv"), analyse=_film())

    assert r.etat is EtatControle.INDETERMINE
    assert r.message.startswith("ffmpeg absent")


def test_un_reencodage_plus_court_que_l_original_est_abime() -> None:
    """Un encodage interrompu se decode tres bien : seule la duree le trahit."""
    decodeur = _Decodeur(SAIN, SAIN, SAIN, SAIN, SAIN)
    r = lecture.controler(
        Path("/f.mkv"),
        analyse=_film(),
        duree_originale=7500.0,
        executer=decodeur,
        ffmpeg="ffmpeg",
    )
    assert r.etat is EtatControle.ABIME
    assert "pour l'original : le fichier est incomplet" in r.message


def test_un_ecart_de_duree_d_une_seconde_est_tolere() -> None:
    decodeur = _Decodeur(SAIN, SAIN, SAIN, SAIN, SAIN)
    r = lecture.controler(
        Path("/f.mkv"), analyse=_film(), duree_originale=7201.0, executer=decodeur, ffmpeg="ff"
    )
    assert r.etat is EtatControle.SAIN


def test_un_seul_controle_a_la_fois() -> None:
    """Deux controles en parallele doubleraient la charge du NAS pour repondre
    deux fois moins vite."""
    libere = threading.Event()
    fini = threading.Event()

    def lent(_source, **_options):
        libere.wait(5)
        fini.set()
        return lecture.ResultatControle(EtatControle.SAIN, "ok")

    controles = lecture.Controles(executer=lent)
    assert controles.lancer("a", Path("/a"))["etat"] == "en_cours"
    # Le meme fichier redemande : pas d'erreur, le meme controle.
    assert controles.lancer("a", Path("/a"))["etat"] == "en_cours"
    with pytest.raises(lecture.Occupe):
        controles.lancer("b", Path("/b"))

    libere.set()
    assert fini.wait(5)
    for _ in range(100):
        if controles.etat("a")["etat"] == "sain":
            break
        threading.Event().wait(0.01)
    assert controles.etat("a")["etat"] == "sain"
    # Le premier fini, le second passe.
    assert controles.lancer("b", Path("/b"))["etat"] in ("en_cours", "sain")


# --- La fiche comparee --------------------------------------------------------


def _original_hdr10() -> lecture.Analyse:
    return analyse(
        _hevc(width=3840, height=2160),
        _audio("truehd", "eng", 8),
        _audio("eac3", "fre", 6),
        _sous_titre("subrip", "fre"),
        _sous_titre("hdmv_pgs_subtitle", "eng"),
        image=_image(MASTERING, LUMIERE, color_transfer="smpte2084"),
    )


def _reencode(**video) -> lecture.Analyse:
    base = {"profile": "High", "pix_fmt": "yuv420p"}
    base.update(video)
    return analyse(
        _video(**base),
        _audio("truehd", "eng", 8),
        _audio("eac3", "fre", 6),
        _sous_titre("subrip", "fre"),
        _sous_titre("hdmv_pgs_subtitle", "eng"),
    )


def _codes(comparaison: lecture.Comparaison) -> list[str]:
    return [v.code for v in comparaison.verdicts]


def test_un_reencodage_h264_10_bits_est_signale() -> None:
    """Kandahar : la source 4K HDR a donne un H.264 High 10."""
    reencode = _reencode(profile="High 10", pix_fmt="yuv420p10le", color_transfer="smpte2084")
    c = lecture.comparer(_original_hdr10(), reencode)

    verdict = next(v for v in c.verdicts if v.code == "h264_10_bits")
    assert verdict.gravite is lecture.Gravite.GRAVE
    assert "très peu de téléviseurs et de clés de streaming le lisent" in verdict.texte
    assert "ne remplace pas l'original" in verdict.texte.lower()
    assert c.gravite == "grave"
    assert c.phrase.startswith("Ne remplace pas l'original en l'état : H.264 10 bits")
    # Le HDR est encore la (courbe PQ recopiee), mais ses metadonnees non.
    assert "metadonnees_hdr" in _codes(c)


def test_une_source_hdr_devenue_sdr_est_signalee() -> None:
    c = lecture.comparer(_original_hdr10(), _reencode())

    verdict = next(v for v in c.verdicts if v.code == "hdr_perdu")
    assert verdict.gravite is lecture.Gravite.GRAVE
    assert verdict.texte == "Source HDR10 devenue SDR : couleurs délavées probables."


def test_un_dolby_vision_devenu_sdr_est_un_hdr_perdu() -> None:
    original = analyse(
        _hevc(side_data_list=[_dovi(8, 1)]), image=_image(color_transfer="smpte2084")
    )
    c = lecture.comparer(original, _reencode())

    verdict = next(v for v in c.verdicts if v.code == "hdr_perdu")
    assert "Dolby Vision profil 8.1 devenue SDR" in verdict.texte


def test_un_dolby_vision_5_reencode_sans_dolby_vision_est_grave() -> None:
    original = analyse(
        _hevc(side_data_list=[_dovi(5, 0)]), image=_image(color_transfer="smpte2084")
    )
    reencode = analyse(_hevc(), image=_image(color_transfer="smpte2084"))
    c = lecture.comparer(original, reencode)

    verdict = next(v for v in c.verdicts if v.code == "dolby_vision_5")
    assert verdict.gravite is lecture.Gravite.GRAVE
    assert "violet et au vert" in verdict.texte


def test_un_dolby_vision_8_1_garde_en_hdr10_n_est_qu_une_information() -> None:
    original = analyse(
        _hevc(side_data_list=[_dovi(8, 1)]),
        image=_image(MASTERING, color_transfer="smpte2084"),
    )
    reencode = analyse(_hevc(), image=_image(MASTERING, color_transfer="smpte2084"))
    c = lecture.comparer(original, reencode)

    assert _codes(c) == ["dolby_vision_perdu"]
    assert c.verdicts[0].gravite is lecture.Gravite.INFO


def test_une_duree_differente_de_plus_de_deux_secondes_est_signalee() -> None:
    original = analyse(_video(), duree="7200.0")
    court = analyse(_video(), duree="5520.0")
    c = lecture.comparer(original, court)

    verdict = next(v for v in c.verdicts if v.code == "duree")
    assert verdict.gravite is lecture.Gravite.GRAVE
    assert "écart de 28 min 00 s" in verdict.texte


def test_une_seconde_et_demie_d_ecart_n_est_pas_un_defaut() -> None:
    c = lecture.comparer(analyse(_video(), duree="7200.0"), analyse(_video(), duree="7201.5"))
    assert "duree" not in _codes(c)


def test_une_piste_audio_perdue_est_signalee_par_sa_langue() -> None:
    original = analyse(_video(), _audio("ac3", "fre"), _audio("dts", "eng"))
    reencode = analyse(_video(), _audio("ac3", "fre"))
    c = lecture.comparer(original, reencode)

    verdict = next(v for v in c.verdicts if v.code == "audio_perdu")
    assert verdict.texte == "Piste audio perdue : 2 dans l'original, 1 ici (manque eng)."


def test_une_piste_de_sous_titres_perdue_est_signalee() -> None:
    original = analyse(_video(), _sous_titre("subrip", "fre"), _sous_titre("ass", "eng"))
    reencode = analyse(_video(), _sous_titre("subrip", "fre"))
    c = lecture.comparer(original, reencode)

    assert "sous_titres_perdus" in _codes(c)
    assert c.gravite == "grave"


def test_un_controle_abime_entre_dans_le_verdict() -> None:
    controle = lecture.ResultatControle(
        EtatControle.ABIME, "Erreur de décodage vers 75 % : le fichier est abîmé.", position=0.75
    )
    c = lecture.comparer(analyse(_video()), analyse(_video()), controle)

    assert _codes(c) == ["decodage"]
    assert c.phrase == "Ne remplace pas l'original en l'état : erreur de décodage vers 75 %."


def test_rien_a_signaler_apres_un_controle_sain() -> None:
    sain = lecture.ResultatControle(EtatControle.SAIN, "ok")
    c = lecture.comparer(_original_hdr10(), _original_hdr10(), sain)

    assert c.verdicts == ()
    assert c.gravite == "ok"
    assert c.phrase.startswith("Rien à signaler")


def test_sans_controle_la_fiche_dit_qu_il_reste_a_faire() -> None:
    c = lecture.comparer(analyse(_video()), analyse(_video()))
    assert c.gravite == "en_attente"
    assert "pas encore rendu son résultat" in c.phrase


def test_l_analyse_se_rend_en_dictionnaire_pour_l_ecran() -> None:
    d = _original_hdr10().en_dict()

    assert d["hdr"]["type"] == "hdr10"
    assert d["hdr"]["mastering"]["x265"].startswith("G(13250,34500)")
    assert d["hdr"]["lumiere"]["x265"] == "1000,400"
    assert d["video"]["libelle_profil"] == "HEVC Main 10"
    assert [p["libelle"] for p in d["audio"]] == ["eng TrueHD 7.1", "fre E-AC-3 5.1"]
    assert d["duree_lisible"] == "2 h 00 min"


# --- VP9 et AV1 : ce que le navigateur declare savoir decoder ----------------
#
# Firefox lit tels quels un WebM VP9 et un MP4 AV1. Les faire convertir par le
# NAS, c'etait un AV1 4K converti en logiciel sur un Celeron, pour une image
# moins fidele que le fichier.


def _libre(codec: str = "av1", pix_fmt: str = "yuv420p", **champs) -> dict:
    return _video(
        codec_name=codec,
        profile="Main" if codec == "av1" else "Profile 0",
        pix_fmt=pix_fmt,
        **champs,
    )


def test_un_webm_vp9_declare_se_lit_directement() -> None:
    a = analyse(_libre("vp9"), _audio("opus"), nom="film.webm")
    d = lecture.decider(a, vp9="8")

    assert d.mode is Mode.DIRECT
    assert d.motif == "WebM en VP9, audio Opus : le navigateur lit le fichier tel quel."


def test_un_mp4_av1_declare_se_lit_directement() -> None:
    a = analyse(_libre("av1", width=3840, height=2160), _audio("aac"), nom="film.mp4")
    assert lecture.decider(a, av1="8").mode is Mode.DIRECT


def test_un_av1_10_bits_en_mkv_est_reemballe_sans_toucher_a_l_image() -> None:
    a = analyse(_libre("av1", "yuv420p10le"), _audio("eac3", canaux=6))
    d = lecture.decider(a, av1="10")
    argv = lecture.commande_hls("ffmpeg", Path("/f.mkv"), Path("/s"), a, d, 0)

    assert d.mode is Mode.REMUX
    assert argv[argv.index("-c:v") + 1] == "copy"
    assert "libx264" not in argv


def test_un_av1_10_bits_ne_passe_pas_avec_un_decodeur_8_bits() -> None:
    d = lecture.decider(analyse(_libre("av1", "yuv420p10le")), av1="8")

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith("vidéo AV1 10 bits : ce navigateur ne décode ce codec qu'en 8 bits")


def test_un_vp9_sans_declaration_reste_converti() -> None:
    d = lecture.decider(analyse(_libre("vp9"), nom="film.webm"))

    assert d.mode is Mode.TRANSCODE
    assert d.motif.startswith("vidéo VP9 : ce navigateur ne la décode pas")


def test_un_av1_hdr_reste_converti_meme_declare() -> None:
    a = analyse(_libre("av1", "yuv420p10le"), image=_image(color_transfer="smpte2084"))
    assert lecture.decider(a, av1="10").mode is Mode.TRANSCODE


def test_un_av1_4_4_4_est_hors_de_portee() -> None:
    d = lecture.decider(analyse(_libre("av1", "yuv444p")), av1="10")

    assert d.mode is Mode.TRANSCODE
    assert "hors de portée d'un navigateur" in d.motif


# --- La commande HLS : les horodatages restent ceux du film ------------------


def test_apres_un_saut_les_segments_gardent_la_position_reelle() -> None:
    """Sans cela, la position affichee ne pouvait etre que « point demande +
    temps ecoule », et un fichier tronque la faisait mentir."""
    a = analyse(_video(), _audio("aac"))
    argv = lecture.commande_hls("ffmpeg", Path("/f.mkv"), Path("/s"), a, lecture.decider(a), 108)

    assert argv[argv.index("-output_ts_offset") + 1] == "108.000"
    options = argv[argv.index("-hls_segment_options") + 1].split(":")
    assert "movflags=+frag_discont" in options
    # Base de temps fine : la base calquee sur la cadence donnait, sous le
    # ffmpeg 7.1 de l'image, des temps de decodage en double.
    assert "video_track_timescale=90000" in options
    assert "-noaccurate_seek" not in argv


def test_la_conversion_part_de_l_image_cle_qui_precede() -> None:
    """Comme la copie : un saut au-dela de la fin reelle montre les dernieres
    images qui existent, au lieu de ne rien produire."""
    a = analyse(_hevc())
    argv = lecture.commande_hls("ffmpeg", Path("/f.mkv"), Path("/s"), a, lecture.decider(a), 90)

    assert argv.index("-noaccurate_seek") < argv.index("-i")
    # « vfr » : les horodatages d'origine, sans jamais deux images au meme.
    assert argv[argv.index("-fps_mode") + 1] == "vfr"
    assert "prev_forced_t" in argv[argv.index("-force_key_frames") + 1]


def test_l_aac_copie_d_un_mpeg_ts_passe_en_mp4() -> None:
    """L'AAC d'un MPEG-TS est en ADTS : sans ce filtre, ffmpeg refuse le premier
    paquet et la session meurt sans image."""
    a = analyse(_video(), _audio("aac"), nom="film.ts")
    argv = lecture.commande_hls("ffmpeg", Path("/f.ts"), Path("/s"), a, lecture.decider(a), 0)

    assert argv[argv.index("-bsf:a") + 1] == "aac_adtstoasc"


# --- Ou l'on est vraiment dans le film ----------------------------------------


def _boite(genre: bytes, contenu: bytes) -> bytes:
    import struct

    return struct.pack(">I", 8 + len(contenu)) + genre + contenu


def _init(*pistes: tuple[int, int, bytes]) -> bytes:
    """moov > trak > (tkhd, mdia > (mdhd, hdlr)), version 0."""
    import struct

    traks = b""
    for ident, echelle, nature in pistes:
        tkhd = _boite(
            b"tkhd", b"\x00\x00\x00\x03" + b"\x00" * 8 + struct.pack(">I", ident) + b"\x00" * 68
        )
        mdhd = _boite(b"mdhd", b"\x00" * 12 + struct.pack(">I", echelle) + b"\x00" * 8)
        hdlr = _boite(b"hdlr", b"\x00" * 8 + nature + b"\x00" * 13)
        traks += _boite(b"trak", tkhd + _boite(b"mdia", mdhd + hdlr))
    return _boite(b"ftyp", b"iso5") + _boite(b"moov", traks)


def _segment(*departs: tuple[int, int]) -> bytes:
    """moof > traf > (tfhd, tfdt version 1), puis un mdat."""
    import struct

    trafs = b""
    for ident, instant in departs:
        tfhd = _boite(b"tfhd", b"\x00\x02\x00\x00" + struct.pack(">I", ident))
        tfdt = _boite(b"tfdt", b"\x01\x00\x00\x00" + struct.pack(">Q", instant))
        trafs += _boite(b"traf", tfhd + tfdt)
    return _boite(b"styp", b"msdh") + _boite(b"moof", trafs) + _boite(b"mdat", b"\x00" * 64)


def test_l_origine_se_lit_dans_le_premier_segment() -> None:
    """La video commence a 70 s (tronque, image cle d'avant le saut), le son a
    69,995 s : hls.js cale le « 0 » sur la plus precoce des deux."""
    init = _init((1, 16000, b"vide"), (2, 48000, b"soun"))
    segment = _segment((1, 70 * 16000), (2, int(69.995 * 48000)))

    debut, image = lecture.origines_hls(init, segment)

    assert image == 70.0
    assert debut == pytest.approx(69.995)


def test_un_segment_illisible_ne_donne_aucune_origine() -> None:
    assert lecture.origines_hls(b"\x00\x00", b"garbage") == (None, None)
    assert lecture.origines_hls(_init((1, 1000, b"vide")), b"\x00\x00\x00\x10moof") == (None, None)


def test_un_fichier_tronque_le_dit_au_lieu_d_afficher_la_position_demandee() -> None:
    """Le cas du controleur : tronque a 1:12, il annonce 2:00, on saute a 90 %."""
    alerte = lecture.alerte_position(108.0, 70.0, fin=71.97, duree=120.02)
    assert alerte == "Rien à 1:48 : le fichier s'arrête vers 1:12 alors qu'il annonce 2:00."


def test_une_fin_avant_la_duree_annoncee_se_dit_meme_sans_saut() -> None:
    alerte = lecture.alerte_position(60.0, 60.0, fin=71.9, duree=120.0)
    assert alerte == "Le fichier s'arrête vers 1:12 alors qu'il annonce 2:00."


def test_une_image_cle_proche_n_est_pas_une_alerte() -> None:
    """Dix secondes d'ecart avec un x264 regle par defaut, c'est normal."""
    assert lecture.alerte_position(55.2, 60.02, fin=120.02, duree=120.02) == ""
    assert lecture.alerte_position(50.0, 48.0, fin=None, duree=120.0) == ""


def test_la_duree_produite_se_lit_dans_la_liste() -> None:
    liste = "#EXTM3U\n#EXTINF:6.000000,\nseg_00000.m4s\n#EXTINF:1.968000,\nseg_00001.m4s\n"
    assert lecture.duree_liste(liste) == pytest.approx(7.968)
    assert lecture.temps_lisible(71.968) == "1:12"
    assert lecture.temps_lisible(3725) == "1:02:05"


# --- Le controle : pas de faux « abime » sur un fichier sain ------------------

COUPURE_AC3 = (
    "[aist#0:1/ac3 @ 0x758ac18180] [dec:ac3 @ 0x758b008640] Error submitting packet to "
    "decoder: Invalid data found when processing input"
)


def test_le_paquet_audio_coupe_par_le_saut_est_tolere() -> None:
    """Un VOB sain (MPEG-2 + AC-3) : chaque saut coupe le premier paquet AC-3.
    La premiere demi-seconde, redecodee depuis le meme point, le reproduit :
    il etait au point d'entree."""
    fenetre = (COUPURE_AC3, 96, 0)
    debut = (COUPURE_AC3, 12, 0)
    decodeur = _Decodeur(SAIN, SAIN, fenetre, debut, SAIN, SAIN)
    r = lecture.controler(Path("/f.vob"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.SAIN
    court = decodeur.appels[3]
    assert court[court.index("-ss") + 1] == "3600.000"
    assert court[court.index("-t") + 1] == "0.5"
    assert r.points[2].bruits == 1


def test_le_meme_message_plus_loin_dans_la_fenetre_reste_une_erreur() -> None:
    """La premiere demi-seconde ne le reproduit pas : le paquet refuse etait
    plus loin, c'est une vraie erreur."""
    decodeur = _Decodeur(SAIN, SAIN, (COUPURE_AC3, 96, 0), ("", 12, 0), SAIN, SAIN)
    r = lecture.controler(Path("/f.vob"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.ABIME
    assert r.position == 0.5


def test_des_paquets_refuses_en_serie_ne_passent_pas_pour_un_seul() -> None:
    """ffmpeg resume les repetitions : un paquet coupe au saut suivi de trois
    autres refuses plus loin ne doit pas se confondre avec le seul premier."""
    serie = f"{COUPURE_AC3}\n    Last message repeated 3 times"
    decodeur = _Decodeur(SAIN, SAIN, (serie, 96, 0), (COUPURE_AC3, 12, 0), SAIN, SAIN)
    r = lecture.controler(Path("/f.vob"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.ABIME


def test_une_fenetre_sans_image_est_rejouee_en_partant_plus_tot() -> None:
    """MPEG-TS a images cles espacees de dix secondes : ffmpeg s'y positionne
    n'importe ou, et la fenetre de 95 % d'un fichier court peut ne rien rendre."""
    decodeur = _Decodeur(SAIN, SAIN, SAIN, SAIN, ("", 0, 0), ("", 96, 0))
    r = lecture.controler(Path("/f.ts"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.SAIN
    rejeu = decodeur.appels[5]
    assert rejeu[rejeu.index("-ss") + 1] == f"{6840.0 - lecture.RECUL:.3f}"
    assert rejeu[rejeu.index("-t") + 1] == f"{lecture.ECHANTILLON + lecture.RECUL:g}"
    # Les images comptees restent celles de la fenetre : la sortie saute le recul.
    apres_entree = rejeu[rejeu.index("-i") :]
    assert apres_entree[apres_entree.index("-ss") + 1] == f"{lecture.RECUL:.3f}"
    assert apres_entree[apres_entree.index("-t") + 1] == "4"


def test_un_fichier_tronque_reste_abime_apres_le_rejeu() -> None:
    decodeur = _Decodeur(SAIN, SAIN, SAIN, ("", 0, 0), ("", 0, 0), ("", 0, 0), ("", 0, 0))
    r = lecture.controler(Path("/f.mp4"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.ABIME
    assert r.position == 0.75
    assert r.premiere_erreur == "aucune image décodée à cet endroit"


def test_une_vraie_erreur_ne_declenche_pas_de_rejeu() -> None:
    tronque = ("[in#0/matroska,webm @ 0x1] File ended prematurely", 0, 0)
    decodeur = _Decodeur(SAIN, SAIN, SAIN, tronque, tronque)
    r = lecture.controler(Path("/f.mkv"), analyse=_film(), executer=decodeur, ffmpeg="ffmpeg")

    assert r.etat is EtatControle.ABIME
    assert len(decodeur.appels) == 5


# --- La fiche comparee : ni faux « grave », ni doublon ------------------------


def test_un_h264_10_bits_deja_dans_l_original_n_est_qu_une_information() -> None:
    """Remplacer ne rend le film ni plus ni moins lisible : le crier « grave »
    pousserait a garder un original qui a exactement le meme defaut."""
    original = _reencode(profile="High 10", pix_fmt="yuv420p10le")
    c = lecture.comparer(original, _reencode(profile="High 10", pix_fmt="yuv420p10le"))

    verdict = next(v for v in c.verdicts if v.code == "h264_10_bits")
    assert verdict.gravite is lecture.Gravite.INFO
    assert "comme l'original" in verdict.texte
    assert c.gravite != "grave"


def test_une_duree_trop_courte_n_est_dite_qu_une_fois_et_pas_en_erreur_de_decodage() -> None:
    controle = lecture.ResultatControle(
        EtatControle.ABIME,
        "Aucune erreur de décodage aux 5 points testés, mais durée de 1 min 40 s contre "
        "2 min 00 s pour l'original : le fichier est incomplet.",
        cause="duree",
    )
    court = analyse(_video(), duree="100.0")
    c = lecture.comparer(analyse(_video(), duree="120.0"), court, controle)

    assert _codes(c) == ["duree"]
    assert "erreur de décodage" not in c.phrase


def test_une_duree_vue_par_le_seul_controle_entre_dans_la_fiche() -> None:
    controle = lecture.ResultatControle(EtatControle.ABIME, "incomplet.", cause="duree")
    c = lecture.comparer(analyse(_video()), analyse(_video()), controle)

    assert _codes(c) == ["duree"]
    assert c.phrase == "Ne remplace pas l'original en l'état : durée incomplète."


def test_la_fiche_n_affirme_jamais_qu_un_fichier_est_intact() -> None:
    sain = lecture.ResultatControle(EtatControle.SAIN, "ok")
    c = lecture.comparer(_original_hdr10(), _original_hdr10(), sain)

    assert "aucune erreur de décodage aux 5 points testés (5, 25, 50, 75 et 95 %)" in c.phrase
    assert "sain" not in c.phrase and "intact" not in c.phrase


def test_un_controle_sur_la_duree_le_dit_dans_son_message() -> None:
    decodeur = _Decodeur(SAIN, SAIN, SAIN, SAIN, SAIN)
    r = lecture.controler(
        Path("/f.mkv"), analyse=_film(), duree_originale=7500.0, executer=decodeur, ffmpeg="f"
    )
    assert r.cause == "duree"
    assert r.message.startswith("Aucune erreur de décodage aux 5 points testés, mais durée")


# --- La premiere image lue, ou pas --------------------------------------------


def test_la_premiere_image_lue_est_notee() -> None:
    assert analyse(_hevc(), image=_image(color_transfer="smpte2084")).premiere_image_lue is True


def test_une_premiere_image_illisible_rend_l_analyse_incomplete() -> None:
    """Le HDR10 d'un MKV ecrit par ffmpeg ne vit souvent que dans la premiere
    image : sans elle, l'analyse dirait SDR sans le savoir."""
    a = analyse(_hevc(color_space="bt2020nc"), image=None)

    assert a.premiere_image_lue is False
    assert a.hdr is TypeHdr.SDR
    assert analyse(_hevc(), image={"frames": []}).premiere_image_lue is False


def test_une_analyse_incomplete_n_est_pas_gardee_en_memoire(monkeypatch, tmp_path) -> None:
    """La tentative suivante doit relire le fichier, pas ressortir la meme
    description incomplete."""
    fichier = tmp_path / "film.mkv"
    fichier.write_bytes(b"x")
    appels = []

    def sonder(chemin):
        appels.append(chemin)
        return analyse(_hevc(), image=None if len(appels) == 1 else _image())

    monkeypatch.setattr(lecture, "sonder", sonder)
    monkeypatch.setattr(lecture, "_ANALYSES", lecture.OrderedDict())

    assert lecture.analyser(fichier).premiere_image_lue is False
    assert lecture.analyser(fichier).premiere_image_lue is True
    assert lecture.analyser(fichier).premiere_image_lue is True
    assert len(appels) == 2


def test_l_apercu_converti_n_a_pas_d_images_b() -> None:
    """Trouve par le test de fumee DANS l'image : sous ffmpeg 7.1, le
    reordonnancement des images B de l'apercu, avec les horodatages d'origine,
    donnait des temps de decodage en double (« 219 >= 219 »)."""
    a = analyse(_hevc())
    argv = lecture.commande_hls("ffmpeg", Path("/f.mkv"), Path("/s"), a, lecture.decider(a), 0)

    assert argv[argv.index("-c:v") + 1] == "libx264", "c'est bien une conversion"
    assert argv[argv.index("-bf") + 1] == "0"
