"""Reprise de l'etat de travail apres redemarrage.

Un scan de mille fichiers coute plusieurs minutes de disque, et les plans qui
en decoulent des centaines d'appels reseau. Les reperdre a chaque mise a jour
d'image rend l'outil penible sur une vraie bibliotheque.

Ce qui est teste ici n'est pas « le JSON contient les bons champs » mais deux
proprietes qui, seules, decident si la reprise est fiable :

1. **L'aller-retour ne perd rien** de ce qui a coute cher. Un champ oublie dans
   l'encodeur ne leve aucune erreur — il revient a sa valeur par defaut, et le
   plan repris devient subtilement faux.
2. **Un instantane illisible ne casse pas le demarrage.** Format d'une version
   anterieure, champ disparu, valeur inattendue : tout cela ramene a « pas
   d'instantane ». Perdre une reprise est un desagrement, ne plus demarrer est
   une panne.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.parser import MediaKind, parse
from sortilege.core.planner import Plan
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile, ScanResult
from sortilege.core.scoring import Decision
from sortilege.core.snapshot import (
    VERSION,
    SnapshotError,
    plans_in,
    plans_out,
    scan_in,
    scan_out,
)
from sortilege.providers.base import Candidate

GB = 1024**3


def scanned(name: str = "Severance/Season 01/Severance.S01E01E02.1080p.mkv") -> ScannedFile:
    path = Path("/media") / name
    return ScannedFile(
        path=path,
        size_bytes=3 * GB,
        parsed=parse(path, list(Path(name).parts[:-1])),
        probe=FileProbe(
            duration_seconds=2712.5,
            width=1920,
            height=1080,
            video_codec="hevc",
            audio_languages=["fra", "eng"],
            tmdb_id="95396",
            nfo_title="Severance",
            probed=True,
        ),
        relative_path=name,
        in_library=False,
    )


def plan(**kw) -> Plan:
    base = dict(
        id="p1",
        source=Path("/media/dl/Severance.S01E01E02.mkv"),
        destination=Path("/media/lib/Series/Severance/Season 01/Severance - S01E01-E02.mkv"),
        kind="episode",
        score=0.91,
        decision=Decision.AUTO,
        reasons=["titre exact", "annee concordante"],
        title="Severance",
        year=2022,
        provider="tmdb",
        external_id="95396",
        poster_url="https://image.tmdb.org/t/p/w200/abc.jpg",
        companions=[(Path("/media/dl/sub.srt"), Path("/media/lib/x.srt"))],
        leftovers=[Path("/media/dl/rarbg.nfo")],
        alternatives=[
            Candidate(provider="tmdb", external_id="1", title="Autre", year=2015, popularity=0.4)
        ],
        manual=True,
    )
    base.update(kw)
    return Plan(**base)


# --- Aller-retour du scan ---------------------------------------------------


def test_un_scan_revient_identique() -> None:
    result = ScanResult(files=[scanned()], skipped=3, errors=["dossier illisible"])
    back, deep = scan_in(scan_out(result, deep=True))

    assert deep is True
    assert back.skipped == 3
    assert back.errors == ["dossier illisible"]
    assert back.total == 1


def test_ce_que_ffprobe_a_coute_survit() -> None:
    """C'est la partie chere : rouvrir mille conteneurs prend des minutes."""
    back, _ = scan_in(scan_out(ScanResult(files=[scanned()]), deep=True))
    probe = back.files[0].probe

    assert probe.probed is True
    assert probe.duration_seconds == 2712.5
    assert probe.video_codec == "hevc"
    assert probe.audio_languages == ["fra", "eng"]
    assert probe.tmdb_id == "95396"


def test_la_lecture_du_nom_survit_en_entier() -> None:
    back, _ = scan_in(scan_out(ScanResult(files=[scanned()]), deep=True))
    parsed = back.files[0].parsed

    assert parsed.title == "Severance"
    assert parsed.kind is MediaKind.EPISODE
    assert (parsed.season, parsed.episode, parsed.episode_end) == (1, 1, 2)
    assert parsed.resolution == "1080p"
    assert parsed.signals  # les signaux servent au scoring, pas a l'affichage


def test_un_fichier_deja_range_reste_marque() -> None:
    """Sans ce drapeau, un rescan proposerait de deplacer ce qui est en place."""
    f = scanned()
    f.in_library = True
    back, _ = scan_in(scan_out(ScanResult(files=[f]), deep=True))
    assert back.files[0].in_library is True


# --- Aller-retour des plans -------------------------------------------------


def test_un_plan_revient_identique() -> None:
    back = plans_in(plans_out([plan()]))[0]

    assert back.id == "p1"
    assert back.source == Path("/media/dl/Severance.S01E01E02.mkv")
    assert back.destination.name == "Severance - S01E01-E02.mkv"
    assert back.decision is Decision.AUTO
    assert back.score == 0.91
    assert back.reasons == ["titre exact", "annee concordante"]


