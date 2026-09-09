"""Ce qu'un nouveau scan doit remettre d'aplomb.

Un rescan ne recalait rien. Deux etats survivaient a leur objet :

- **les plans dont le fichier a disparu** — range, evacue, supprime a la main.
  Le plan restait affiche, proposait un deplacement impossible et echouait a
  l'application. La file ne se vidait donc jamais tout a fait ;
- **les chemins marques comme deja planifies**, qui empechaient de recalculer
  un fichier revenu sous le meme nom — un telechargement refait apres un echec,
  qu'on ne pouvait plus identifier.

Ce qui compte autant que le menage : ce qui est TOUJOURS LA ne doit pas bouger.
Un arbitrage en attente sur un fichier present est du travail humain, et le
perdre serait bien pire que de garder un plan perime.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.api import review
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision


def plan(pid: str, source: str, decision: Decision = Decision.AUTO) -> Plan:
    return Plan(
        id=pid,
        source=Path(source),
        destination=Path(f"/lib/{pid}.mkv"),
        kind="episode",
        score=0.9,
        decision=decision,
        title="Une Serie",
    )


@pytest.fixture
def file_de_revue():
    review._plans.clear()
    review._job.done_paths.clear()
    yield
    review._plans.clear()
    review._job.done_paths.clear()


# --- Le menage --------------------------------------------------------------


def test_un_plan_dont_le_fichier_a_disparu_est_retire(file_de_revue) -> None:
    review._plans["parti"] = plan("parti", "/dl/deja-range.mkv")
    review._plans["reste"] = plan("reste", "/dl/toujours-la.mkv")

    bilan = review.reconcile_with_scan({"/dl/toujours-la.mkv"})

    assert bilan["dropped_plans"] == 1
    assert "parti" not in review._plans
    assert "reste" in review._plans


def test_un_chemin_disparu_cesse_d_etre_marque_planifie(file_de_revue) -> None:
    """Sinon un telechargement refait sous le meme nom ne serait plus jamais
    identifie : l'outil le croirait deja traite."""
    review._job.done_paths = {"/dl/a.mkv", "/dl/b.mkv"}

    bilan = review.reconcile_with_scan({"/dl/a.mkv"})

    assert bilan["forgotten_paths"] == 1
    assert review._job.done_paths == {"/dl/a.mkv"}


def test_un_scan_vide_vide_la_file(file_de_revue) -> None:
    """Sources debranchees ou vidées : plus rien ne doit etre propose."""
    review._plans["x"] = plan("x", "/dl/x.mkv")
    review._job.done_paths = {"/dl/x.mkv"}

    review.reconcile_with_scan(set())

    assert not review._plans
    assert not review._job.done_paths


# --- Ce qui ne doit PAS bouger ----------------------------------------------


def test_un_arbitrage_en_attente_survit(file_de_revue) -> None:
    """LA propriete a preserver. C'est du travail humain : le perdre serait
    bien pire que de garder un plan perime."""
    review._plans["a_trancher"] = plan("a_trancher", "/dl/ambigu.mkv", Decision.REVIEW)

    review.reconcile_with_scan({"/dl/ambigu.mkv"})

    assert review._plans["a_trancher"].decision is Decision.REVIEW


def test_un_scan_identique_ne_change_rien(file_de_revue) -> None:
    review._plans["a"] = plan("a", "/dl/a.mkv")
    review._job.done_paths = {"/dl/a.mkv"}

    bilan = review.reconcile_with_scan({"/dl/a.mkv"})

    assert bilan == {"dropped_plans": 0, "forgotten_paths": 0}
    assert "a" in review._plans


# --- Cache des vignettes ----------------------------------------------------


def test_le_cache_des_vignettes_se_vide(tmp_path: Path, monkeypatch) -> None:
    """Une vignette perimee n'est jamais SERVIE — les cles incluent taille et
    date — mais elle n'est jamais liberee non plus. Le scan est le moment ou
    l'on sait que la bibliotheque a bouge."""
    from sortilege.api import media

    cache = tmp_path / "vignettes"
    cache.mkdir()
    for i in range(3):
        (cache / f"{i}.jpg").write_bytes(b"image")
    monkeypatch.setattr(media, "CACHE_DIR", cache)

    assert media.purge_thumbnails() == 3
    assert not list(cache.glob("*.jpg"))


def test_vider_un_cache_absent_ne_leve_pas(tmp_path: Path, monkeypatch) -> None:
    from sortilege.api import media

    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "jamais-cree")
    assert media.purge_thumbnails() == 0
