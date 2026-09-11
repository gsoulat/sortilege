"""Fiches .nfo et affiches deposees a cote des medias.

Ce module ecrit dans la bibliotheque de l'utilisateur, et ce qu'il ecrit FAIT
AUTORITE chez Jellyfin comme chez Plex. Les tests portent donc, dans l'ordre,
sur ce qu'il refuse de faire :

1. il n'ecrit jamais un element vide, qui effacerait ce que le serveur aurait
   su trouver ;
2. il n'ecrit jamais a moitie — un .nfo tronque est pire qu'absent ;
3. il n'ecrase jamais un fichier qu'il n'a pas ecrit sans qu'on l'ait demande ;
4. il ne fait jamais echouer un rangement, quoi qu'il lui arrive.
"""

from __future__ import annotations

import os
from pathlib import Path
from xml.etree import ElementTree

import httpx
import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps
from sortilege.core import nfo
from sortilege.core.journal import Journal, apply_plan
from sortilege.core.nfo import (
    LocalMetadataSettings,
    MediaInfo,
    OnExisting,
    Outcome,
    deposit,
    display_size,
    episode_xml,
    fetch_image,
    info_from_plan,
    movie_xml,
    tvshow_xml,
    work_folder,
    write_nfo,
)
from sortilege.core.planner import Plan
from sortilege.core.preferences import PreferenceError, Preferences, PreferenceStore
from sortilege.core.probe import read_nfo
from sortilege.core.scoring import Decision
from sortilege.main import app

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64
"""En-tete JPEG suivi de remplissage : assez pour que ce soit une image."""


@pytest.fixture
def client() -> TestClient:
    """Client deja authentifie : toute l'API est fermee par defaut."""
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


# --- Le XML produit ---------------------------------------------------------


