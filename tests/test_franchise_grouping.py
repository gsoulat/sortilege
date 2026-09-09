"""Regroupement des series d'une meme franchise, en situation.

TMDB ne declare aucun lien entre « Star Trek », « Star Trek: Discovery » et
« Star Trek: Picard » : trois series sans rapport pour lui. Le jeton
{collection} du prereglage Jellyfin promettait donc un regroupement que rien
n'alimentait — un dossier de franchise qui n'apparaissait jamais.

Le point qui demande une seconde passe : « Star Trek » tout court n'a rien
d'une franchise tant qu'on ignore que « Star Trek: Discovery » existe a cote.
La deduction ne peut donc pas se faire fichier par fichier ; elle attend que
tous les titres du lot soient resolus.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from helpers import FakeTMDB

from sortilege.core.franchise import derive_franchise, franchise_for
from sortilege.core.parser import parse
from sortilege.core.pipeline import Pipeline
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile
from sortilege.core.scoring import Policy
from sortilege.providers.base import Candidate

TEMPLATE = "{collection}/{title}/Season {season:02}/{title} - S{season:02}E{episode:02}"


# --- La deduction seule -----------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Star Trek: Discovery", "Star Trek"),
        ("Star Trek: Picard", "Star Trek"),
        ("Star Trek: Strange New Worlds", "Star Trek"),
    ],
)
def test_un_sous_titre_designe_la_franchise(title, expected) -> None:
    assert derive_franchise(title) == expected


def test_une_serie_sans_sous_titre_n_a_pas_de_franchise() -> None:
    assert derive_franchise("Severance") is None


def test_la_serie_d_origine_rejoint_sa_propre_franchise() -> None:
    """« Star Trek » (1966) doit atterrir DANS le dossier Star Trek, pas a
    cote. C'est le cas que la deduction seule ne peut pas voir : le titre ne
    porte aucun sous-titre, seule la presence des autres series le revele."""
    voisins = ["Star Trek: Discovery", "Star Trek: Picard"]
    assert franchise_for("Star Trek", [*voisins, "Star Trek"]) == "Star Trek"


def test_une_serie_seule_ne_cree_pas_de_dossier_de_franchise() -> None:
    """Un dossier a un seul element n'est pas un regroupement, c'est un niveau
    de plus a traverser."""
    assert franchise_for("The Witcher: Blood Origin", ["Severance", "Dune"]) is None


def test_deux_series_de_la_meme_franchise_suffisent() -> None:
    voisins = ["Star Trek: Discovery", "Star Trek: Picard"]
    assert franchise_for("Star Trek: Discovery", voisins) == "Star Trek"


# --- Le regroupement au bout du pipeline ------------------------------------


def scanned(name: str) -> ScannedFile:
    path = Path("/dl") / name
    return ScannedFile(
        path=path,
        size_bytes=3 * 1024**3,
        parsed=parse(path, []),
        probe=FileProbe(),
        relative_path=name,
    )


def series(title: str, tmdb_id: str) -> Candidate:
    return Candidate(
        provider="tmdb",
        external_id=tmdb_id,
        title=title,
        year=2020,
        popularity=0.9,
        kind="episode",
    )


def make_pipeline(tmdb: FakeTMDB, tmp_path: Path, known: set[str] | None = None) -> Pipeline:
    return Pipeline(
        tmdb=tmdb,
        anilist=None,
        library_root=tmp_path,
        templates={"episode": TEMPLATE, "movie": "{title}", "anime": "{title}"},
        policy=Policy(auto_apply_threshold=0.5, reject_threshold=0.1),
        known_titles=known,
    )


async def test_les_star_trek_d_un_meme_lot_se_regroupent(tmp_path: Path) -> None:
    """Le cas concret : trois series importees ensemble doivent partager un
    dossier, alors qu'aucun fournisseur ne les relie."""
    tmdb = FakeTMDB(
        series={
            "star trek discovery": [series("Star Trek: Discovery", "1")],
            "star trek picard": [series("Star Trek: Picard", "2")],
        }
    )
    pipeline = make_pipeline(tmdb, tmp_path)

    plans = await pipeline.plan_all(
        [
            scanned("Star.Trek.Discovery.S01E01.mkv"),
            scanned("Star.Trek.Picard.S01E01.mkv"),
        ]
    )

    for plan in plans:
        assert plan.destination is not None
        assert "Star Trek/" in str(plan.destination), plan.destination


async def test_une_serie_isolee_n_est_pas_enfouie(tmp_path: Path) -> None:
    """Sans voisin, pas de dossier de franchise : le chemin reste court."""
    tmdb = FakeTMDB(series={"the witcher blood origin": [series("The Witcher: Blood Origin", "9")]})
    pipeline = make_pipeline(tmdb, tmp_path)

    plans = await pipeline.plan_all([scanned("The.Witcher.Blood.Origin.S01E01.mkv")])

    assert "The Witcher/The Witcher: Blood Origin" not in str(plans[0].destination)


async def test_la_bibliotheque_existante_compte_comme_voisine(tmp_path: Path) -> None:
    """Importer « Star Trek: Picard » seul doit le ranger avec les Star Trek
    deja presents — sinon la franchise se reconstitue au coup par coup."""
    tmdb = FakeTMDB(series={"star trek picard": [series("Star Trek: Picard", "2")]})
    pipeline = make_pipeline(tmdb, tmp_path, known={"Star Trek: Discovery"})

    plans = await pipeline.plan_all([scanned("Star.Trek.Picard.S01E01.mkv")])

    assert "Star Trek/" in str(plans[0].destination)


async def test_un_gabarit_sans_jeton_de_collection_est_inchange(tmp_path: Path) -> None:
    """La seconde passe ne doit rien casser chez qui n'a pas demande le
    regroupement."""
    tmdb = FakeTMDB(
        series={
            "star trek discovery": [series("Star Trek: Discovery", "1")],
            "star trek picard": [series("Star Trek: Picard", "2")],
        }
    )
    pipeline = Pipeline(
        tmdb=tmdb,
        anilist=None,
        library_root=tmp_path,
        templates={"episode": "{title}/S{season:02}E{episode:02}", "movie": "", "anime": ""},
        policy=Policy(auto_apply_threshold=0.5, reject_threshold=0.1),
    )

    plans = await pipeline.plan_all(
        [scanned("Star.Trek.Discovery.S01E01.mkv"), scanned("Star.Trek.Picard.S01E01.mkv")]
    )

    for plan in plans:
        assert "Star Trek/Star Trek:" not in str(plan.destination)
