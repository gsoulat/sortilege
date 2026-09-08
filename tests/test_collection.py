"""Tests du regroupement en oeuvres, des manques et des doublons.

Logique pure : aucun reseau, aucun disque. Ce qui est verifie ici, c'est
qu'on ne declare pas un doublon a tort — l'erreur couterait un fichier mis en
corbeille sans raison.
"""

from __future__ import annotations

from pathlib import Path

from sortilege.core.collection import SeasonHolding, fill_known_episodes, group
from sortilege.core.parser import parse
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile

GB = 1024**3


def scanned(
    relative: str, *, size: int = 2 * GB, ancestors: list[str] | None = None
) -> ScannedFile:
    path = Path("/media") / relative
    parts = ancestors if ancestors is not None else list(Path(relative).parts[:-1])
    return ScannedFile(
        path=path,
        size_bytes=size,
        parsed=parse(path, parts),
        probe=FileProbe(),
        relative_path=relative,
    )


# --- Regroupement -----------------------------------------------------------


def test_les_episodes_d_une_serie_forment_une_oeuvre() -> None:
    works = group(
        [
            scanned("Severance/Season 01/Severance.S01E01.mkv"),
            scanned("Severance/Season 01/Severance.S01E02.mkv"),
            scanned("Severance/Season 02/Severance.S02E01.mkv"),
        ]
    )
    assert len(works) == 1
    work = works[0]
    assert work.file_count == 3
    assert sorted(work.seasons) == [1, 2]
    assert work.seasons[1].owned == {1, 2}


def test_la_casse_ne_separe_pas_une_serie() -> None:
    works = group(
        [
            scanned("severance.S01E01.mkv"),
            scanned("Severance.S01E02.mkv"),
        ]
    )
    assert len(works) == 1


def test_un_film_et_une_serie_homonymes_restent_distincts() -> None:
    works = group([scanned("Dune.2024.mkv"), scanned("Dune.S01E01.mkv")])
    assert len(works) == 2
    assert {w.kind for w in works} == {"movie", "episode"}


# --- Doublons ---------------------------------------------------------------


def test_deux_fichiers_pour_le_meme_episode_sont_un_doublon() -> None:
    works = group(
        [
            scanned("Severance.S01E01.1080p.mkv", size=2 * GB),
            scanned("Severance.S01E01.2160p.mkv", size=8 * GB),
        ]
    )
    duplicates = works[0].duplicates
    assert len(duplicates) == 1
    assert duplicates[0].label == "S01E01"


def test_le_meilleur_est_celui_de_plus_haute_resolution() -> None:
    """La resolution prime sur la taille : un 2160p compresse vaut mieux
    qu'un 1080p volumineux."""
    works = group(
        [
            scanned("Film.2024.2160p.mkv", size=4 * GB),
            scanned("Film.2024.1080p.mkv", size=9 * GB),
        ]
    )
    best = works[0].duplicates[0].best
    assert best.resolution == "2160p"


def test_l_espace_recuperable_exclut_le_fichier_garde() -> None:
    works = group(
        [
            scanned("Film.2024.2160p.mkv", size=8 * GB),
            scanned("Film.2024.1080p.mkv", size=2 * GB),
            scanned("Film.2024.720p.mkv", size=1 * GB),
        ]
    )
    assert works[0].wasted_bytes == 3 * GB


def test_deux_episodes_differents_ne_sont_pas_un_doublon() -> None:
    works = group([scanned("Severance.S01E01.mkv"), scanned("Severance.S01E02.mkv")])
    assert works[0].duplicates == []


def test_deux_fichiers_sans_numero_ne_sont_pas_declares_doublons() -> None:
    """Sans numero d'episode, rien ne prouve qu'ils occupent la meme place —
    et un faux doublon coute un fichier mis en corbeille sans raison."""
    works = group(
        [
            scanned("Serie/Season 01/inconnu-a.mkv"),
            scanned("Serie/Season 01/inconnu-b.mkv"),
        ]
    )
    assert all(not w.duplicates for w in works)


# --- Manques ----------------------------------------------------------------


class Episode:
    def __init__(self, name: str, aired: bool = True) -> None:
        self.name = name
        self.aired = aired


def test_les_episodes_absents_sont_listes() -> None:
    season = SeasonHolding(number=1, owned={1, 2, 5})
    fill_known_episodes(season, {n: Episode(f"Episode {n}") for n in range(1, 7)})
    assert season.missing == [3, 4, 6]


def test_un_episode_non_diffuse_n_est_pas_manquant() -> None:
    """Sinon toute serie en cours afficherait des trous impossibles a combler,
    et la liste cesserait d'etre lisible."""
    season = SeasonHolding(number=1, owned={1, 2})
    fill_known_episodes(
        season,
        {
            1: Episode("Un"),
            2: Episode("Deux"),
            3: Episode("Trois", aired=False),
        },
    )
    assert season.missing == []


def test_une_saison_complete_est_signalee() -> None:
    season = SeasonHolding(number=1, owned={1, 2, 3})
    fill_known_episodes(season, {n: Episode(f"E{n}") for n in (1, 2, 3)})
    assert season.complete is True
    assert season.missing == []


def test_sans_information_du_fournisseur_aucun_manque() -> None:
    """Ne rien savoir n'est pas manquer : afficher des trous inventes serait
    pire que de ne rien afficher."""
    season = SeasonHolding(number=1, owned={1})
    fill_known_episodes(season, None)
    assert season.missing == []
    assert season.complete is False