def test_fiche_de_film_conforme() -> None:
    xml = movie_xml(
        MediaInfo(
            title="Dune",
            original_title="Dune",
            year=2021,
            plot="Paul Atreides part pour Arrakis.",
            genres=["Science-Fiction", "Aventure"],
            studios=["Legendary Pictures"],
            tmdb_id="438631",
            imdb_id="tt1160419",
        )
    )

    assert xml.startswith('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    root = ElementTree.fromstring(xml)  # noqa: S314 - chaine produite ici meme

    assert root.tag == "movie"
    assert root.findtext("title") == "Dune"
    assert root.findtext("originaltitle") == "Dune"
    assert root.findtext("year") == "2021"
    assert root.findtext("plot") == "Paul Atreides part pour Arrakis."
    assert [e.text for e in root.findall("genre")] == ["Science-Fiction", "Aventure"]
    assert [e.text for e in root.findall("studio")] == ["Legendary Pictures"]

    ids = {e.get("type"): (e.text, e.get("default")) for e in root.findall("uniqueid")}
    assert ids["tmdb"] == ("438631", "true")
    # Un seul identifiant porte « default » : deux en feraient un tirage au sort.
    assert ids["imdb"] == ("tt1160419", None)


def test_aucun_element_vide() -> None:
    """La regle qui justifie tout le reste.

    Un element ABSENT laisse le fournisseur distant remplir le champ ; un
    element VIDE ecrase ce qu'il aurait trouve, et par du vide. La difference
    ne se voit pas dans le fichier, elle se voit a l'ecran.
    """
    root = ElementTree.fromstring(movie_xml(MediaInfo(title="Dune")))  # noqa: S314

    assert [e.tag for e in root] == ["title"]


def test_fiche_d_episode_sans_identifiant() -> None:
    """L'identifiant qu'on possede est celui de la SERIE, pas de l'episode.

    L'inscrire ferait chercher au lecteur un episode sous un numero de serie —
    une identification fausse, presentee comme certaine.
    """
    xml = episode_xml(
        MediaInfo(title="Severance", season=1, episode=2, episode_title="Moitie et moitie")
    )
    root = ElementTree.fromstring(xml)  # noqa: S314

    assert root.tag == "episodedetails"
    assert root.findtext("title") == "Moitie et moitie"
    assert root.findtext("showtitle") == "Severance"
    assert root.findtext("season") == "1"
    assert root.findtext("episode") == "2"
    assert root.findall("uniqueid") == []


def test_fiche_de_serie() -> None:
    root = ElementTree.fromstring(  # noqa: S314
        tvshow_xml(MediaInfo(title="Severance", year=2022, tmdb_id="95396"))
    )

    assert root.tag == "tvshow"
    assert root.findtext("year") == "2022"
    assert root.findtext("uniqueid") == "95396"


def test_sortilege_relit_ses_propres_fiches(tmp_path: Path) -> None:
    """Aller-retour avec core/probe : ce qu'on ecrit doit se relire.

    Sans cela, un fichier range puis rescanne perdrait l'identite qu'on venait
    d'y inscrire — et repartirait en revue avec la meme question.
    """
    video = tmp_path / "Dune (2021).mkv"
    video.touch()
    write_nfo(video.with_suffix(".nfo"), movie_xml(MediaInfo(title="Dune", year=2021, tmdb_id="1")))

    relu = read_nfo(video)

    assert relu.tmdb_id == "1"
    assert relu.nfo_title == "Dune"
    assert relu.nfo_year == 2021


def test_les_caracteres_speciaux_ne_cassent_pas_le_xml() -> None:
    xml = movie_xml(MediaInfo(title="Astérix & Obélix <Mission Cléopâtre>"))
    root = ElementTree.fromstring(xml)  # noqa: S314

    assert root.findtext("title") == "Astérix & Obélix <Mission Cléopâtre>"


# --- L'ecriture -------------------------------------------------------------


def test_ecriture_atomique_ne_laisse_pas_de_moitie(tmp_path: Path, monkeypatch) -> None:
    """Une coupure pendant l'ecriture ne doit rien publier du tout.

    Le renommage final est le seul instant ou le fichier apparait. Tout ce qui
    echoue avant doit s'effacer : un .nfo de taille nulle serait relu comme
    faisant autorite.
    """
    cible = tmp_path / "Dune.nfo"

    def replace_qui_echoue(*args, **kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(nfo.os, "replace", replace_qui_echoue)

    with pytest.raises(OSError, match="disque plein"):
        write_nfo(cible, movie_xml(MediaInfo(title="Dune")))

    assert not cible.exists()
    # Et surtout : aucun temporaire abandonne a cote du media.
    assert list(tmp_path.iterdir()) == []


def test_un_nfo_existant_est_laisse_en_place(tmp_path: Path) -> None:
    """Le defaut. Le fichier present peut valoir bien mieux que le notre."""
    cible = tmp_path / "Dune.nfo"
    cible.write_text("<movie><title>Écrit à la main</title></movie>", encoding="utf-8")

    resultat = write_nfo(cible, movie_xml(MediaInfo(title="Dune")))

    assert resultat is Outcome.SKIPPED
    assert "à la main" in cible.read_text(encoding="utf-8")


def test_ecrasement_demande_explicitement(tmp_path: Path) -> None:
    cible = tmp_path / "Dune.nfo"
    cible.write_text("ancien", encoding="utf-8")

    resultat = write_nfo(
        cible, movie_xml(MediaInfo(title="Dune")), on_existing=OnExisting.OVERWRITE
    )

    assert resultat is Outcome.REPLACED
    assert "<title>Dune</title>" in cible.read_text(encoding="utf-8")
    assert not (tmp_path / "Dune.nfo.bak").exists()


def test_sauvegarde_de_l_ancien(tmp_path: Path) -> None:
    cible = tmp_path / "Dune.nfo"
    cible.write_text("ancien", encoding="utf-8")

    resultat = write_nfo(cible, movie_xml(MediaInfo(title="Dune")), on_existing=OnExisting.BACKUP)

    assert resultat is Outcome.BACKED_UP
    assert (tmp_path / "Dune.nfo.bak").read_text(encoding="utf-8") == "ancien"
    assert "<title>Dune</title>" in cible.read_text(encoding="utf-8")


# --- Ou les fichiers atterrissent -------------------------------------------


def test_le_dossier_de_l_oeuvre_est_reconnu(tmp_path: Path) -> None:
    film = tmp_path / "Amelie (2001)" / "Amelie (2001).mkv"
    assert work_folder(film, "Amélie", series=False) == film.parent

    episode = tmp_path / "Severance (2022)" / "Season 01" / "Severance - S01E01.mkv"
    assert work_folder(episode, "Severance", series=True) == episode.parent.parent


def test_un_dossier_commun_n_est_jamais_etiquete(tmp_path: Path) -> None:
    """« poster.jpg » decrit son dossier ENTIER.

    Depose dans un dossier qui contient trois cents films, il ferait de
    l'affiche de Dune la vignette de toute la bibliotheque.
    """
    film = tmp_path / "Films" / "Dune (2021).mkv"

    assert work_folder(film, "Dune", series=False) is None


def test_un_rangement_alphabetique_n_est_pas_pris_pour_un_dossier_d_oeuvre(
    tmp_path: Path,
) -> None:
    """« Films/D/Dune.mkv » : le dossier « D » n'appartient pas a Dune.

    C'est le piege de la comparaison faite dans les deux sens — le titre
    commence bien par « D », et une bibliotheque entiere se serait retrouvee
    etiquetee sous l'affiche du premier film range.
    """
    film = tmp_path / "Films" / "D" / "Dune (2021).mkv"

    assert work_folder(film, "Dune", series=False) is None


def test_depot_d_un_film(tmp_path: Path) -> None:
    film = tmp_path / "Dune (2021)" / "Dune (2021).mkv"
    film.parent.mkdir(parents=True)
    film.touch()

    depot = deposit(
        film, "movie", MediaInfo(title="Dune", year=2021), LocalMetadataSettings(nfo=True)
    )

    assert depot.written == [film.with_suffix(".nfo")]
    assert film.with_suffix(".nfo").is_file()


def test_depot_d_un_episode(tmp_path: Path) -> None:
    """Deux fiches : l'identite de la serie a sa racine, le numero a cote."""
    episode = tmp_path / "Severance (2022)" / "Season 01" / "Severance - S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.touch()

    depot = deposit(
        episode,
        "episode",
        MediaInfo(title="Severance", year=2022, tmdb_id="95396", season=1, episode=1),
        LocalMetadataSettings(nfo=True),
    )

    racine = episode.parent.parent
    assert set(depot.written) == {racine / "tvshow.nfo", episode.with_suffix(".nfo")}


def test_pas_de_fiche_d_episode_sans_numerotation(tmp_path: Path) -> None:
    """Un anime numerote en absolu n'a pas encore de saison resolue.

    Ecrire quand meme ne dirait que ce que le serveur lit deja dans le nom, et
    prendrait autorite pour le repeter.
    """
    episode = tmp_path / "One Piece" / "One Piece - 1088.mkv"
    episode.parent.mkdir(parents=True)
    episode.touch()

    depot = deposit(episode, "anime", MediaInfo(title="One Piece"), LocalMetadataSettings(nfo=True))

    assert depot.written == [episode.parent / "tvshow.nfo"]
    assert not episode.with_suffix(".nfo").exists()


def test_un_livre_ne_recoit_pas_de_fiche(tmp_path: Path) -> None:
    """Jellyfin attend un metadata.opf pour les livres, pas du XML Kodi."""
    livre = tmp_path / "Rothfuss" / "Le Nom du vent.epub"
    livre.parent.mkdir(parents=True)
    livre.touch()

    depot = deposit(
        livre, "book", MediaInfo(title="Le Nom du vent"), LocalMetadataSettings(nfo=True)
    )

    assert depot.written == []


def test_rien_n_est_ecrit_quand_le_reglage_est_au_repos(tmp_path: Path) -> None:
    """Le defaut : ecrire dans la bibliotheque de quelqu'un se demande."""
    film = tmp_path / "Dune (2021)" / "Dune (2021).mkv"
    film.parent.mkdir(parents=True)
    film.touch()

    depot = deposit(film, "movie", MediaInfo(title="Dune"), LocalMetadataSettings())

    assert depot.written == []
    assert list(film.parent.iterdir()) == [film]


# --- Les affiches -----------------------------------------------------------


def _client(reponses: dict[str, httpx.Response], vues: list[str]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        vues.append(str(request.url))
        return reponses.get(str(request.url), httpx.Response(404))

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_l_affiche_est_demandee_en_taille_d_affichage() -> None:
    """Cent quatre-vingt-cinq pixels suffisent a une grille, pas a un televiseur."""
    assert display_size("https://image.tmdb.org/t/p/w185/abc.jpg").endswith("/w780/abc.jpg")
    # Une URL etrangere a TMDB est laissee telle quelle : on ne devine pas la
    # convention de tailles d'un service qu'on ne connait pas.
    assert display_size("https://ailleurs.example/a.jpg") == "https://ailleurs.example/a.jpg"


def test_depot_des_affiches(tmp_path: Path) -> None:
    film = tmp_path / "Dune (2021)" / "Dune (2021).mkv"
    film.parent.mkdir(parents=True)
    film.touch()
    vues: list[str] = []
    client = _client(
        {
            "https://image.tmdb.org/t/p/w780/p.jpg": httpx.Response(
                200, content=JPEG, headers={"content-type": "image/jpeg"}
            ),
            "https://image.tmdb.org/t/p/w1280/f.jpg": httpx.Response(
                200, content=JPEG, headers={"content-type": "image/jpeg"}
            ),
        },
        vues,
    )

    with client:
        depot = deposit(
            film,
            "movie",
            MediaInfo(
                title="Dune",
                poster_url="https://image.tmdb.org/t/p/w185/p.jpg",
                backdrop_url="https://image.tmdb.org/t/p/w1280/f.jpg",
            ),
            LocalMetadataSettings(artwork=True),
            client=client,
        )

    assert (film.parent / "poster.jpg").read_bytes() == JPEG
    assert (film.parent / "fanart.jpg").read_bytes() == JPEG
    assert len(depot.images) == 2
    # L'image de fond n'a jamais servi de vignette : elle n'est pas redimensionnee.
    assert "https://image.tmdb.org/t/p/w1280/f.jpg" in vues


def test_une_affiche_absente_n_est_pas_un_echec(tmp_path: Path) -> None:
    """Le film est a sa place : c'est ce qui compte. On ne retente pas."""
    film = tmp_path / "Dune (2021)" / "Dune (2021).mkv"
    film.parent.mkdir(parents=True)
    film.touch()
    vues: list[str] = []

    with _client({}, vues) as client:
        depot = deposit(
            film,
            "movie",
            MediaInfo(title="Dune", poster_url="https://image.tmdb.org/t/p/w185/p.jpg"),
            LocalMetadataSettings(nfo=True, artwork=True),
            client=client,
        )

    assert depot.images == []
    assert depot.written == [film.with_suffix(".nfo")]
    assert len(vues) == 1, "un echec de telechargement ne doit pas etre retente"


def test_une_reponse_qui_n_est_pas_une_image_est_refusee(tmp_path: Path) -> None:
    cible = tmp_path / "poster.jpg"
    reponse = httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})

    with _client({"https://x.example/p.jpg": reponse}, []) as client:
        assert fetch_image("https://x.example/p.jpg", cible, client=client) is False

    assert not cible.exists()


def test_une_affiche_deja_posee_n_est_pas_remplacee(tmp_path: Path) -> None:
    """Meme regle que pour les .nfo : on ne remplace pas ce qu'on n'a pas ecrit."""
    cible = tmp_path / "poster.jpg"
    cible.write_bytes(b"deja la")
    vues: list[str] = []

    with _client({}, vues) as client:
        assert fetch_image("https://x.example/p.jpg", cible, client=client) is False

    assert cible.read_bytes() == b"deja la"
    assert vues == [], "rien ne doit meme etre demande"


# --- Le rangement ne depend jamais de la fiche ------------------------------


def _plan(source: Path, destination: Path) -> Plan:
    return Plan(
        id="p1",
        source=source,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        title="Dune",
        year=2021,
        provider="tmdb",
        external_id="438631",
    )


def test_un_echec_d_ecriture_ne_casse_pas_le_rangement(tmp_path: Path, monkeypatch) -> None:
    """La regle absolue : le fichier est deja a sa place, c'est l'essentiel."""
    source = tmp_path / "dl" / "dune.mkv"
    source.parent.mkdir()
    source.write_text("contenu")
    destination = tmp_path / "media" / "Dune (2021)" / "Dune (2021).mkv"

    def ecriture_impossible(*args, **kwargs):
        raise OSError("dossier en lecture seule")

    monkeypatch.setattr(nfo, "_write_nfo", ecriture_impossible)

    resultat = apply_plan(
        _plan(source, destination),
        Journal(tmp_path / "journal.jsonl"),
        dry_run=False,
        local_metadata=LocalMetadataSettings(nfo=True),
    )

    assert resultat.ok is True
    assert destination.read_text() == "contenu"
    assert not destination.with_suffix(".nfo").exists()


def test_une_panne_du_depot_ne_casse_pas_le_rangement(tmp_path: Path, monkeypatch) -> None:
    """Meme promesse pour une erreur qu'on n'avait pas prevue."""
    source = tmp_path / "dl" / "dune.mkv"
    source.parent.mkdir()
    source.write_text("contenu")
    destination = tmp_path / "media" / "Dune (2021)" / "Dune (2021).mkv"

    def panne(*args, **kwargs):
        raise RuntimeError("quelque chose d'inattendu")

    monkeypatch.setattr("sortilege.core.journal.deposit", panne)

    resultat = apply_plan(
        _plan(source, destination),
        Journal(tmp_path / "journal.jsonl"),
        dry_run=False,
        local_metadata=LocalMetadataSettings(nfo=True),
    )

    assert resultat.ok is True
    assert destination.is_file()


def test_la_fiche_suit_un_rangement_reussi(tmp_path: Path) -> None:
    source = tmp_path / "dl" / "dune.mkv"
    source.parent.mkdir()
    source.write_text("contenu")
    destination = tmp_path / "media" / "Dune (2021)" / "Dune (2021).mkv"

    resultat = apply_plan(
        _plan(source, destination),
        Journal(tmp_path / "journal.jsonl"),
        dry_run=False,
        local_metadata=LocalMetadataSettings(nfo=True),
    )

    assert resultat.ok is True
    fiche = ElementTree.fromstring(  # noqa: S314
        destination.with_suffix(".nfo").read_text(encoding="utf-8")
    )
    assert fiche.findtext("title") == "Dune"
    assert fiche.findtext("uniqueid") == "438631"


def test_la_simulation_n_ecrit_aucune_fiche(tmp_path: Path) -> None:
    source = tmp_path / "dl" / "dune.mkv"
    source.parent.mkdir()
    source.write_text("contenu")
    destination = tmp_path / "media" / "Dune (2021)" / "Dune (2021).mkv"

    apply_plan(
        _plan(source, destination),
        Journal(tmp_path / "journal.jsonl"),
        dry_run=True,
        local_metadata=LocalMetadataSettings(nfo=True),
    )

    assert not destination.parent.exists()


def test_la_numerotation_resolue_arrive_jusqu_a_la_fiche() -> None:
    """Ce que le plan transporte est ce qu'une fiche ne peut pas redeviner.

    Un anime converti depuis une numerotation absolue porte une saison que
    personne ne retrouverait dans le nom du fichier.
    """
    plan = Plan(
        id="p1",
        source=Path("/dl/onepiece-1088.mkv"),
        destination=Path("/media/One Piece/One Piece - 1088.mkv"),
        kind="anime",
        score=0.9,
        decision=Decision.AUTO,
        title="One Piece",
        provider="tmdb",
        external_id="37854",
        values={"season": 21, "episode": 13, "episode_title": "Un nouveau depart"},
    )

    info = info_from_plan(plan)

    assert (info.season, info.episode) == (21, 13)
    assert info.episode_title == "Un nouveau depart"
    assert info.tmdb_id == "37854"


# --- Le reglage -------------------------------------------------------------


def _store(tmp_path: Path) -> PreferenceStore:
    return PreferenceStore(tmp_path / "prefs.json", tmp_path / "media", [tmp_path / "dl"])


def test_desactive_par_defaut() -> None:
    prefs = Preferences()

    assert prefs.local_metadata.nfo is False
    assert prefs.local_metadata.artwork is False
    assert prefs.local_metadata.on_existing == "skip"


def test_une_conduite_inconnue_est_refusee(tmp_path: Path) -> None:
    """Refuse plutot que corrige : cette valeur decide du sort de fichiers que
    l'utilisateur n'a pas ecrits."""
    prefs = Preferences()
    prefs.local_metadata.on_existing = "efface tout"

    with pytest.raises(PreferenceError, match="conduite"):
        _store(tmp_path).validate(prefs)


def test_le_reglage_survit_a_un_redemarrage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    prefs = Preferences()
    prefs.local_metadata = LocalMetadataSettings(nfo=True, artwork=True, on_existing="backup")
    store.save(prefs)

    relu = _store(tmp_path).load()

    assert relu.local_metadata.nfo is True
    assert relu.local_metadata.on_existing == "backup"


# --- La route de regeneration -----------------------------------------------


@pytest.fixture
def fiches_actives():
    """Active l'ecriture des fiches le temps d'un test, puis la remet au repos.

    Les preferences sont partagees par toute la session de tests : les laisser
    actives ferait ecrire des .nfo dans les repertoires temporaires d'autres
    tests, qui comptent leurs fichiers.
    """
    prefs = deps.get_store().load()
    avant = prefs.local_metadata
    prefs.local_metadata = LocalMetadataSettings(nfo=True)
    yield
    prefs.local_metadata = avant


def test_regeneration_refusee_quand_le_reglage_est_au_repos(client) -> None:
    """Le reglage EST le consentement a ecrire dans la bibliotheque."""
    reponse = client.post("/api/review/nfo-library")

    assert reponse.status_code == 400
    assert "désactivée" in reponse.json()["detail"]


def test_regeneration_de_la_bibliotheque(
    client, tmp_path: Path, monkeypatch, fiches_actives
) -> None:
    from sortilege.api import review
    from sortilege.core.parser import parse
    from sortilege.core.probe import FileProbe
    from sortilege.core.scanner import ScannedFile, ScanResult

    film = tmp_path / "Films" / "Dune (2021)" / "Dune (2021).mkv"
    film.parent.mkdir(parents=True)
    film.touch()
    scanne = ScannedFile(
        path=film,
        size_bytes=1024,
        parsed=parse(film, ["Films", film.parent.name]),
        probe=FileProbe(tmdb_id="438631"),
    )
    monkeypatch.setattr(review, "scan", lambda *a, **k: ScanResult(files=[scanne]))

    reponse = client.post("/api/review/nfo-library")

    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["examined"] == 1
    assert corps["written"] == 1
    assert corps["failed"] == 0

    fiche = ElementTree.fromstring(  # noqa: S314
        film.with_suffix(".nfo").read_text(encoding="utf-8")
    )
    # Aucune identification n'a ete refaite : la fiche ne porte que ce que le
    # fichier disait deja de lui-meme.
    assert fiche.findtext("title") == "Dune"
    assert fiche.findtext("uniqueid") == "438631"


def test_les_reglages_voyagent_jusqu_a_l_interface(client) -> None:
    donnees = client.get("/api/settings/preferences").json()["local_metadata"]

    assert donnees["nfo"] is False
    assert "skip" in donnees["on_existing_choices"]


def test_l_api_refuse_une_conduite_inconnue(client) -> None:
    reponse = client.put(
        "/api/settings/preferences", json={"local_metadata": {"on_existing": "efface"}}
    )

    assert reponse.status_code == 400


def test_l_ecriture_reste_dans_le_dossier_du_media(tmp_path: Path, monkeypatch) -> None:
    """Le temporaire vit dans le dossier de destination, jamais dans /tmp.

    ``os.replace`` n'est atomique qu'a l'interieur d'un meme systeme de
    fichiers ; passer par un volume distinct retirerait la garantie qu'on vient
    chercher, et dans un conteneur /tmp EST un volume distinct.
    """
    observes: list[str] = []
    vrai_replace = os.replace

    def espion(source, cible):
        observes.append(str(Path(source).parent))
        return vrai_replace(source, cible)

    cible = tmp_path / "media" / "Dune.nfo"
    cible.parent.mkdir(parents=True)
    monkeypatch.setattr(nfo.os, "replace", espion)

    write_nfo(cible, movie_xml(MediaInfo(title="Dune")))

    assert observes == [str(cible.parent)]
