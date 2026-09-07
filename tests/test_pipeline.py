"""Test d'integration : du fichier sur le disque au fichier range, et retour.

Les fournisseurs sont simules — pas pour eviter le reseau, mais parce qu'un
test qui dependrait de TMDB echouerait un jour pour une raison etrangere au
code. Ce qui est verifie ici est l'ENCHAINEMENT : scan, identification,
score, plan, deplacement reel, annulation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.journal import Journal, apply_plan, undo_last
from sortilege.core.pipeline import Pipeline
from sortilege.core.scanner import scan
from sortilege.core.scoring import Decision, Policy
from sortilege.core.template import PRESETS
from sortilege.providers.base import Candidate

BIG = 60 * 1024 * 1024


class FakeTMDB:
    """Repond ce qu'on lui dit de repondre, et compte ses appels."""

    name = "tmdb"

    def __init__(self, movies=None, series=None, episode_titles=None, collections=None):
        self.movies = movies or {}
        self.series = series or {}
        self.episode_titles = episode_titles or {}
        self.collections = collections or {}
        self.calls = 0

    async def search_movie(self, title, year):
        self.calls += 1
        return self.movies.get(title.lower(), [])

    async def search_series(self, title, year):
        self.calls += 1
        return self.series.get(title.lower(), [])

    async def get_episode_title(self, series_id, season, episode):
        return self.episode_titles.get((series_id, season, episode))

    async def get_collection(self, movie_id):
        return self.collections.get(movie_id)

    async def aclose(self):
        pass


def cand(**kw) -> Candidate:
    base = dict(provider="tmdb", external_id="1", title="X", year=2024, popularity=0.8)
    base.update(kw)
    return Candidate(**base)


@pytest.fixture
def tree(tmp_path: Path):
    src = tmp_path / "dl"
    lib = tmp_path / "media"
    src.mkdir()
    lib.mkdir()
    return src, lib


def big_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(BIG)
        handle.write(b"\0")
    return path


async def run(src: Path, lib: Path, tmdb: FakeTMDB):
    result = scan([src], deep=False, library_root=lib)
    pipeline = Pipeline(
        tmdb=tmdb,
        anilist=None,
        library_root=lib,
        templates={k: PRESETS["jellyfin"][k] for k in ("movie", "episode", "anime")},
        policy=Policy(auto_apply_threshold=0.92, reject_threshold=0.40),
    )
    try:
        return await pipeline.plan_all(result.files)
    finally:
        await pipeline.aclose()


@pytest.mark.asyncio
async def test_film_identifie_puis_range_puis_annule(tree) -> None:
    src, lib = tree
    source = big_file(src / "Dune.Part.Two.2024.1080p.BluRay.x265-GROUPE.mkv")

    # TMDB interroge en fr-FR renvoie le titre traduit ET le titre original.
    # C'est ce dernier qui permet de reconnaitre un fichier nomme a l'anglaise.
    tmdb = FakeTMDB(
        movies={
            "dune part two": [
                cand(
                    external_id="693134",
                    title="Dune, deuxième partie",
                    original_title="Dune: Part Two",
                    year=2024,
                )
            ]
        },
        collections={"693134": "Dune - Saga"},
    )

    plans = await run(src, lib, tmdb)
    assert len(plans) == 1
    plan = plans[0]

    # La saga produit bien un niveau de dossier supplementaire.
    assert plan.decision is Decision.AUTO
    assert "Dune - Saga" in str(plan.destination)
    assert plan.destination.suffix == ".mkv"

    journal = Journal(lib.parent / "journal.jsonl")
    result = apply_plan(plan, journal, dry_run=False)
    assert result.ok
    assert not source.exists()
    assert plan.destination.is_file()

    undone = undo_last(journal, 1)
    assert undone[0].ok
    assert source.is_file()
    assert not plan.destination.exists()


