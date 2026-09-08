"""Tests de la seconde passe assistee par IA.

Le resolveur est simule : ce qui est verifie ici n'est pas la qualite du
modele, mais la PLACE qu'on lui donne — quand il est appele, ce qu'on fait de
sa reponse, et surtout ce qu'on lui refuse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FakeTMDB, big_file, cand

from sortilege.core.ai_resolver import AIProposal
from sortilege.core.pipeline import Pipeline
from sortilege.core.scanner import scan
from sortilege.core.scoring import Decision, Policy
from sortilege.core.template import PRESETS


class FakeAI:
    """Renvoie des propositions preparees et enregistre ce qu'on lui soumet."""

    def __init__(self, proposals: dict[int, AIProposal] | None = None, boom: bool = False):
        self._proposals = proposals or {}
        self.boom = boom
        self.seen: list[list] = []

    def resolve(self, items):
        self.seen.append(list(items))
        if self.boom:
            raise RuntimeError("API indisponible")
        return {i.index: self._proposals[i.index] for i in items if i.index in self._proposals}


@pytest.fixture
def tree(tmp_path: Path):
    src = tmp_path / "dl"
    lib = tmp_path / "media"
    src.mkdir()
    lib.mkdir()
    return src, lib


async def run(src: Path, lib: Path, tmdb, ai=None):
    result = scan([src], deep=False, library_root=lib)
    pipeline = Pipeline(
        tmdb=tmdb,
        anilist=None,
        library_root=lib,
        templates={k: PRESETS["jellyfin"][k] for k in ("movie", "episode", "anime")},
        policy=Policy(auto_apply_threshold=0.92, reject_threshold=0.40),
        ai=ai,
        ai_batch_size=12,
    )
    try:
        return await pipeline.plan_all(result.files)
    finally:
        await pipeline.aclose()


async def test_l_ia_n_est_pas_appelee_sur_ce_qui_est_deja_sur(tree) -> None:
    """Le tout-venant ne doit rien couter : c'est le principe de la seconde passe."""
    src, lib = tree
    big_file(src / "Dune.2024.mkv")
    tmdb = FakeTMDB(movies={"dune": [cand(external_id="1", title="Dune", year=2024)]})
    ai = FakeAI()

    plans = await run(src, lib, tmdb, ai)

    assert plans[0].decision is Decision.AUTO
    assert ai.seen == []


async def test_l_ia_est_appelee_sur_les_cas_ambigus(tree) -> None:
    src, lib = tree
    big_file(src / "vbt.2009.mkv")
    ai = FakeAI()

    await run(src, lib, FakeTMDB(), ai)

    assert len(ai.seen) == 1
    assert ai.seen[0][0].filename == "vbt.2009.mkv"


async def test_le_titre_propose_relance_une_recherche(tree) -> None:
    """Le modele fournit une meilleure REQUETE, pas la reponse : les candidats
    viennent toujours du fournisseur."""
    src, lib = tree
    big_file(src / "vbt.2009.mkv")

    tmdb = FakeTMDB(
        movies={
            # Introuvable sous le nom du fichier...
            "vbt": [],
            # ...mais trouvable sous le titre que le modele propose.
            "very bad trip": [
                cand(
                    external_id="18785",
                    title="Very Bad Trip",
                    original_title="The Hangover",
                    year=2009,
                )
            ],
        }
    )
    ai = FakeAI(
        {0: AIProposal(index=0, kind="movie", title="Very Bad Trip", year=2009, confidence=0.95)}
    )

    plans = await run(src, lib, tmdb, ai)

    assert plans[0].title == "Very Bad Trip"
    assert plans[0].destination is not None
    assert "Very Bad Trip" in str(plans[0].destination)


async def test_la_confiance_du_modele_plafonne_le_score(tree) -> None:
    """Un modele hesitant ne doit pas produire une application automatique."""
    src, lib = tree
    big_file(src / "vbt.2009.mkv")

    tmdb = FakeTMDB(
        movies={
            "vbt": [],
            "very bad trip": [cand(external_id="1", title="Very Bad Trip", year=2009)],
        }
    )
    ai = FakeAI(
        {0: AIProposal(index=0, kind="movie", title="Very Bad Trip", year=2009, confidence=0.5)}
    )

    plans = await run(src, lib, tmdb, ai)

    assert plans[0].score <= 0.5
    assert plans[0].decision is not Decision.AUTO


async def test_une_proposition_moins_bonne_ne_degrade_pas(tree) -> None:
    """La seconde passe ne peut qu'ameliorer : sinon un modele qui se trompe
    ferait perdre une identification deterministe deja correcte."""
    src, lib = tree
    big_file(src / "Severance.2022.mkv")

    tmdb = FakeTMDB(
        movies={
            "severance": [cand(external_id="1", title="Severance", year=2022, popularity=0.9)],
            "nawak": [cand(external_id="2", title="Nawak", year=1999, popularity=0.1)],
        }
    )
    ai = FakeAI({0: AIProposal(index=0, kind="movie", title="Nawak", year=1999, confidence=0.3)})

    plans_sans = await run(src, lib, tmdb)
    plans_avec = await run(src, lib, tmdb, ai)

    assert plans_avec[0].title == plans_sans[0].title
    assert plans_avec[0].score >= plans_sans[0].score


async def test_une_panne_du_modele_laisse_le_lot_intact(tree) -> None:
    """Le resolveur est un bonus : son indisponibilite ne casse rien."""
    src, lib = tree
    big_file(src / "vbt.2009.mkv")

    plans_sans = await run(src, lib, FakeTMDB())
    plans_avec = await run(src, lib, FakeTMDB(), FakeAI(boom=True))

    assert len(plans_avec) == len(plans_sans)
    assert plans_avec[0].decision is plans_sans[0].decision


async def test_proposition_vide_ignoree(tree) -> None:
    """Le modele qui avoue ne pas savoir ne doit rien declencher."""
    src, lib = tree
    big_file(src / "vbt.2009.mkv")
    ai = FakeAI({0: AIProposal(index=0, kind="unknown", title="", confidence=0.1)})

    plans = await run(src, lib, FakeTMDB(), ai)

    assert plans[0].decision is Decision.REJECT


async def test_les_appels_sont_groupes_par_lot(tree) -> None:
    """Un appel par fichier couterait dix fois le prix d'un appel groupe."""
    src, lib = tree
    for i in range(5):
        big_file(src / f"inconnu{i}.2020.mkv")
    ai = FakeAI()

    await run(src, lib, FakeTMDB(), ai)

    assert len(ai.seen) == 1
    assert len(ai.seen[0]) == 5


async def test_sans_resolveur_le_pipeline_est_inchange(tree) -> None:
    src, lib = tree
    big_file(src / "Dune.2024.mkv")
    tmdb = FakeTMDB(movies={"dune": [cand(external_id="1", title="Dune", year=2024)]})

    plans = await run(src, lib, tmdb, ai=None)

    assert plans[0].decision is Decision.AUTO
