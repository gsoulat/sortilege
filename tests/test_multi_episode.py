"""Fichiers couvrant plusieurs episodes : « S01E01E02 ».

Un cas frequent des sorties televisees (double episode, pilote en deux parties)
qui casse trois choses a la fois si on ne le lit pas :

1. le second episode apparait comme MANQUANT alors qu'il est sur le disque ;
2. le fichier double et le fichier simple visent le meme nom de destination —
   le second ecrase le premier ;
3. les deux sont declares doublons l'un de l'autre.

Le motif doit donc etre teste AVANT le motif simple, sinon « S01E01E02 » est lu
comme « S01E01 » sans que rien ne le signale.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.collection import group
from sortilege.core.parser import parse
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile
from sortilege.core.template import PRESETS, render, validate

# --- Lecture du nom ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Serie.S01E01E02.1080p.mkv", (1, 1, 2)),
        ("Serie.S01E01-E02.mkv", (1, 1, 2)),
        ("Serie S01E01 E02.mkv", (1, 1, 2)),
        ("Serie.s02e09e10.FRENCH.mkv", (2, 9, 10)),
    ],
)
def test_un_fichier_double_est_lu_en_entier(name, expected) -> None:
    parsed = parse(Path(name), [])
    assert (parsed.season, parsed.episode, parsed.episode_end) == expected


def test_un_fichier_simple_n_a_pas_de_fin() -> None:
    """Non-regression : le motif multi ne doit pas capturer le cas courant."""
    parsed = parse(Path("Serie.S01E01.1080p.mkv"), [])
    assert (parsed.season, parsed.episode, parsed.episode_end) == (1, 1, None)


def test_le_titre_reste_propre() -> None:
    parsed = parse(Path("Severance.S01E01E02.1080p.WEB-DL.mkv"), [])
    assert parsed.title == "Severance"


def test_un_second_numero_anterieur_est_ignore() -> None:
    """« S01E05E02 » n'est pas une plage, c'est un nom bizarre.

    Retenir la plage produirait un range() vide et un nom de fichier absurde ;
    mieux vaut retomber sur la lecture simple."""
    parsed = parse(Path("Serie.S01E05E02.mkv"), [])
    assert parsed.episode == 5
    assert parsed.episode_end is None


# --- Nom de destination -----------------------------------------------------


def values(**kw) -> dict:
    base = {
        "title": "Severance",
        "year": 2022,
        "collection": "",
        "season": 1,
        "episode": 1,
        "episode_end": None,
        "episode_title": "",
        "resolution": None,
    }
    base.update(kw)
    return base


def test_la_plage_apparait_dans_le_nom() -> None:
    rendered = render(PRESETS["jellyfin"]["episode"], values(episode_end=2))
    assert rendered.endswith("Severance - S01E01-E02")


def test_un_episode_simple_garde_son_nom() -> None:
    rendered = render(PRESETS["jellyfin"]["episode"], values())
    assert rendered.endswith("Severance - S01E01")


def test_le_fichier_double_ne_vise_pas_le_meme_nom_que_le_simple() -> None:
    """C'est le point qui evite une perte de donnees silencieuse."""
    gabarit = PRESETS["jellyfin"]["episode"]
    assert render(gabarit, values(episode_end=2)) != render(gabarit, values())


def test_la_plage_est_zero_paddee_comme_le_reste() -> None:
    """« E01-E2 » a cote de « E01 » serait incoherent."""
    assert "E01-E02" in render(PRESETS["jellyfin"]["episode"], values(episode_end=2))


def test_les_gabarits_livres_restent_valides() -> None:
    for famille in PRESETS.values():
        for gabarit in famille.values():
            validate(gabarit)


def test_le_conditionnel_sans_padding_fonctionne_toujours() -> None:
    """Non-regression du moteur : le padding est optionnel."""
    assert render("{title}{? year: ($)}", {"title": "Dune", "year": 2024}) == "Dune (2024)"


# --- Inventaire -------------------------------------------------------------


def scanned(name: str) -> ScannedFile:
    path = Path("/media") / name
    return ScannedFile(
        path=path,
        size_bytes=2 * 1024**3,
        parsed=parse(path, []),
        probe=FileProbe(),
        relative_path=name,
    )


def test_le_second_episode_n_est_pas_porte_manquant() -> None:
    """Le grief concret : voir E02 dans la liste des manques alors qu'on l'a."""
    work = group([scanned("Severance.S01E01E02.mkv")])[0]
    assert work.seasons[1].owned == {1, 2}


def test_un_fichier_double_et_un_simple_ne_sont_pas_doublons() -> None:
    works = group([scanned("Severance.S01E01E02.mkv"), scanned("Severance.S01E01.mkv")])
    assert all(not w.duplicates for w in works)
