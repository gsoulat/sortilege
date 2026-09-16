"""Ranger et Ma mediatheque : deux espaces, deux listes, deux jeux de chiffres.

Cas reel (v0.52.1). Des que l'index de bibliotheque existe, une oeuvre possedee
qui partage sa cle avec un fichier en attente devient UNE entree fusionnee. La
route rendait ces entrees melangees et l'ecran les triait lui-meme, sur une page
deja tronquee :

- l'onglet Ranger affichait « Only Murders in the Building — 28 fichiers ·
  49,8 Go · 2 manquants · 4 prets » : les chiffres de la mediatheque, pour
  quatre episodes a ranger ;
- ses filtres de type comptaient TOUTES les oeuvres chargees (« Films (68) ·
  Series (132) ») a cote d'un « Tout (11) » ;
- « Relire la bibliotheque » semblait donc verser la mediatheque dans Ranger.

Ces tests passent par les vraies routes : index construit par
``POST /api/collection/build``, rangement par ``POST /api/review/apply``,
lecture par ``GET /api/workspace``.
"""

from __future__ import annotations

import errno
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient
from helpers import BIG_ENOUGH, big_file

from sortilege.api import collection, library, review
from sortilege.api.deps import scan_rules
from sortilege.config import get_settings
from sortilege.core import journal as core_journal
from sortilege.core import vpn
from sortilege.core.journal import Journal, apply_plan
from sortilege.core.parser import parse
from sortilege.core.planner import Plan
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile, scan
from sortilege.core.scoring import Decision
from sortilege.main import app

PASSWORD = "mot-de-passe-de-test"
TAILLE = BIG_ENOUGH + 1
"""Taille apparente de chaque fichier cree par ``big_file``."""

SERIE = "Only Murders in the Building"


@dataclass
class Monde:
    client: TestClient
    media: Path
    dl: Path
    episodes_en_attente: list[Path]
    arrival: Path
    dune: Path


def _plan_episode(source: Path, media: Path, numero: int) -> Plan:
    return Plan(
        id=f"omitb-s04e{numero:02d}",
        source=source,
        destination=media
        / "Series"
        / f"{SERIE} (2021)"
        / "Season 04"
        / f"{SERIE} - S04E{numero:02d}.mkv",
        kind="episode",
        score=0.97,
        decision=Decision.AUTO,
        title=SERIE,
        year=2021,
    )


@pytest.fixture
def monde(tmp_path: Path, monkeypatch) -> Monde:
    conf = get_settings()
    media = tmp_path / "media"
    dl = tmp_path / "dl"
    monkeypatch.setattr(conf, "library_root", media)
    # Index purement local : sans cle, aucune sortie reseau a garder.
    monkeypatch.setattr(collection, "tmdb_key", lambda: "")

    # --- Ce qui est deja range : une serie de 28 episodes, un film ---------
    for saison, nombre in ((1, 10), (2, 10), (3, 8)):
        for episode in range(1, nombre + 1):
            big_file(
                media
                / "Series"
                / f"{SERIE} (2021)"
                / f"Season {saison:02d}"
                / f"{SERIE} - S{saison:02d}E{episode:02d}.mkv"
            )
    big_file(
        media
        / "Films"
        / "Independence Day Resurgence (2016)"
        / "Independence Day Resurgence (2016).mkv"
    )

    # --- Ce qui attend dans la source ---------------------------------------
    episodes = [
        big_file(dl / f"Only.Murders.in.the.Building.S04E{n:02d}.1080p.WEB.H264.mkv")
        for n in range(1, 5)
    ]
    arrival = big_file(dl / "Arrival.2016.1080p.BluRay.x264.mkv")
    dune = big_file(dl / "Dune.2021.1080p.BluRay.x264.mkv")

    avant = library._job.result
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": PASSWORD})
        library._job.result = scan([dl], deep=False, library_root=media, rules=scan_rules())
        review._plans.clear()
        review._job.done_paths.clear()
        for numero, source in enumerate(episodes, start=1):
            plan = _plan_episode(source, media, numero)
            review._plans[plan.id] = plan
        review._plans["arrival"] = Plan(
            id="arrival",
            source=arrival,
            destination=media / "Films" / "Arrival (2016)" / "Arrival (2016).mkv",
            kind="movie",
            score=0.97,
            decision=Decision.AUTO,
            title="Arrival",
            year=2016,
        )
        # Dune reste sans plan : un film seulement dans la source, pas encore
        # identifie.
        collection.forget_index()
        try:
            yield Monde(client, media, dl, episodes, arrival, dune)
        finally:
            review._plans.clear()
            review._job.done_paths.clear()
            collection.forget_index()
            library._job.result = avant


