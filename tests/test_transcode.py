"""Reencodage differe : la nuit, un fichier a la fois, sans rien remplacer.

Ce module fait la seule chose que Sortilege ne sait pas defaire : il DEGRADE
volontairement de la qualite. Une annulation rendra le fichier d'origine tant
qu'il est en corbeille, jamais les details perdus a l'encodage.

Les tests portent donc presque tous sur des REFUS. Le cas nominal — ffmpeg
produit un fichier plus petit — n'a rien d'interessant ; ce qui compte, c'est
tout ce qui doit empecher un remplacement quand quelque chose a mal tourne.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from sortilege.core.journal import Journal
from sortilege.core.transcode import (
    DEBIT_PLANCHER,
    DUREE_TOLERANCE,
    Job,
    State,
    bitrate_for,
    build_command,
    discard,
    in_window,
    new_job,
    next_queued,
    purge_staging,
    replace,
    verify,
)

GO = 1024**3


@pytest.fixture
def espace(tmp_path: Path) -> Path:
    (tmp_path / "media" / "Films").mkdir(parents=True)
    (tmp_path / "media" / ".corbeille").mkdir()
    return tmp_path


def travail(espace: Path, *, source_octets: int = 10 * GO, sortie_octets: int = 3 * GO) -> Job:
    """Un reencodage termine, pret a etre verifie."""
    source = espace / "media" / "Films" / "Film (2024) [2160p].mkv"
    source.write_bytes(b"S" * 1000)
    sortie = espace / "media" / ".sortilege-reencodage" / "abc-1080p.mkv"
    sortie.parent.mkdir(parents=True, exist_ok=True)
    sortie.write_bytes(b"R" * 500)

    job = new_job(source, "Films/Film (2024) [2160p].mkv", "Film", "1080p", source_octets)
    job.state = State.DONE
    job.output = sortie
    job.output_bytes = sortie_octets
    job.source_duration = 7200.0
    job.output_duration = 7200.0
    return job


# --- La fenetre nocturne ----------------------------------------------------
#
# La plage traverse minuit dans le cas normal — 23 h a 7 h — ce qui interdit la
# comparaison naive « start <= h < end ». Lue ainsi, une plage 23-7 ne serait
# JAMAIS vraie et le reencodage n'aurait jamais lieu, sans rien pour
# l'expliquer.


@pytest.mark.parametrize("heure", [23, 0, 3, 6])
def test_la_nuit_traverse_minuit(heure: int) -> None:
    assert in_window(datetime(2026, 1, 1, heure), 23, 7) is True


@pytest.mark.parametrize("heure", [7, 12, 18, 22])
def test_le_jour_reste_hors_plage(heure: int) -> None:
    assert in_window(datetime(2026, 1, 1, heure), 23, 7) is False


def test_une_plage_ordinaire_fonctionne_aussi() -> None:
    assert in_window(datetime(2026, 1, 1, 14), 9, 18) is True
    assert in_window(datetime(2026, 1, 1, 20), 9, 18) is False


def test_une_plage_vide_n_interdit_rien() -> None:
    """Bornes egales : l'utilisateur ne veut pas de restriction horaire."""
    assert in_window(datetime(2026, 1, 1, 15), 0, 0) is True


# --- La commande ------------------------------------------------------------


def test_l_audio_et_les_sous_titres_sont_copies() -> None:
    """Reencoder l'audio couterait de la qualite sur les pistes VF/VO sans
    gagner de place. Perdre les sous-titres rendrait un fichier inutilisable
    pour qui regarde en VO."""
    commande = build_command(Path("/a.mkv"), Path("/b.mkv"), 1080)

    assert "-c:a" in commande
    assert commande[commande.index("-c:a") + 1] == "copy"
    assert commande[commande.index("-c:s") + 1] == "copy"


def test_la_largeur_est_forcee_paire() -> None:
    """Les encodeurs H.264 et HEVC refusent une largeur impaire : l'encodage
    echouerait des la premiere image."""
    commande = build_command(Path("/a.mkv"), Path("/b.mkv"), 720)

    assert commande[commande.index("-vf") + 1] == "scale=-2:720"


def test_les_pistes_optionnelles_ne_font_pas_echouer() -> None:
    """Un film sans sous-titres est le cas courant, pas une erreur."""
    commande = build_command(Path("/a.mkv"), Path("/b.mkv"), 1080)

    assert "0:s?" in commande
    assert "0:a?" in commande


