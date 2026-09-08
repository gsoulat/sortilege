"""Tests de la detection de nouveaux fichiers.

Ce qui compte ici n'est pas de reperer un fichier nouveau — c'est trivial —
mais de ne PAS le prendre tant qu'il grossit encore. Un telechargement en cours
traite trop tot, c'est un fichier incomplet deplace et un journal d'annulation
qui pointe vers du vide.

Tous les tests pilotent l'horloge : attendre reellement deux minutes rendrait
la suite inutilisable, et la logique testee est celle des comparaisons, pas
celle de `time.time`.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sortilege.core.watch import Watcher

BIG = 60 * 1024 * 1024


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "dl"
    root.mkdir()
    return root


def write(path: Path, size: int = BIG, *, age_seconds: float = 0.0, now: float = 1000.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(size)
        handle.write(b"\0")
    stamp = now - age_seconds
    os.utime(path, (stamp, stamp))
    return path


def test_un_fichier_vu_une_seule_fois_n_est_pas_pris(source: Path) -> None:
    """Il faut DEUX observations pour comparer une taille."""
    write(source / "Film.2024.mkv", age_seconds=9999)
    watcher = Watcher(quiet_seconds=60)

    assert watcher.poll([source], now=1000.0) == []


def test_un_fichier_stable_est_pris_au_second_passage(source: Path) -> None:
    path = write(source / "Film.2024.mkv", age_seconds=9999)
    watcher = Watcher(quiet_seconds=60)

    watcher.poll([source], now=1000.0)
    assert watcher.poll([source], now=1060.0) == [path]


def test_un_fichier_qui_grossit_n_est_jamais_pris(source: Path) -> None:
    """Le cas qui compte : un telechargement en cours."""
    path = source / "EnCours.2024.mkv"
    write(path, size=BIG, age_seconds=9999)
    watcher = Watcher(quiet_seconds=60)

    watcher.poll([source], now=1000.0)
    write(path, size=BIG * 2, age_seconds=9999)  # il a grossi
    assert watcher.poll([source], now=1060.0) == []


def test_un_fichier_modifie_recemment_attend(source: Path) -> None:
    """Meme taille sur deux observations, mais ecrit il y a trois secondes :
    une ecriture lente peut donner deux mesures identiques par hasard."""
    write(source / "Film.2024.mkv", age_seconds=3)
    watcher = Watcher(quiet_seconds=120)

    watcher.poll([source], now=1000.0)
    assert watcher.poll([source], now=1001.0) == []


def test_il_finit_par_etre_pris_une_fois_calme(source: Path) -> None:
    path = source / "Film.2024.mkv"
    write(path, age_seconds=3)
    watcher = Watcher(quiet_seconds=120)

    watcher.poll([source], now=1000.0)
    watcher.poll([source], now=1001.0)
    # Le fichier n'a plus bouge : son mtime vieillit tout seul.
    assert watcher.poll([source], now=1200.0) == [path]


def test_un_fichier_deja_traite_n_est_pas_repropose(source: Path) -> None:
    """Sans cela, un fichier rejete relancerait une recherche a chaque cycle."""
    path = write(source / "Film.2024.mkv", age_seconds=9999)
    watcher = Watcher(quiet_seconds=60)

    watcher.poll([source], now=1000.0)
    assert watcher.poll([source], now=1060.0) == [path]

    watcher.mark_processed([path])
    assert watcher.poll([source], now=1120.0) == []


def test_un_fichier_revenu_apres_disparition_est_neuf(source: Path) -> None:
    path = write(source / "Film.2024.mkv", age_seconds=9999)
    watcher = Watcher(quiet_seconds=60)
    watcher.poll([source], now=1000.0)
    watcher.poll([source], now=1060.0)
    watcher.mark_processed([path])

    path.unlink()
    watcher.poll([source], now=1120.0)

    write(path, age_seconds=9999)
    watcher.poll([source], now=1180.0)
    assert watcher.poll([source], now=1240.0) == [path]


def test_plusieurs_fichiers_independants(source: Path) -> None:
    stable = write(source / "Stable.2024.mkv", size=BIG, age_seconds=9999)
    grossit = source / "Grossit.2024.mkv"
    write(grossit, size=BIG, age_seconds=9999)

    watcher = Watcher(quiet_seconds=60)
    watcher.poll([source], now=1000.0)
    write(grossit, size=BIG * 2, age_seconds=9999)

    assert watcher.poll([source], now=1060.0) == [stable]


def test_racine_absente_ne_leve_pas(tmp_path: Path) -> None:
    watcher = Watcher()
    assert watcher.poll([tmp_path / "nexiste-pas"], now=1000.0) == []


def test_oublier_un_fichier_le_rend_neuf(source: Path) -> None:
    path = write(source / "Film.2024.mkv", age_seconds=9999)
    watcher = Watcher(quiet_seconds=60)
    watcher.poll([source], now=1000.0)
    watcher.poll([source], now=1060.0)
    watcher.mark_processed([path])

    watcher.forget(path)
    watcher.poll([source], now=1120.0)
    assert watcher.poll([source], now=1180.0) == [path]