def _espace(client: TestClient, espace: str) -> dict:
    reponse = client.get(f"/api/workspace?espace={espace}")
    assert reponse.status_code == 200
    return reponse.json()


def _par_titre(corps: dict) -> dict[str, dict]:
    return {w["title"]: w for w in corps["works"]}


# --- 1. Reproduction ----------------------------------------------------------


def test_relire_la_bibliotheque_ne_verse_pas_la_mediatheque_dans_ranger(monde: Monde) -> None:
    assert monde.client.post("/api/collection/build").status_code == 200

    ranger = _espace(monde.client, "source")
    lignes = _par_titre(ranger)

    # Ce qui reste a ranger, et rien d'autre : le film deja possede n'a rien a
    # faire dans Ranger.
    assert set(lignes) == {SERIE, "Arrival", "Dune"}

    serie = lignes[SERIE]
    # Les chiffres de la SOURCE : quatre episodes a ranger, pas 28 fichiers.
    assert serie["source"]["file_count"] == 4
    assert serie["source"]["total_bytes"] == 4 * TAILLE
    assert serie["source"]["ready"] == 4
    assert serie["source"]["unplanned"] == 0
    # L'indication pour arbitrer : ce qui est deja la.
    assert serie["source"]["in_library"] == {"files": 28, "episodes": 28}
    assert lignes["Arrival"]["source"]["in_library"] is None


def test_les_compteurs_de_ranger_comptent_la_liste_affichee(monde: Monde) -> None:
    monde.client.post("/api/collection/build")

    ranger = _espace(monde.client, "source")
    works = ranger["works"]
    tab = ranger["tab"]

    assert tab["works"] == len(works) == ranger["total"] == 3
    assert tab["ready"] == sum(1 for w in works if w["pending"]["ready"]) == 2
    assert tab["unplanned"] == sum(1 for w in works if w["pending"]["unplanned_count"]) == 1
    assert tab["review"] == 0
    assert tab["kinds"] == {"episode": 1, "movie": 2}
    # Aucune entree de la liste ne compte dans un type absent des compteurs.
    assert sum(tab["kinds"].values()) == len(works)


def test_la_mediatheque_garde_ses_oeuvres_et_leurs_chiffres(monde: Monde) -> None:
    monde.client.post("/api/collection/build")

    mediatheque = _espace(monde.client, "library")
    lignes = _par_titre(mediatheque)

    assert set(lignes) == {SERIE, "Independence Day Resurgence"}
    assert lignes[SERIE]["owned"]["file_count"] == 28
    assert mediatheque["tab"]["works"] == len(mediatheque["works"]) == 2
    assert mediatheque["tab"]["kinds"] == {"episode": 1, "movie": 1}


def test_sans_espace_la_route_rend_toujours_tout(monde: Monde) -> None:
    """Le defaut ne change pas : un client qui ne precise rien voit tout."""
    monde.client.post("/api/collection/build")

    tout = monde.client.get("/api/workspace").json()

    assert {w["title"] for w in tout["works"]} == {
        SERIE,
        "Independence Day Resurgence",
        "Arrival",
        "Dune",
    }


# --- 3. Apres « Ranger » -------------------------------------------------------


def test_ranger_vide_ranger_sauf_ce_qui_echoue(monde: Monde, monkeypatch) -> None:
    monde.client.post("/api/collection/build")
    refuse = monde.episodes_en_attente[1]
    vrai_move = core_journal._move

    def move(source: Path, destination: Path) -> str:
        # Le reglage du NAS de l'utilisateur : dossier en 999:999, conteneur en
        # 1000:1000.
        if source == refuse:
            raise PermissionError(errno.EACCES, "Permission denied", str(source))
        return vrai_move(source, destination)

    monkeypatch.setattr(core_journal, "_move", move)

    corps = monde.client.post(
        "/api/review/apply",
        json={
            "plan_ids": [*(f"omitb-s04e{n:02d}" for n in range(1, 5)), "arrival"],
            "dry_run": False,
        },
    ).json()
    assert (corps["applied"], corps["failed"]) == (4, 1)

    ranger = _par_titre(_espace(monde.client, "source"))

    # Arrival est range : il quitte Ranger sans rescan.
    assert "Arrival" not in ranger
    # La serie ne garde que l'episode en echec, avec la raison.
    serie = ranger[SERIE]
    assert serie["source"]["file_count"] == 1
    assert serie["source"]["failed"] == 1
    (reste,) = serie["pending"]["ready"]
    assert reste["source"] == str(refuse)
    assert reste["failure"]["reason"] == "permission_denied"
    # Les fichiers deplaces ne reviennent pas en « non identifies ».
    assert serie["source"]["unplanned"] == 0
    assert "Dune" in ranger


