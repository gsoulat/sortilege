"""Tests du cycle automatique.

C'est le chemin le plus risque du projet : la seule voie ou des fichiers
bougent sans personne devant l'ecran. Ce qui est verifie ici est surtout ce
qu'il REFUSE de faire — traiter un fichier instable, deplacer sans y avoir ete
autorise, ou toucher a ce qui attend un arbitrage.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from sortilege.api import automation, review
from sortilege.core.planner import Plan
from sortilege.core.preferences import AutomationSettings, Preferences
from sortilege.core.scoring import Decision

GB = 1024**3


class FakePipeline:
    """Renvoie des plans prepares. Le pipeline lui-meme est teste ailleurs."""

    instances: ClassVar[list] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        FakePipeline.instances.append(self)

    async def plan_all(self, files, on_progress=None):
        return [
            Plan(
                id=f"p{i}",
                source=f.path,
                destination=f.path.parent.parent / "media" / f.path.name,
                kind="movie",
                score=0.99 if i == 0 else 0.6,
                decision=Decision.AUTO if i == 0 else Decision.REVIEW,
            )
            for i, f in enumerate(files)
        ]

    async def aclose(self) -> None:
        pass


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """Une source, une bibliotheque, et un magasin de preferences maitrise."""
    source = tmp_path / "dl"
    library = tmp_path / "media"
    source.mkdir()
    library.mkdir()

    conf = automation.get_settings()
    monkeypatch.setattr(conf, "library_root", library)
    monkeypatch.setattr(conf, "tmdb_api_key", "cle-de-test")

    prefs = Preferences()

    class Store:
        def load(self):
            return prefs

        def resolved_sources(self):
            return [source]

        def destination_root(self, kind, size=None):
            return library

    # Une seule instance : « lambda: Store() » en creerait une par appel, et
    # un monkeypatch sur l'une n'atteindrait jamais celle qu'utilise le cycle.
    store = Store()
    monkeypatch.setattr(automation, "get_store", lambda: store)
    monkeypatch.setattr(automation, "Pipeline", FakePipeline)
    monkeypatch.setattr(automation, "TMDBProvider", lambda *a, **k: None)
    monkeypatch.setattr(automation, "AniListProvider", lambda *a, **k: None)

    FakePipeline.instances.clear()
    automation._watcher.__init__()
    review._plans.clear()

    return source, library, prefs, store


def big(path: Path, *, size: int = 2 * GB) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(size)
        handle.write(b"\0")
    return path


async def test_sans_nouveau_fichier_rien_ne_se_passe(env) -> None:
    """Le premier passage ne fait qu'observer : un fichier doit avoir ete vu
    deux fois pour qu'on puisse comparer sa taille."""
    source, _, _, _ = env
    big(source / "Dune.2024.mkv")

    report = await automation.run_cycle()

    assert report.detected == 0
    assert report.planned == 0
    assert FakePipeline.instances == []


async def test_le_mode_force_traite_meme_sans_nouveaute(env) -> None:
    """« Lancer maintenant » doit agir, sinon le bouton ne sert a rien."""
    source, _, _, _ = env
    big(source / "Dune.2024.mkv")

    report = await automation.run_cycle(forced=True)

    assert report.scanned == 1
    assert report.planned == 1


async def test_par_defaut_aucun_fichier_n_est_deplace(env) -> None:
    """apply_auto est desactive : la boucle prepare la file et s'arrete la."""
    source, _, prefs, _ = env
    path = big(source / "Dune.2024.mkv")
    prefs.automation = AutomationSettings(enabled=True, apply_auto=False)

    report = await automation.run_cycle(forced=True)

    assert report.applied == 0
    assert path.is_file(), "un fichier a bouge alors que l'application est desactivee"
    assert "desactivee" in report.message


async def test_avec_apply_auto_seuls_les_plans_surs_bougent(env) -> None:
    """Ce qui attend un arbitrage ne doit jamais partir tout seul — c'est la
    promesse centrale de l'outil."""
    source, library, prefs, _ = env
    sur = big(source / "Dune.2024.mkv")
    doute = big(source / "Inconnu.2020.mkv")
    prefs.automation = AutomationSettings(enabled=True, apply_auto=True)

    report = await automation.run_cycle(forced=True)

    assert report.applied == 1
    assert not sur.exists(), "le plan sur n'a pas ete applique"
    assert doute.is_file(), "un plan en attente d'arbitrage a ete deplace"
    assert (library / "Dune.2024.mkv").is_file()


async def test_les_plans_en_attente_restent_dans_la_file(env) -> None:
    source, _, prefs, _ = env
    big(source / "Dune.2024.mkv")
    big(source / "Inconnu.2020.mkv")
    prefs.automation = AutomationSettings(enabled=True, apply_auto=True)

    await automation.run_cycle(forced=True)

    restants = [p for p in review._plans.values()]
    assert len(restants) == 1
    assert restants[0].decision is Decision.REVIEW


async def test_sans_source_le_cycle_le_dit(env, monkeypatch) -> None:
    _, _, _, store = env
    monkeypatch.setattr(store, "resolved_sources", lambda: [])
    report = await automation.run_cycle(forced=True)
    assert "aucune source" in report.message


async def test_sans_cle_tmdb_le_cycle_le_dit(env, monkeypatch) -> None:
    """Sans fournisseur il n'y a aucun candidat : le dire vaut mieux que de
    produire une file vide sans explication."""
    source, _, _, _ = env
    big(source / "Dune.2024.mkv")
    monkeypatch.setattr(automation.get_settings(), "tmdb_api_key", "")

    report = await automation.run_cycle(forced=True)

    assert "TheMovieDB" in report.message


async def test_le_rapport_compte_ce_qui_attend(env) -> None:
    source, _, prefs, _ = env
    big(source / "Dune.2024.mkv")
    big(source / "Inconnu.2020.mkv")
    prefs.automation = AutomationSettings(enabled=True, apply_auto=False)

    report = await automation.run_cycle(forced=True)

    assert report.queued == 1
