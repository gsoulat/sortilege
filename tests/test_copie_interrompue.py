"""Deux fichiers de tailles differentes : deux encodages, ou une copie coupee ?

Le cas reel : « la copie fait 3,31 Go, le fichier range 3,31 Go — ce n'est pas
le meme fichier ». L'arrondi cachait l'ecart, et le message se lisait comme une
contradiction. Derriere, deux situations que tout separe :

- deux encodages distincts, a arbitrer ;
- une copie INTERROMPUE, ou le plus gros contient tout — et ou « garder le plus
  petit » mettrait un film tronque en bibliotheque.
"""

from __future__ import annotations

import os
import random
from pathlib import Path

import pytest

from sortilege.core import quality
from sortilege.core.journal import (
    Journal,
    _permission_hint,
    apply_plan,
    delete_ranged_source,
    evacuate_ranged_source,
    keep_by_size,
    keep_by_strategy,
)
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision

MO = 1024 * 1024


@pytest.fixture
def espace(tmp_path: Path) -> Path:
    (tmp_path / "dl").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / ".corbeille").mkdir()
    return tmp_path


@pytest.fixture
def journal(espace: Path) -> Journal:
    return Journal(espace / "journal.jsonl")


def film(taille: int, graine: int = 1) -> bytes:
    """Des octets qui ne se repetent pas : une fenetre mal placee ne doit pas
    coincider par hasard, comme le ferait b"x" * n."""
    return random.Random(graine).randbytes(taille)  # noqa: S311 - donnees de test


def paire(espace: Path, copie: bytes, rangee: bytes) -> Plan:
    source = espace / "dl" / "Film.mkv"
    source.write_bytes(copie)
    destination = espace / "lib" / "Films" / "Film (2016).mkv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(rangee)
    return Plan(
        id="p1",
        source=source,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        title="Film",
        year=2016,
    )


def corbeille(espace: Path) -> Path:
    return espace / "lib" / ".corbeille"


# --- Le message -------------------------------------------------------------


def test_l_ecart_exact_est_donne_quand_l_arrondi_le_cache(espace: Path, journal: Journal) -> None:
    complet = film(3 * MO)
    retag = b"autre etiquette" + complet[15:] + b"1234567"
    plan = paire(espace, retag, complet)

    resultat = evacuate_ranged_source(plan, journal, corbeille(espace))

    assert resultat.reason == "size_mismatch"
    assert "écart : 7 octets" in resultat.message
    assert plan.source.read_bytes() == retag, "rien n'a ete touche"


# --- La copie interrompue ---------------------------------------------------


@pytest.mark.parametrize("taille", [700, 3 * MO + 12345])
def test_un_fichier_range_tronque_est_reconnu(espace: Path, journal: Journal, taille: int) -> None:
    complet = film(taille)
    plan = paire(espace, complet, complet[: taille - 300])

    resultat = evacuate_ranged_source(plan, journal, corbeille(espace))

    assert resultat.reason == "incomplete_copy"
    assert "le fichier rangé est le début exact" in resultat.message
    assert plan.source.read_bytes() == complet
    assert plan.destination.stat().st_size == taille - 300


def test_une_copie_tronquee_en_telechargement_est_reconnue(espace: Path, journal: Journal) -> None:
    complet = film(2 * MO)
    plan = paire(espace, complet[: MO + 1], complet)

    resultat = delete_ranged_source(plan)

    assert resultat.reason == "incomplete_copy"
    assert "la copie est le début exact" in resultat.message
    assert plan.source.is_file()


def test_deux_encodages_ne_passent_pas_pour_une_copie_coupee(
    espace: Path, journal: Journal
) -> None:
    """Meme longueur de debut, contenu different des l'en-tete."""
    plan = paire(espace, film(2 * MO, graine=1), film(2 * MO - 50, graine=2))

    assert evacuate_ranged_source(plan, journal, corbeille(espace)).reason == "size_mismatch"


def test_une_difference_au_milieu_suffit_a_ecarter_la_copie_coupee(
    espace: Path, journal: Journal
) -> None:
    complet = bytearray(film(4 * MO))
    rangee = bytearray(complet[: 4 * MO - 10])
    rangee[2 * MO] ^= 0xFF
    plan = paire(espace, bytes(complet), bytes(rangee))

    assert evacuate_ranged_source(plan, journal, corbeille(espace)).reason == "size_mismatch"


def test_un_fichier_vide_est_une_copie_coupee(espace: Path, journal: Journal) -> None:
    """Une copie tuee avant son premier octet laisse un fichier vide : c'est
    le cas le plus extreme de copie interrompue, pas un « autre encodage »."""
    complet = film(2 * MO)
    plan = paire(espace, complet, b"")

    resultat = evacuate_ranged_source(plan, journal, corbeille(espace))

    assert resultat.reason == "incomplete_copy"
    assert "le fichier rangé est vide" in resultat.message


def test_garder_le_plus_petit_ne_garde_jamais_un_fichier_vide(
    espace: Path, journal: Journal
) -> None:
    complet = film(2 * MO)
    plan = paire(espace, complet, b"")

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is False
    assert plan.source.read_bytes() == complet
    assert not any(corbeille(espace).rglob("*.mkv"))


# --- Arbitrer ne garde jamais le film tronque -------------------------------


