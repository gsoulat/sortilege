"""Tests du confinement des chemins.

C'est la derniere barriere avant l'ecriture disque : si elle cede, une
metadonnee hostile ecrit hors de la bibliotheque.
"""

from pathlib import Path

import pytest

from sortilege.core.safety import (
    PathConfinementError,
    assert_readable_source,
    resolve_within,
    sanitize_segment,
)

ROOT = Path("/storage/media")


def test_resolution_nominale() -> None:
    out = resolve_within(ROOT, "Films/Dune (2024)/Dune (2024).mkv")
    assert out == Path("/storage/media/Films/Dune (2024)/Dune (2024).mkv")


def test_remontee_neutralisee() -> None:
    """Les segments « .. » sont retires, pas interpretes."""
    out = resolve_within(ROOT, "../../etc/passwd")
    assert ROOT in out.parents
    assert out == Path("/storage/media/etc/passwd")


def test_remontee_intercalee_neutralisee() -> None:
    out = resolve_within(ROOT, "Films/../../../root/.ssh/authorized_keys")
    assert ROOT in out.parents


def test_chemin_vide_rejete() -> None:
    with pytest.raises(PathConfinementError):
        resolve_within(ROOT, "../..")


def test_le_resultat_reste_toujours_sous_la_racine() -> None:
    for hostile in ["../x", "a/../../b", "./../../etc", "..%2F..%2Fetc"]:
        out = resolve_within(ROOT, hostile)
        assert ROOT in out.parents


# --- Assainissement de segment ----------------------------------------------


def test_separateurs_retires() -> None:
    assert "/" not in sanitize_segment("a/b")
    assert "\\" not in sanitize_segment("a\\b")


def test_point_et_espace_finaux_retires() -> None:
    """Un segment finissant par « . » ou espace est illisible en SMB."""
    assert sanitize_segment("dossier.") == "dossier"
    assert sanitize_segment("dossier ") == "dossier"


def test_noms_reserves_windows_prefixes() -> None:
    assert sanitize_segment("CON").startswith("_")
    assert sanitize_segment("COM1").startswith("_")


def test_segment_vide_devient_placeholder() -> None:
    assert sanitize_segment("") == "_"
    assert sanitize_segment("...") == "_"


def test_troncature_des_noms_trop_longs() -> None:
    out = sanitize_segment("x" * 500)
    assert len(out) <= 180


def test_override_droite_a_gauche_retire() -> None:
    """Caractere d'inversion visuelle : masque la vraie extension."""
    assert "‮" not in sanitize_segment("facture‮gpj.exe")


def test_unicode_legitime_preserve() -> None:
    assert sanitize_segment("Amélie Poulain") == "Amélie Poulain"
    # Le « 。 » final est un point ideographique (U+3002), pas un point ASCII :
    # il ne pose aucun probleme de systeme de fichiers et doit etre conserve.
    assert sanitize_segment("君の名は。") == "君の名は。"


# --- Sources -----------------------------------------------------------------


def test_source_hors_racine_rejetee(tmp_path: Path) -> None:
    autorise = tmp_path / "downloads"
    autorise.mkdir()
    dehors = tmp_path / "ailleurs"
    dehors.mkdir()
    fichier = dehors / "x.mkv"
    fichier.touch()

    with pytest.raises(PathConfinementError):
        assert_readable_source(fichier, [autorise])


def test_source_dans_racine_acceptee(tmp_path: Path) -> None:
    autorise = tmp_path / "downloads"
    autorise.mkdir()
    fichier = autorise / "x.mkv"
    fichier.touch()

    assert assert_readable_source(fichier, [autorise]) == fichier.resolve()