@pytest.mark.asyncio
async def test_titre_traduit_seul_part_en_revue(tree) -> None:
    """Sans le titre original, « Dune Part Two » ne ressemble pas assez a
    « Dune, deuxième partie » pour etre applique sans regard humain.

    Le comportement est voulu : l'outil ne bluffe pas quand il ne reconnait
    qu'a moitie. C'est aussi la raison d'etre de Candidate.all_titles().
    """
    src, lib = tree
    big_file(src / "Dune.Part.Two.2024.1080p.mkv")

    tmdb = FakeTMDB(
        movies={
            "dune part two": [cand(external_id="693134", title="Dune, deuxième partie", year=2024)]
        },
    )

    plans = await run(src, lib, tmdb)
    assert plans[0].decision is Decision.REVIEW


@pytest.mark.asyncio
async def test_serie_rangee_par_saison(tree) -> None:
    src, lib = tree
    big_file(src / "Severance" / "Season 02" / "07.mkv")

    tmdb = FakeTMDB(
        series={"severance": [cand(external_id="95396", title="Severance", year=2022)]},
        episode_titles={("95396", 2, 7): "Chikhai Bardo"},
    )

    plans = await run(src, lib, tmdb)
    assert len(plans) == 1
    destination = str(plans[0].destination)

    # Saison lue sur le dossier, titre d'episode ramene du fournisseur.
    assert "Severance (2022)" in destination
    assert "Season 02" in destination
    assert "S02E07" in destination
    assert "Chikhai Bardo" in destination


@pytest.mark.asyncio
async def test_episode_inexistant_chez_le_candidat_bloque_l_automatique(tree) -> None:
    """La saison 9 n'existe pas : l'identification est douteuse."""
    src, lib = tree
    big_file(src / "Severance.S09E99.mkv")

    tmdb = FakeTMDB(
        series={"severance": [cand(external_id="95396", title="Severance", year=2022)]},
        episode_titles={},  # aucune reponse pour S09E99
    )

    plans = await run(src, lib, tmdb)
    assert plans[0].decision is not Decision.AUTO


@pytest.mark.asyncio
async def test_fichier_non_identifie_est_rejete_avec_un_motif(tree) -> None:
    src, lib = tree
    big_file(src / "Inconnu.Total.2024.mkv")

    plans = await run(src, lib, FakeTMDB())

    assert plans[0].decision is Decision.REJECT
    assert plans[0].destination is None
    assert any("aucun candidat" in r for r in plans[0].reasons)


@pytest.mark.asyncio
async def test_les_fichiers_deja_ranges_sont_ignores(tree) -> None:
    """Rescanner sa bibliotheque ne doit pas produire de plans vides."""
    _, lib = tree
    big_file(lib / "Films" / "Interstellar (2014)" / "Interstellar (2014).mkv")

    result = scan([lib], deep=False, library_root=lib)
    assert result.total == 1
    assert result.files[0].in_library is True

    pipeline = Pipeline(
        tmdb=FakeTMDB(),
        anilist=None,
        library_root=lib,
        templates={k: PRESETS["jellyfin"][k] for k in ("movie", "episode", "anime")},
        policy=Policy(),
    )
    try:
        plans = await pipeline.plan_all(result.files)
    finally:
        await pipeline.aclose()

    assert plans == []


@pytest.mark.asyncio
async def test_un_fichier_en_erreur_n_arrete_pas_le_lot(tree) -> None:
    src, lib = tree
    big_file(src / "Dune.2024.mkv")
    big_file(src / "Autre.2020.mkv")

    class Explosif(FakeTMDB):
        async def search_movie(self, title, year):
            if "autre" in title.lower():
                raise RuntimeError("fournisseur casse")
            return [cand(external_id="1", title="Dune", year=2024)]

    plans = await run(src, lib, Explosif())

    assert len(plans) == 2
    # Celui qui a echoue est rejete, l'autre est bien planifie.
    assert any(p.decision is Decision.AUTO for p in plans)
    assert any(p.decision is Decision.REJECT for p in plans)


@pytest.mark.asyncio
async def test_la_simulation_ne_deplace_rien(tree) -> None:
    src, lib = tree
    source = big_file(src / "Dune.2024.mkv")
    tmdb = FakeTMDB(movies={"dune": [cand(external_id="1", title="Dune", year=2024)]})

    plans = await run(src, lib, tmdb)
    journal = Journal(lib.parent / "journal.jsonl")
    result = apply_plan(plans[0], journal, dry_run=True)

    assert result.ok and result.simulated
    assert source.is_file()
    assert journal.read_all() == []