def test_aucun_shell_n_est_implique() -> None:
    """Un nom de fichier contenant « ; rm -rf » ne doit jamais etre interprete."""
    commande = build_command(Path("/films/Film ; rm -rf ~.mkv"), Path("/b.mkv"), 1080)

    assert "/films/Film ; rm -rf ~.mkv" in commande, "le chemin reste UN argument"


# --- Ce qui interdit un remplacement ----------------------------------------


def test_un_resultat_correct_est_remplacable(espace: Path) -> None:
    ok, motif = verify(travail(espace))

    assert ok is True, motif


def test_un_encodage_interrompu_est_refuse(espace: Path) -> None:
    """LE test qui compte. Un encodage coupe produit un fichier plus court ET
    plus petit : sans controle de duree, il ressemble a une reussite
    particulierement efficace, et on remplacerait un film entier par ses vingt
    premieres minutes."""
    job = travail(espace, sortie_octets=1 * GO)
    job.output_duration = job.source_duration * 0.3

    ok, motif = verify(job)

    assert ok is False
    assert "interrompu" in motif


def test_un_ecart_de_duree_infime_reste_accepte(espace: Path) -> None:
    """Les conteneurs n'arrondissent pas la duree de la meme facon : refuser au
    centieme de seconde rejetterait des encodages parfaits."""
    job = travail(espace)
    job.output_duration = job.source_duration * (1 + DUREE_TOLERANCE / 2)

    assert verify(job)[0] is True


