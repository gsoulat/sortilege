"""Les lots de la revue : sortie reseau, reprise par decalage, concurrence.

Trois defauts du meme genre, de ceux qui ne se voient pas a l'ecran :

- "exiger un VPN" laissait partir la recherche manuelle, le choix d'un
  candidat et le telechargement des affiches : le reglage protegeait
  l'identification, et rien d'autre ;
- "sous-titres de toute la bibliotheque" reprenait a chaque appel les memes
  premiers fichiers, et n'allait donc jamais plus loin ;
- deux clics lancaient deux lots sur les memes fichiers.

Aucun test ne touche le reseau : la sortie est une decision fabriquee, et les
fournisseurs qu'on ne doit pas construire levent s'ils le sont.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sortilege.api import review
from sortilege.api.deps import get_store
from sortilege.config import get_settings
from sortilege.core import nfo, opf, subtitles, vpn
from sortilege.core.companions import TRASH_DIRNAME
from sortilege.core.nfo import LocalMetadataSettings
from sortilege.core.parser import parse
from sortilege.core.planner import Plan
from sortilege.core.preferences import SubtitleSettings
from sortilege.core.probe import FileProbe
from sortilege.core.scanner import ScannedFile, ScanResult
from sortilege.core.scoring import Decision
from sortilege.main import app
from sortilege.providers import opensubtitles
from sortilege.providers.base import Candidate

MOTIF = "Émission refusée : aucun tunnel détecté."


@pytest.fixture(autouse=True)
def _etat_neuf():
    """Reglages, file et drapeaux remis a zero apres chaque test."""
    store = get_store()
    avant = copy.deepcopy(store.load())
    review._plans.clear()
    yield
    review._plans.clear()
    review._sous_titres_en_cours = False
    review._fiches_en_cours = False
    store._cache = None
    store.save(avant)
    vpn.reset_cache()


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    conf = get_settings()
    monkeypatch.setattr(conf, "library_root", tmp_path / "media")
    (tmp_path / "media").mkdir()
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


def _reglages(**blocs) -> None:
    store = get_store()
    prefs = store.load()
    for nom, valeur in blocs.items():
        setattr(prefs, nom, valeur)
    store.save(prefs)


def _sous_titres(**champs) -> SubtitleSettings:
    return SubtitleSettings(
        enabled=True, opensubtitles_api_key="cle-de-test", languages=["fr"], **champs
    )


def _decision(permise: bool) -> vpn.Decision:
    return vpn.Decision(
        allowed=permise, warn=False, reason="Tunnel confirmé." if permise else MOTIF
    )


def _fixer_la_sortie(monkeypatch, permise: bool) -> None:
    async def sortie(*args, **kwargs):
        return _decision(permise)

    monkeypatch.setattr(review, "egress_allowed", sortie)


def _interdire_les_fournisseurs(monkeypatch) -> None:
    class Interdit:
        def __init__(self, *args, **kwargs):
            raise AssertionError("fournisseur construit malgre une sortie refusee")

    for nom in ("TMDBProvider", "AniListProvider", "Pipeline"):
        monkeypatch.setattr(review, nom, Interdit)


def _scanne(chemin: Path) -> ScannedFile:
    return ScannedFile(
        path=chemin,
        size_bytes=1,
        parsed=parse(chemin, [chemin.parent.parent.name, chemin.parent.name]),
        probe=FileProbe(),
    )


def _films(racine: Path, nombre: int) -> list[ScannedFile]:
    return [
        _scanne(racine / "Films" / f"Film {i:02d} (2020)" / f"Film {i:02d} (2020).mkv")
        for i in range(nombre)
    ]


# --- "exiger" couvre toute sortie, pas seulement l'identification ----------


def test_la_recherche_est_refusee_avant_tout_fournisseur(client, monkeypatch) -> None:
    plan = Plan(
        id="cherche",
        source=Path("/dl/un.nom.illisible.S01E01.mkv"),
        destination=Path("/lib/x.mkv"),
        kind="episode",
        score=0.4,
        decision=Decision.REVIEW,
        title="Un Nom Illisible",
    )
    review._plans[plan.id] = plan
    monkeypatch.setattr(review, "tmdb_key", lambda: "cle-de-test")
    _fixer_la_sortie(monkeypatch, permise=False)
    _interdire_les_fournisseurs(monkeypatch)

    reponse = client.get("/api/review/cherche/search", params={"q": "Severance"})

    assert reponse.status_code == 409
    assert reponse.json()["detail"] == MOTIF
    assert review._plans["cherche"].alternatives == []


def test_le_choix_est_refuse_avant_tout_fournisseur(client, tmp_path, monkeypatch) -> None:
    source = tmp_path / "dl" / "Dark.Matter.S01E01.mkv"
    plan = Plan(
        id="choisit",
        source=source,
        destination=tmp_path / "media" / "x.mkv",
        kind="episode",
        score=0.5,
        decision=Decision.REVIEW,
        title="Dark Matter",
        alternatives=[Candidate(provider="tmdb", external_id="1", title="Dark Matter", year=2024)],
    )
    review._plans[plan.id] = plan
    monkeypatch.setattr(review, "last_scan", lambda: ScanResult(files=[_scanne(source)]))
    monkeypatch.setattr(review, "tmdb_key", lambda: "cle-de-test")
    _fixer_la_sortie(monkeypatch, permise=False)
    _interdire_les_fournisseurs(monkeypatch)

    reponse = client.post(
        "/api/review/choisit/choose", json={"provider": "tmdb", "external_id": "1"}
    )

    assert reponse.status_code == 409
    assert reponse.json()["detail"] == MOTIF
    assert review._plans["choisit"] is plan


@pytest.fixture
def affiches(monkeypatch) -> list[str]:
    """Fiches et affiches actives ; rend la liste des telechargements tentes."""
    tentes: list[str] = []

    def espion(url, destination, *, client=None):
        tentes.append(url)
        return False

    monkeypatch.setattr(nfo, "fetch_image", espion)
    _reglages(local_metadata=LocalMetadataSettings(nfo=True, artwork=True))
    return tentes


def _film_a_ranger(tmp_path: Path) -> Plan:
    source = tmp_path / "dl" / "Dune.2021.mkv"
    source.parent.mkdir(parents=True)
    source.write_text("contenu")
    plan = Plan(
        id="film",
        source=source,
        destination=tmp_path / "media" / "Films" / "Dune (2021)" / "Dune (2021).mkv",
        kind="movie",
        score=0.97,
        decision=Decision.AUTO,
        title="Dune",
        year=2021,
        poster_url="https://image.exemple/affiche.jpg",
        backdrop_url="https://image.exemple/fond.jpg",
    )
    review._plans[plan.id] = plan
    return plan


def test_une_sortie_refusee_retire_les_affiches_sans_bloquer_le_rangement(
    client, tmp_path, monkeypatch, affiches
) -> None:
    plan = _film_a_ranger(tmp_path)
    _fixer_la_sortie(monkeypatch, permise=False)

    reponse = client.post("/api/review/apply", json={"plan_ids": ["film"], "dry_run": False})

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["applied"] == 1
    assert plan.destination.is_file()
    assert affiches == []
    assert corps["artwork_refused"] == MOTIF
    # La fiche, elle, ne sort pas de la maison : elle reste ecrite.
    assert plan.destination.with_suffix(".nfo").is_file()


def test_une_sortie_permise_telecharge_les_affiches(
    client, tmp_path, monkeypatch, affiches
) -> None:
    """Le temoin du test precedent : sans lui, "jamais appele" ne prouvait rien."""
    _film_a_ranger(tmp_path)
    _fixer_la_sortie(monkeypatch, permise=True)

    corps = client.post("/api/review/apply", json={"plan_ids": ["film"], "dry_run": False}).json()

    assert corps["applied"] == 1
    assert affiches
    assert "artwork_refused" not in corps


# --- subtitles-library avance par decalage -----------------------------------


def test_le_decalage_fait_avancer_la_bibliotheque(client, tmp_path, monkeypatch) -> None:
    _reglages(subtitles=_sous_titres())
    films = _films(tmp_path / "media", 5)
    # Rendus dans le desordre : l'ordre stable est celui du serveur, pas du disque.
    monkeypatch.setattr(review, "scan", lambda *a, **k: ScanResult(files=list(reversed(films))))
    lots: list[list[Path]] = []

    async def faux(demandes):
        lots.append([d[0] for d in demandes])
        return {"examined": len(demandes), "written": 0}

    monkeypatch.setattr(review, "_recuperer_sous_titres", faux)

    premier = client.post("/api/review/subtitles-library", params={"limit": 3}).json()
    assert (premier["total"], premier["offset"], premier["remaining"]) == (5, 0, 2)
    assert premier["next_offset"] == 3

    second = client.post(
        "/api/review/subtitles-library", params={"offset": premier["next_offset"], "limit": 3}
    ).json()
    assert (second["offset"], second["remaining"], second["next_offset"]) == (3, 0, None)

    assert set(lots[0]).isdisjoint(lots[1])
    assert sorted(lots[0] + lots[1]) == sorted(f.path for f in films)


def test_un_lot_interrompu_reprend_la_ou_il_s_est_arrete(client, tmp_path, monkeypatch) -> None:
    """Reprendre apres la fin du lot sauterait en silence les fichiers jamais vus."""
    _reglages(subtitles=_sous_titres())
    monkeypatch.setattr(review, "scan", lambda *a, **k: ScanResult(files=_films(tmp_path, 5)))

    async def refuse_au_deuxieme(demandes):
        return {"refused": MOTIF, "examined": 1, "written": 0}

    monkeypatch.setattr(review, "_recuperer_sous_titres", refuse_au_deuxieme)

    corps = client.post("/api/review/subtitles-library", params={"limit": 3}).json()

    assert corps["refused"] == MOTIF
    assert (corps["next_offset"], corps["remaining"]) == (1, 4)


@pytest.mark.parametrize("parametres", [{"limit": 501}, {"limit": 0}, {"offset": -1}])
def test_un_lot_hors_bornes_est_refuse(client, parametres) -> None:
    _reglages(subtitles=_sous_titres())
    reponse = client.post("/api/review/subtitles-library", params=parametres)
    assert reponse.status_code == 422


# --- Un lot a la fois ----------------------------------------------------------


def _pendant_un_lot(monkeypatch, appel) -> tuple[HTTPException, dict]:
    """Lance ``appel`` deux fois, la seconde pendant que la premiere scanne."""
    entre, libere = threading.Event(), threading.Event()

    def scan_lent(*args, **kwargs):
        entre.set()
        libere.wait(5)
        return ScanResult(files=[])

    monkeypatch.setattr(review, "scan", scan_lent)

    async def scenario():
        premier = asyncio.create_task(appel())
        assert await asyncio.to_thread(entre.wait, 5)
        try:
            with pytest.raises(HTTPException) as refus:
                await appel()
        finally:
            libere.set()
        return refus.value, await premier

    return asyncio.run(scenario())


def test_un_second_lot_de_sous_titres_est_refuse_pendant_le_premier(tmp_path, monkeypatch) -> None:
    _reglages(subtitles=_sous_titres())
    monkeypatch.setattr(get_settings(), "library_root", tmp_path)

    refus, corps = _pendant_un_lot(
        monkeypatch, lambda: review.subtitles_library(offset=0, limit=10)
    )

    assert refus.status_code == 409
    assert refus.detail == "Un lot de sous-titres est déjà en cours."
    assert corps["total"] == 0
    assert review._sous_titres_en_cours is False


def test_une_seconde_ecriture_des_fiches_est_refusee_pendant_la_premiere(
    tmp_path, monkeypatch
) -> None:
    _reglages(local_metadata=LocalMetadataSettings(nfo=True))
    monkeypatch.setattr(get_settings(), "library_root", tmp_path)

    refus, corps = _pendant_un_lot(monkeypatch, review.nfo_library)

    assert refus.status_code == 409
    assert refus.detail == "Une écriture des métadonnées locales est déjà en cours."
    assert corps["examined"] == 0
    assert review._fiches_en_cours is False


def test_un_lot_qui_echoue_libere_la_place(tmp_path, monkeypatch) -> None:
    _reglages(subtitles=_sous_titres())
    monkeypatch.setattr(get_settings(), "library_root", tmp_path)

    def scan_en_panne(*args, **kwargs):
        raise OSError("disque absent")

    monkeypatch.setattr(review, "scan", scan_en_panne)

    with pytest.raises(OSError):
        asyncio.run(review.subtitles_library(offset=0, limit=10))
    assert review._sous_titres_en_cours is False


# --- Les sous-titres revoient la sortie a chaque fichier --------------------


def test_les_sous_titres_s_arretent_au_premier_refus(tmp_path, monkeypatch) -> None:
    _reglages(subtitles=_sous_titres(audio_is_enough=True))
    mesures = 0

    async def sortie(*args, **kwargs):
        nonlocal mesures
        mesures += 1
        return _decision(permise=mesures == 1)

    appels: list[tuple[Path, dict]] = []

    async def faux_fetch(video, wanted, source, **kwargs):
        appels.append((video, kwargs))
        return []

    class FauxFournisseur:
        available = True
        unavailable_reason = ""

        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(review, "egress_allowed", sortie)
    monkeypatch.setattr(subtitles, "fetch_missing", faux_fetch)
    monkeypatch.setattr(opensubtitles, "OpenSubtitlesProvider", FauxFournisseur)
    monkeypatch.setattr(review, "probe_media", lambda chemin: FileProbe())
    demandes = [(tmp_path / f"e{i}.mkv", "Titre", 2020, 1, i) for i in (1, 2, 3)]

    rapport = asyncio.run(review._recuperer_sous_titres(demandes))

    assert rapport == {"refused": MOTIF, "examined": 1, "written": 0}
    assert mesures == 2
    assert [video for video, _ in appels] == [demandes[0][0]]
    options = appels[0][1]
    assert options["audio_is_enough"] is True
    assert isinstance(options["probe"], FileProbe)


# --- nfo-library : corbeille et episodes sans fiche -------------------------


def test_la_regeneration_passe_la_corbeille_et_compte_les_episodes_sans_fiche(
    tmp_path, monkeypatch
) -> None:
    racine = tmp_path / "media"
    monkeypatch.setattr(get_settings(), "library_root", racine)
    _reglages(local_metadata=LocalMetadataSettings(nfo=True, opf=True))
    episode = racine / "Series" / "Severance" / "Severance - episode.mkv"
    livre = racine / "Livres" / "Auteur - Titre" / "Auteur - Titre.epub"
    for chemin in (episode, livre):
        chemin.parent.mkdir(parents=True)
        chemin.write_bytes(b"x")
    # Un livre se lit par parse_book, comme le fait le scanner : parse seul le
    # prendrait pour une video et le test ne verrait jamais le depot du livre.
    from sortilege.core.parser import parse_book

    fichiers = [
        _scanne(episode),
        ScannedFile(path=livre, size_bytes=1, parsed=parse_book(livre), probe=FileProbe()),
    ]
    monkeypatch.setattr(review, "scan", lambda *a, **k: ScanResult(files=fichiers))
    corbeilles: list[Path | None] = []

    def faux_depot(media, kind, info, reglages, **kwargs):
        corbeilles.append(kwargs.get("trash_root"))
        return SimpleNamespace(written=[], skipped=[], errors=[], unnumbered=[media])

    def faux_depot_livre(book, meta, **kwargs):
        corbeilles.append(kwargs.get("trash_root"))
        return SimpleNamespace(manifest=None, cover=None)

    monkeypatch.setattr(review, "deposit", faux_depot)
    monkeypatch.setattr(opf, "deposit", faux_depot_livre)

    corps = asyncio.run(review.nfo_library())

    assert corps["failed"] == 0, corps["errors"]
    assert corps["without_sheet"] == 1
    assert corbeilles == [racine / TRASH_DIRNAME] * 2


def _faux_fournisseur():
    class FauxFournisseur:
        available = True
        unavailable_reason = ""

        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            pass

    return FauxFournisseur


def test_une_panne_du_fournisseur_arrete_le_lot_sans_sauter_de_video(tmp_path, monkeypatch) -> None:
    """Cle refusee ou quota epuise en cours de lot.

    La boucle continuait : chaque video restante etait comptee comme examinee
    sans rien recevoir, et la pagination sautait par-dessus. Le lot s'arrete et
    laisse la video fautive au lot suivant.
    """
    _reglages(subtitles=_sous_titres())
    demandes = [(tmp_path / f"e{i}.mkv", "Titre", 2020, 1, i) for i in (1, 2, 3)]
    appels: list[Path] = []

    async def sortie(*args, **kwargs):
        return _decision(permise=True)

    async def faux_fetch(video, wanted, source, **kwargs):
        appels.append(video)
        source.unavailable_reason = "Quota épuisé."
        return []

    monkeypatch.setattr(review, "egress_allowed", sortie)
    monkeypatch.setattr(subtitles, "fetch_missing", faux_fetch)
    monkeypatch.setattr(opensubtitles, "OpenSubtitlesProvider", _faux_fournisseur())
    monkeypatch.setattr(review, "probe_media", lambda chemin: FileProbe())

    rapport = asyncio.run(review._recuperer_sous_titres(demandes))

    assert rapport == {"unavailable": "Quota épuisé.", "examined": 0, "written": 0}
    assert appels == [demandes[0][0]], "aucune video suivante ne doit etre sollicitee"


def test_une_panne_sur_le_dernier_fichier_le_laisse_au_lot_suivant(tmp_path, monkeypatch) -> None:
    _reglages(subtitles=_sous_titres())
    demandes = [(tmp_path / f"e{i}.mkv", "Titre", 2020, 1, i) for i in (1, 2)]
    appels: list[Path] = []

    async def sortie(*args, **kwargs):
        return _decision(permise=True)

    async def faux_fetch(video, wanted, source, **kwargs):
        appels.append(video)
        if video == demandes[-1][0]:
            source.unavailable_reason = "Clé refusée."
        return []

    monkeypatch.setattr(review, "egress_allowed", sortie)
    monkeypatch.setattr(subtitles, "fetch_missing", faux_fetch)
    monkeypatch.setattr(opensubtitles, "OpenSubtitlesProvider", _faux_fournisseur())
    monkeypatch.setattr(review, "probe_media", lambda chemin: FileProbe())

    rapport = asyncio.run(review._recuperer_sous_titres(demandes))

    assert rapport == {"unavailable": "Clé refusée.", "examined": 1, "written": 0}
    assert appels == [demandes[0][0], demandes[1][0]]


def test_un_refus_d_un_lot_precedent_n_empeche_pas_de_reessayer(tmp_path, monkeypatch) -> None:
    """Le registre des refus est global : un 401 d'hier y restait.

    Le test d'indisponibilite en tete de boucle arretait alors chaque nouveau lot
    avant toute requete, meme la cle corrigee.
    """
    from sortilege.providers import base

    _reglages(subtitles=_sous_titres())
    demandes = [(tmp_path / f"e{i}.mkv", "Titre", 2020, 1, i) for i in (1, 2)]
    appels: list[Path] = []

    class FournisseurDuRegistre:
        available = True

        def __init__(self, *args, **kwargs):
            pass

        @property
        def unavailable_reason(self):
            return base.last_auth_error("opensubtitles")

        async def aclose(self):
            pass

    async def sortie(*args, **kwargs):
        return _decision(permise=True)

    async def faux_fetch(video, wanted, source, **kwargs):
        appels.append(video)
        return []

    base._AUTH_ERRORS["opensubtitles"] = "Clé OpenSubtitles refusée."
    try:
        monkeypatch.setattr(review, "egress_allowed", sortie)
        monkeypatch.setattr(subtitles, "fetch_missing", faux_fetch)
        monkeypatch.setattr(opensubtitles, "OpenSubtitlesProvider", FournisseurDuRegistre)
        monkeypatch.setattr(review, "probe_media", lambda chemin: FileProbe())
        rapport = asyncio.run(review._recuperer_sous_titres(demandes))
    finally:
        base.forget_auth_errors("opensubtitles")

    assert "unavailable" not in rapport
    assert appels == [demandes[0][0], demandes[1][0]]