def test_apres_ranger_la_mediatheque_dit_qu_il_faut_la_relire(monde: Monde) -> None:
    monde.client.post("/api/collection/build")
    monde.client.post(
        "/api/review/apply",
        json={"plan_ids": ["omitb-s04e01", "arrival"], "dry_run": False},
    )

    avant = _espace(monde.client, "library")
    # L'index ne s'est pas relu tout seul, et la reponse le DIT.
    assert avant["ranged_since_index"] == 2
    assert "Arrival" not in _par_titre(avant)

    monde.client.post("/api/collection/build")
    apres = _espace(monde.client, "library")

    assert apres["ranged_since_index"] == 0
    lignes = _par_titre(apres)
    assert "Arrival" in lignes
    assert lignes[SERIE]["owned"]["file_count"] == 29


# --- 4. Identification : une seule regle d'eligibilite -------------------------


class PipelineEspion:
    """Retient les fichiers recus et rend un plan par fichier, comme le vrai."""

    lots: ClassVar[list[list[Path]]] = []

    def __init__(self, **kwargs) -> None:
        pass

    async def plan_all(self, files, on_progress=None, on_plan=None):
        PipelineEspion.lots.append([f.path for f in files])
        plans = [
            Plan(
                id=f"espion-{f.path.name}",
                source=f.path,
                destination=None,
                kind="movie",
                score=0.5,
                decision=Decision.REVIEW,
                title=f.parsed.title,
            )
            for f in files
        ]
        for plan in plans:
            if on_plan is not None:
                on_plan(plan)
        return plans

    async def aclose(self) -> None:
        pass


async def _sortie_libre(*args, **kwargs) -> vpn.Decision:
    return vpn.Decision(allowed=True, warn=False, reason="")


def test_identifier_prend_exactement_ce_que_l_ecran_compte(monde: Monde, monkeypatch) -> None:
    """« Identification interrompue : 68 fichier(s) n'ont pas pu etre planifies ».

    L'ecran comptait « a identifier » tout fichier present sans plan vivant ; le
    calcul, lui, sautait tout chemin deja passe par un lot. Un fichier dont le
    plan avait disparu alors qu'il etait toujours la — un rangement annule
    depuis le journal, typiquement — restait compte sans jamais etre repris.
    """
    PipelineEspion.lots.clear()
    monkeypatch.setattr(review, "Pipeline", PipelineEspion)
    monkeypatch.setattr(review, "tmdb_key", lambda: "cle-de-test")
    monkeypatch.setattr(review, "egress_allowed", _sortie_libre)

    # Dune : jamais planifie. Arrival : planifie, range, puis ANNULE — revenu a
    # sa place d'origine, sans plan, mais marque comme deja traite.
    del review._plans["arrival"]
    review._job.done_paths.add(str(monde.arrival))
    # Un fichier du scan disparu depuis : ni compte, ni envoye au calcul.
    parti = monde.dl / "Parti.2020.1080p.mkv"
    library._job.result.files.append(
        ScannedFile(
            path=parti,
            size_bytes=TAILLE,
            parsed=parse(parti, []),
            probe=FileProbe(),
            relative_path=parti.name,
        )
    )

    compte = monde.client.get("/api/workspace?espace=source").json()["counts"]["unplanned"]
    assert compte == 2
    assert review._remaining() == compte

    assert monde.client.post("/api/review/plan").json()["started"] is True
    limite = time.monotonic() + 5
    while review._job.running and time.monotonic() < limite:
        time.sleep(0.02)
    assert not review._job.running

    assert sorted(PipelineEspion.lots[-1]) == sorted([monde.arrival, monde.dune])
    assert monde.client.get("/api/workspace").json()["counts"]["unplanned"] == 0


def test_un_lot_en_cours_ne_se_juge_pas_a_son_lancement(monde: Monde, monkeypatch) -> None:
    """La route rend la main des le lancement : le compte ne baisse qu'ensuite.

    L'ecran comparait le compte juste apres cet appel, concluait « rien n'a
    avance » et affichait « Identification interrompue » pendant que le lot
    tournait encore. C'est ce que ce test fige cote serveur : l'interface doit
    attendre ``jobs.plan.running == false`` avant de juger.
    """
    import asyncio

    feu = asyncio.Event()

    class PipelineLent(PipelineEspion):
        async def plan_all(self, files, on_progress=None, on_plan=None):
            await feu.wait()
            return await super().plan_all(files, on_progress, on_plan)

    monkeypatch.setattr(review, "Pipeline", PipelineLent)
    monkeypatch.setattr(review, "tmdb_key", lambda: "cle-de-test")
    monkeypatch.setattr(review, "egress_allowed", _sortie_libre)

    assert monde.client.post("/api/review/plan").json()["started"] is True
    juste_apres = monde.client.get("/api/workspace").json()
    assert juste_apres["jobs"]["plan"]["running"] is True
    assert juste_apres["counts"]["unplanned"] == 1

    review._plan_task.get_loop().call_soon_threadsafe(feu.set)
    limite = time.monotonic() + 5
    while review._job.running and time.monotonic() < limite:
        time.sleep(0.02)
    assert monde.client.get("/api/workspace").json()["counts"]["unplanned"] == 0