def test_un_gain_negligeable_est_refuse(espace: Path) -> None:
    """Perdre de la qualite pour trois pour cent de place est un mauvais
    marche — et l'utilisateur ne le verrait qu'apres coup."""
    job = travail(espace, source_octets=10 * GO, sortie_octets=97 * GO // 10)

    ok, motif = verify(job)

    assert ok is False
    assert "negligeable" in motif


def test_un_travail_non_termine_n_est_pas_remplacable(espace: Path) -> None:
    job = travail(espace)
    job.state = State.RUNNING

    assert verify(job)[0] is False


def test_un_original_disparu_arrete_tout(espace: Path) -> None:
    job = travail(espace)
    job.source.unlink()

    ok, motif = verify(job)

    assert ok is False
    assert "origine" in motif


def test_un_resultat_disparu_arrete_tout(espace: Path) -> None:
    job = travail(espace)
    job.output.unlink()

    assert verify(job)[0] is False


# --- Le remplacement --------------------------------------------------------


def test_l_original_part_en_corbeille_jamais_a_la_poubelle(espace: Path) -> None:
    """Un reencodage peut etre visuellement decevant sans que le controle
    automatique l'ait vu — cela ne se decouvre parfois qu'en regardant le
    film."""
    job = travail(espace)
    journal = Journal(espace / "journal.jsonl")

    ok, motif = replace(job, journal, espace / "media" / ".corbeille")

    assert ok is True, motif
    assert job.state is State.REPLACED
    en_corbeille = list((espace / "media" / ".corbeille").rglob("*.mkv"))
    assert len(en_corbeille) == 1
    assert en_corbeille[0].read_bytes().startswith(b"S"), "l'original est recuperable"


def test_le_remplacant_prend_la_place_de_l_original(espace: Path) -> None:
    job = travail(espace)
    journal = Journal(espace / "journal.jsonl")

    replace(job, journal, espace / "media" / ".corbeille")

    assert job.source.read_bytes().startswith(b"R"), "le fichier reencode occupe la place"


def test_le_remplacement_est_journalise_en_deux_temps(espace: Path) -> None:
    """Deux mouvements, deux lignes : l'annulation doit pouvoir defaire l'un
    puis l'autre, dans l'ordre inverse."""
    job = travail(espace)
    journal = Journal(espace / "journal.jsonl")

    replace(job, journal, espace / "media" / ".corbeille")

    lignes = journal.read_all()
    assert len(lignes) == 2
    assert [r.kind for r in lignes] == ["trash", "video"]


def test_sans_corbeille_rien_n_est_remplace(espace: Path) -> None:
    """Mieux vaut ne rien faire que d'ecarter sans filet."""
    job = travail(espace)

    ok, _ = replace(job, Journal(espace / "journal.jsonl"), None)

    assert ok is False
    assert job.source.read_bytes().startswith(b"S")


def test_un_echec_a_mi_chemin_restaure_l_original(espace: Path, monkeypatch) -> None:
    """Le pire scenario : l'original est deja ecarte quand l'installation
    echoue. Laisser la bibliotheque SANS le fichier serait pire que de n'avoir
    rien tente."""
    from sortilege.core import transcode as module

    job = travail(espace)
    vrai_move = module._move
    appels = {"n": 0}

    def move_capricieux(source, destination):
        appels["n"] += 1
        if appels["n"] == 2:
            raise PermissionError(13, "Permission denied")
        return vrai_move(source, destination)

    monkeypatch.setattr(module, "_move", move_capricieux)
    ok, _ = replace(job, Journal(espace / "journal.jsonl"), espace / "media" / ".corbeille")

    assert ok is False
    assert job.source.is_file(), "l'original a ete remis en place"
    assert job.source.read_bytes().startswith(b"S")


def test_jeter_un_resultat_ne_touche_pas_a_l_original(espace: Path) -> None:
    job = travail(espace)

    discard(job)

    assert job.state is State.DISCARDED
    assert not job.output.exists()
    assert job.source.is_file()


# --- La file ----------------------------------------------------------------


def test_un_seul_encodage_a_la_fois(espace: Path) -> None:
    """Deux ffmpeg en parallele sur un NAS ne vont pas deux fois plus vite : ils
    se disputent le processeur et rendent la machine inutilisable pour ce a quoi
    elle sert."""
    en_cours = travail(espace)
    en_cours.state = State.RUNNING
    attente = travail(espace)
    attente.state = State.QUEUED

    assert next_queued([en_cours, attente]) is None


def test_la_file_avance_dans_l_ordre_d_arrivee(espace: Path) -> None:
    premier, second = travail(espace), travail(espace)
    premier.state = second.state = State.QUEUED

    assert next_queued([premier, second]) is premier


def test_le_menage_efface_les_restes_d_un_redemarrage(espace: Path) -> None:
    """Un redemarrage pendant un encodage laisse un fichier partiel que plus
    rien ne reclame — et qui occupe la place qu'on cherchait a liberer."""
    job = travail(espace)
    orphelin = job.output.with_name("perdu-1080p.mkv")
    orphelin.write_bytes(b"x")

    efface = purge_staging(espace / "media", [job])

    assert efface == 1
    assert not orphelin.exists()
    assert job.output.is_file(), "le fichier d'un travail connu est preserve"


# --- Traduire un budget en consigne d'encodage ------------------------------
#
# « Un episode doit peser 500 Mo » ne veut rien dire pour ffmpeg. Ce qu'il
# comprend, c'est un debit. La conversion est la seule facon de tenir une
# consigne de POIDS : le mode a qualite constante, lui, produit le poids qu'il
# produit.


def test_un_budget_devient_un_debit() -> None:
    """500 Mo pour 45 minutes d'episode, moins l'audio qui est copie."""
    debit = bitrate_for(500 * 1024**2, 45 * 60)

    assert 1300 < debit < 1600, debit


def test_l_audio_est_deduit_du_budget() -> None:
    """L'audio est COPIE : son poids s'ajoute a celui de la video. L'oublier
    ferait viser un total superieur a la consigne, et le budget ne serait
    jamais tenu."""
    sans = bitrate_for(500 * 1024**2, 3600, audio_kbps=0)
    avec = bitrate_for(500 * 1024**2, 3600, audio_kbps=192)

    assert sans - avec == 192


def test_un_budget_absurde_ne_produit_pas_une_bouillie() -> None:
    """Mieux vaut depasser le budget et le dire que rendre un fichier
    inregardable en silence."""
    assert bitrate_for(10 * 1024**2, 2 * 3600) == DEBIT_PLANCHER


def test_sans_budget_on_reste_a_qualite_constante() -> None:
    assert bitrate_for(0, 3600) == 0
    assert bitrate_for(500 * 1024**2, 0) == 0


def test_le_mode_debit_borne_les_pics() -> None:
    """Sans maxrate, une scene chargee ferait depasser le budget que la moyenne
    respectait."""
    commande = build_command(Path("/a.mkv"), Path("/b.mkv"), 720, bitrate_kbps=1500)

    assert "-b:v" in commande
    assert commande[commande.index("-b:v") + 1] == "1500k"
    assert "-maxrate" in commande
    assert "-crf" not in commande, "les deux modes s'excluent"


def test_sans_debit_la_qualite_constante_reste_le_defaut() -> None:
    commande = build_command(Path("/a.mkv"), Path("/b.mkv"), 720, crf=21)

    assert "-crf" in commande
    assert "-b:v" not in commande