def test_les_sous_titres_ne_sont_pas_perdus() -> None:
    """Les oublier ferait repartir la video sans eux : perte silencieuse."""
    back = plans_in(plans_out([plan()]))[0]
    assert back.companions == [(Path("/media/dl/sub.srt"), Path("/media/lib/x.srt"))]
    assert back.leftovers == [Path("/media/dl/rarbg.nfo")]


def test_les_candidats_alternatifs_survivent() -> None:
    """Ils n'existent que pour l'arbitrage humain : sans eux, la reprise
    presenterait un choix a faire sans les options."""
    back = plans_in(plans_out([plan()]))[0]
    assert len(back.alternatives) == 1
    assert back.alternatives[0].title == "Autre"


def test_un_choix_humain_reste_un_choix_humain() -> None:
    """Sinon la reprise reposerait une question deja tranchee."""
    assert plans_in(plans_out([plan(manual=True)]))[0].manual is True


def test_un_plan_sans_destination_survit() -> None:
    """Un plan en echec n'a pas de destination — et doit rester visible."""
    back = plans_in(plans_out([plan(destination=None, error="titre illisible")]))[0]
    assert back.destination is None
    assert back.error == "titre illisible"


def test_la_file_vide_est_un_etat_valide() -> None:
    assert plans_in(plans_out([])) == []


# --- Robustesse -------------------------------------------------------------


def test_un_schema_d_une_autre_version_est_refuse() -> None:
    """Le refus est le comportement voulu : mieux vaut recalculer que relire
    des champs qui ont change de sens."""
    payload = plans_out([plan()])
    payload["version"] = VERSION + 1
    with pytest.raises(SnapshotError):
        plans_in(payload)


@pytest.mark.parametrize("raw", [None, [], "texte", 42, {}])
def test_un_contenu_inattendu_est_refuse(raw) -> None:
    with pytest.raises(SnapshotError):
        scan_in(raw)


def test_un_champ_manquant_est_refuse_et_non_devine() -> None:
    """Deviner produirait un plan silencieusement faux, donc un fichier
    deplace au mauvais endroit."""
    payload = plans_out([plan()])
    del payload["plans"][0]["source"]
    with pytest.raises(SnapshotError):
        plans_in(payload)


def test_une_decision_inconnue_est_refusee() -> None:
    payload = plans_out([plan()])
    payload["plans"][0]["decision"] = "peut-etre"
    with pytest.raises(SnapshotError):
        plans_in(payload)


def test_un_type_de_media_inconnu_est_refuse() -> None:
    payload = scan_out(ScanResult(files=[scanned()]), deep=True)
    payload["files"][0]["parsed"]["kind"] = "podcast"
    with pytest.raises(SnapshotError):
        scan_in(payload)


def test_l_instantane_est_serialisable_en_json() -> None:
    """Il part dans une colonne TEXT : un Path non converti passerait les
    tests d'aller-retour en memoire et echouerait a l'ecriture."""
    import json

    json.dumps(plans_out([plan()]))
    json.dumps(scan_out(ScanResult(files=[scanned()]), deep=True))


# --- Reprise reelle, a travers la base --------------------------------------


@pytest.fixture
def memoire(tmp_path, monkeypatch):
    """Une base sur disque, substituee aux deux routeurs concernes."""
    from sortilege.api import library, review
    from sortilege.core.store import Store

    store = Store(tmp_path / "sortilege.db")
    monkeypatch.setattr(library, "get_memory", lambda: store)
    monkeypatch.setattr(review, "get_memory", lambda: store)
    return store


def test_un_scan_traverse_un_redemarrage(memoire) -> None:
    from sortilege.api import library

    library.adopt_scan(ScanResult(files=[scanned()], skipped=2), deep=True)

    # Ce que fait un redemarrage : la memoire du processus repart a zero.
    library._job.result = None
    assert library.last_scan() is None

    assert library.restore_scan() is True
    assert library.last_scan().total == 1
    assert library.last_scan().files[0].probe.probed is True


def test_les_plans_traversent_un_redemarrage(memoire) -> None:
    from sortilege.api import review

    review.adopt_plans([plan()])
    review._plans.clear()

    assert review.restore_plans() is True
    assert review._plans["p1"].title == "Severance"


def test_l_avancement_du_lot_est_repris(memoire) -> None:
    """Sans les chemins deja traites, « Traiter les 100 suivants » repartirait
    du premier lot au lieu d'avancer."""
    from sortilege.api import review

    review._job.done_paths = {"/media/a.mkv", "/media/b.mkv"}
    review.adopt_plans([plan()])

    review._plans.clear()
    review._job.done_paths.clear()

    review.restore_plans()
    assert review._job.done_paths == {"/media/a.mkv", "/media/b.mkv"}


def test_rien_a_reprendre_n_est_pas_une_erreur(memoire) -> None:
    from sortilege.api import library, review

    assert library.restore_scan() is False
    assert review.restore_plans() is False


def test_un_instantane_corrompu_ne_bloque_pas_le_demarrage(memoire) -> None:
    """Le cas d'une mise a jour qui change le schema : on repart de zero
    plutot que d'empecher le conteneur de demarrer."""
    from sortilege.api import library

    memoire.save_blob("scan", {"version": 999, "files": []})
    assert library.restore_scan() is False