# --- Un deplacement refuse ne laisse pas de copie derriere lui ----------------


def test_un_deplacement_refuse_ne_laisse_pas_de_copie_a_destination(
    tmp_path: Path, monkeypatch
) -> None:
    """Deux montages distincts (telechargements et bibliotheque) : ``rename``
    echoue en EXDEV, ``shutil.move`` COPIE puis tente de supprimer la source. Si
    le dossier source est en lecture seule, la suppression est refusee — et la
    copie complete restait dans la bibliotheque. Le clic suivant disait alors
    « la destination existe deja », et « Relire la bibliotheque » montrait comme
    possede un fichier que le rangement declarait en echec.
    """
    source = tmp_path / "dl" / "Film.2020.mkv"
    source.parent.mkdir()
    source.write_bytes(b"contenu")
    destination = tmp_path / "media" / "Films" / "Film (2020).mkv"
    plan = Plan(
        id="film",
        source=source,
        destination=destination,
        kind="movie",
        score=0.97,
        decision=Decision.AUTO,
    )

    def rename(a, b, *args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    vrai_unlink = os.unlink

    def unlink(chemin, *args, **kwargs):
        if Path(chemin) == source:
            raise PermissionError(errno.EACCES, "Permission denied", str(chemin))
        return vrai_unlink(chemin, *args, **kwargs)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "unlink", unlink)

    resultat = apply_plan(plan, Journal(tmp_path / "journal.jsonl"), dry_run=False)

    assert not resultat.ok
    assert resultat.reason == "permission_denied"
    assert source.is_file(), "la source est intacte"
    assert not destination.exists(), "aucune copie orpheline a destination"


def test_la_repetition_d_un_echec_garde_sa_raison(monde: Monde, monkeypatch) -> None:
    """Le second clic sur « Ranger » rapporte la meme cause, pas « la
    destination existe deja » -- et la mediatheque relue ne montre pas le film
    comme possede."""
    refuse = monde.arrival

    def rename(a, b, *args, **kwargs):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    vrai_unlink = os.unlink

    def unlink(chemin, *args, **kwargs):
        if Path(chemin) == refuse:
            raise PermissionError(errno.EACCES, "Permission denied", str(chemin))
        return vrai_unlink(chemin, *args, **kwargs)

    # Un contexte a part : ``undo`` sur le monkeypatch du test defairait aussi
    # la racine de bibliotheque posee par la fixture.
    with monkeypatch.context() as montages:
        montages.setattr(os, "rename", rename)
        montages.setattr(os, "unlink", unlink)
        for _ in range(2):
            corps = monde.client.post(
                "/api/review/apply", json={"plan_ids": ["arrival"], "dry_run": False}
            ).json()
            assert corps["results"][0]["reason"] == "permission_denied"
    assert refuse.is_file()

    assert monde.client.post("/api/collection/build").json()["count"] == 2
    assert "Arrival" not in _par_titre(_espace(monde.client, "library"))
    assert "Arrival" in _par_titre(_espace(monde.client, "source"))


def test_l_echec_de_rangement_survit_a_la_relecture_de_la_file(monde: Monde, monkeypatch) -> None:
    """La raison est portee par le plan, donc enregistree avec lui : un
    redemarrage ne la fait pas retomber dans un « pret » muet."""
    from sortilege.core.snapshot import plans_in, plans_out

    def move(source: Path, destination: Path) -> str:
        raise PermissionError(errno.EACCES, "Permission denied", str(source))

    monkeypatch.setattr(core_journal, "_move", move)
    monde.client.post("/api/review/apply", json={"plan_ids": ["arrival"], "dry_run": False})

    (relu,) = [p for p in plans_in(plans_out(review.current_plans())) if p.id == "arrival"]
    assert relu.apply_failure is not None
    assert relu.apply_failure["reason"] == "permission_denied"


def test_une_simulation_n_inscrit_pas_d_echec(monde: Monde, monkeypatch) -> None:
    monkeypatch.setattr(core_journal, "_why_not_writable", lambda plan: "dossier en lecture seule")
    monde.client.post("/api/review/apply", json={"plan_ids": ["arrival"], "dry_run": True})
    assert review._plans["arrival"].apply_failure is None