def test_garder_le_plus_petit_refuse_un_film_tronque(espace: Path, journal: Journal) -> None:
    complet = film(2 * MO)
    plan = paire(espace, complet, complet[:MO])

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is False
    assert resultat.reason == "incomplete_copy"
    assert plan.source.read_bytes() == complet
    assert plan.destination.read_bytes() == complet[:MO]
    assert not any(corbeille(espace).rglob("*.mkv")), "rien en corbeille"


def test_garder_le_plus_gros_remet_le_film_entier(espace: Path, journal: Journal) -> None:
    complet = film(2 * MO)
    plan = paire(espace, complet, complet[:MO])

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="larger")

    assert resultat.ok is True
    assert plan.destination.read_bytes() == complet
    assert not plan.source.exists()
    ecartes = list(corbeille(espace).rglob("*.mkv"))
    assert len(ecartes) == 1 and ecartes[0].stat().st_size == MO


def test_garder_le_plus_petit_reste_permis_entre_deux_encodages(
    espace: Path, journal: Journal
) -> None:
    plan = paire(espace, film(MO, graine=3), film(2 * MO, graine=4))

    resultat = keep_by_size(plan, journal, corbeille(espace), keep="smaller")

    assert resultat.ok is True


def test_une_strategie_legere_ne_choisit_pas_le_film_tronque(
    espace: Path, journal: Journal
) -> None:
    complet = film(2 * MO)
    plan = paire(espace, complet, complet[:MO])
    leger = quality.Strategy(
        key="leger",
        label="Léger",
        summary="le plus petit",
        order=("1080p",),
        prefer_bigger=False,
    )

    resultat = keep_by_strategy(
        plan,
        journal,
        corbeille(espace),
        leger,
        candidate_resolution="1080p",
        incumbent_resolution="1080p",
    )

    assert resultat.reason == "incomplete_copy"
    assert plan.source.read_bytes() == complet


# --- Le conseil de permission regarde l'autre bout --------------------------


def _refus_simule(espace: Path, monkeypatch) -> tuple[Path, Path]:
    """Sortilege « tourne » sous une autre identite que le proprietaire des
    dossiers de test : exactement la situation du NAS."""
    source = espace / "dl" / "Film.mkv"
    source.write_bytes(b"x")
    bibliotheque = espace / "lib" / "Films" / "Film (2016).mkv"
    monkeypatch.setattr(os, "getuid", lambda: os.stat(espace).st_uid + 1)
    return source, bibliotheque


def test_puid_est_conseille_quand_la_bibliotheque_le_supporte(espace: Path, monkeypatch) -> None:
    source, bibliotheque = _refus_simule(espace, monkeypatch)
    proprietaire = os.stat(espace / "dl").st_uid

    message = _permission_hint(source, bibliotheque)

    assert f"Mets PUID={proprietaire}" in message


def test_puid_n_est_pas_conseille_s_il_bloquerait_la_bibliotheque(
    espace: Path, monkeypatch
) -> None:
    source, bibliotheque = _refus_simule(espace, monkeypatch)
    lib = espace / "lib"
    lib.chmod(0o555)
    try:
        message = _permission_hint(source, bibliotheque)
    finally:
        lib.chmod(0o755)

    assert "Mets PUID" not in message
    assert "Ne change PAS PUID/PGID" in message
    assert str(lib) in message, "le dossier qui bloquerait est nomme"
    assert "USER_ID/GROUP_ID" in message
    assert f"« {espace / 'dl'} »" in message, "c'est le dossier bloque qu'on reattribue"


def test_quand_la_bibliotheque_bloque_c_est_elle_qu_on_reattribue(
    espace: Path, monkeypatch
) -> None:
    """Le sens inverse : la bibliotheque est verrouillee, les telechargements
    appartiennent deja a Sortilege. Conseiller un chown des telechargements
    viserait le dossier qui marche."""
    source, bibliotheque = _refus_simule(espace, monkeypatch)
    bibliotheque.parent.mkdir(parents=True)
    dl = espace / "dl"
    dl.chmod(0o555)
    try:
        message = _permission_hint(bibliotheque.parent / "x", source)
    finally:
        dl.chmod(0o755)

    assert "Ne change PAS PUID/PGID" in message
    assert str(dl) in message, "le dossier qui bloquerait est nomme"
    assert f"« {espace / 'lib' / 'Films'} »" in message


def test_un_rangement_refuse_par_la_bibliotheque_designe_la_bibliotheque(
    espace: Path, journal: Journal
) -> None:
    """L'erreur systeme ne dit pas quel dossier a refuse. Le conseil portait
    toujours sur la source : une bibliotheque verrouillee envoyait chercher
    dans les telechargements, avec « l'identite concorde pourtant »."""
    source = espace / "dl" / "Film.mkv"
    source.write_bytes(film(1000))
    lib = espace / "lib"
    plan = Plan(
        id="p1",
        source=source,
        destination=lib / "Films" / "Film (2016).mkv",
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        title="Film",
        year=2016,
    )
    lib.chmod(0o555)
    try:
        resultat = apply_plan(plan, journal, dry_run=False)
    finally:
        lib.chmod(0o755)

    assert resultat.ok is False
    assert resultat.reason == "permission_denied"
    assert f"impossible d'écrire dans « {lib} »" in resultat.message
    assert "concorde pourtant" not in resultat.message
    assert source.is_file()
