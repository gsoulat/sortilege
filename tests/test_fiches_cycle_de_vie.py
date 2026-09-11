"""Cycle de vie des fichiers deposes a cote des medias : fiches, affiches, manifestes.

Un fichier depose fait autorite chez le serveur multimedia. Ces tests portent
sur ce qui arrive APRES le premier depot : un second passage, un remplacement,
une regeneration, une annulation, une correction d'identification. C'est la
que la regle cardinale du projet — rien n'est supprime sans corbeille — etait
prise en defaut.

Aucun test ne touche au reseau : les telechargements passent par un transport
simule, ou par une fonction remplacee.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from sortilege.core import nfo, opf
from sortilege.core import probe as sonde
from sortilege.core.companions import TRASH_DIRNAME, find_orphan_dirs
from sortilege.core.ebook import BookMeta
from sortilege.core.journal import (
    Journal,
    _depose_metadonnees,
    apply_plan,
    keep_by_size,
    undo_last,
    undo_plans,
)
from sortilege.core.nfo import (
    LocalMetadataSettings,
    MediaInfo,
    OnExisting,
    Outcome,
    deposit,
    fetch_image,
    info_from_scan,
    movie_xml,
    write_nfo,
)
from sortilege.core.parser import parse
from sortilege.core.planner import Plan
from sortilege.core.probe import FileProbe, read_nfo_file
from sortilege.core.renaming import rename_plans
from sortilege.core.scanner import ScannedFile
from sortilege.core.scoring import Decision
from sortilege.core.subtitles import fetch_missing, missing_languages
from sortilege.providers import opensubtitles

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64

FICHE_TMM = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<movie><title>Dune</title><plot>Ecrit par tinyMediaManager</plot>"
    '<uniqueid type="tmdb" default="true">438631</uniqueid></movie>\n'
)
"""Une fiche soignee posee par un autre outil, avec son identifiant."""


def _film(tmp_path: Path) -> Path:
    film = tmp_path / "media" / "Dune (2021)" / "Dune (2021).mkv"
    film.parent.mkdir(parents=True)
    film.write_bytes(b"video")
    return film


def _contenus(corbeille: Path) -> list[bytes]:
    return sorted(p.read_bytes() for p in corbeille.rglob("*") if p.is_file())


def _plan_film(source: Path, destination: Path, *, tmdb: str = "438631", poster: str = "") -> Plan:
    return Plan(
        id="p-dune",
        source=source,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        title="Dune",
        year=2021,
        provider="tmdb",
        external_id=tmdb,
        poster_url=poster,
        values={"title": "Dune", "year": 2021},
    )


# --- G1 : backup et overwrite ne detruisent rien -----------------------------


def test_deux_passages_en_backup_gardent_l_original_en_corbeille(tmp_path: Path) -> None:
    """Au second passage, l'ancien « .bak » contenait l'original : il etait ecrase."""
    film = _film(tmp_path)
    fiche = film.with_suffix(".nfo")
    fiche.write_text(FICHE_TMM, encoding="utf-8")
    corbeille = tmp_path / "media" / TRASH_DIRNAME
    reglages = LocalMetadataSettings(nfo=True, on_existing="backup")

    deposit(
        film,
        "movie",
        MediaInfo(title="Dune", year=2021, tmdb_id="438631"),
        reglages,
        trash_root=corbeille,
    )
    deposit(
        film,
        "movie",
        MediaInfo(title="Dune", year=2022, tmdb_id="438631"),
        reglages,
        trash_root=corbeille,
    )

    contenus = _contenus(corbeille)
    assert FICHE_TMM.encode("utf-8") in contenus
    assert len(contenus) == 2, "l'original et notre premiere fiche, rien d'ecrase"
    assert "<year>2022</year>" in fiche.read_text(encoding="utf-8")
    assert not list(film.parent.glob("*.bak"))


def test_un_contenu_identique_ne_remplit_pas_la_corbeille(tmp_path: Path) -> None:
    """Chaque episode reecrit « tvshow.nfo » : a l'identique, rien ne doit bouger."""
    film = _film(tmp_path)
    corbeille = tmp_path / "corbeille"
    reglages = LocalMetadataSettings(nfo=True, on_existing="overwrite")
    info = MediaInfo(title="Dune", year=2021, tmdb_id="438631")

    deposit(film, "movie", info, reglages, trash_root=corbeille)
    second = deposit(film, "movie", info, reglages, trash_root=corbeille)

    assert second.skipped == [film.with_suffix(".nfo")]
    assert not corbeille.exists()


def test_sans_corbeille_un_bak_existant_n_est_jamais_ecrase(tmp_path: Path) -> None:
    cible = tmp_path / "Dune.nfo"
    cible.write_text("original", encoding="utf-8")

    write_nfo(cible, movie_xml(MediaInfo(title="Dune")), on_existing=OnExisting.BACKUP)
    write_nfo(cible, movie_xml(MediaInfo(title="Dune", year=2021)), on_existing=OnExisting.BACKUP)

    assert (tmp_path / "Dune.nfo.bak").read_text(encoding="utf-8") == "original"
    horodatees = list(tmp_path.glob("Dune.nfo.*.bak"))
    assert len(horodatees) == 1
    assert "<title>Dune</title>" in horodatees[0].read_text(encoding="utf-8")


def test_overwrite_envoie_l_existant_en_corbeille(tmp_path: Path) -> None:
    cible = tmp_path / "media" / "Dune.nfo"
    cible.parent.mkdir()
    cible.write_text("ancien", encoding="utf-8")
    corbeille = tmp_path / "corbeille"

    resultat = write_nfo(
        cible,
        movie_xml(MediaInfo(title="Dune")),
        on_existing=OnExisting.OVERWRITE,
        trash_root=corbeille,
    )

    assert resultat is Outcome.REPLACED
    assert _contenus(corbeille) == [b"ancien"]


def test_overwrite_sans_corbeille_ne_detruit_rien(tmp_path: Path) -> None:
    cible = tmp_path / "Dune.nfo"
    cible.write_text("ancien", encoding="utf-8")

    write_nfo(cible, movie_xml(MediaInfo(title="Dune")), on_existing=OnExisting.OVERWRITE)

    assert [p.read_text(encoding="utf-8") for p in tmp_path.glob("Dune.nfo.*.bak")] == ["ancien"]


def test_une_affiche_remplacee_part_en_corbeille(tmp_path: Path) -> None:
    film = _film(tmp_path)
    affiche = film.parent / "poster.jpg"
    affiche.write_bytes(b"ancienne")
    corbeille = tmp_path / "corbeille"
    reponses = {
        "https://image.tmdb.org/t/p/w780/p.jpg": httpx.Response(
            200, content=JPEG, headers={"content-type": "image/jpeg"}
        )
    }
    transport = httpx.MockTransport(lambda r: reponses.get(str(r.url), httpx.Response(404)))

    with httpx.Client(transport=transport) as client:
        depot = deposit(
            film,
            "movie",
            MediaInfo(title="Dune", poster_url="https://image.tmdb.org/t/p/w185/p.jpg"),
            LocalMetadataSettings(artwork=True, on_existing="overwrite"),
            client=client,
            trash_root=corbeille,
        )

    assert affiche.read_bytes() == JPEG
    assert _contenus(corbeille) == [b"ancienne"]
    assert depot.set_aside == [(affiche, next(p for p in corbeille.rglob("*") if p.is_file()))]


def test_le_manifeste_de_calibre_survit_a_deux_passages(tmp_path: Path) -> None:
    livre = tmp_path / "Livre" / "x.epub"
    livre.parent.mkdir()
    livre.write_bytes(b"")
    (livre.parent / "metadata.opf").write_text("calibre", encoding="utf-8")
    corbeille = tmp_path / "corbeille"

    opf.deposit(livre, BookMeta(title="A"), on_existing=OnExisting.BACKUP, trash_root=corbeille)
    opf.deposit(livre, BookMeta(title="B"), on_existing=OnExisting.BACKUP, trash_root=corbeille)

    assert b"calibre" in _contenus(corbeille)
    assert "B" in (livre.parent / "metadata.opf").read_text(encoding="utf-8")


# --- G2 : la regeneration garde l'identifiant ---------------------------------


def test_la_regeneration_sans_sonde_garde_l_identifiant(tmp_path: Path) -> None:
    """Sans sonde simulee : un scan rapide rend une sonde VIDE, rien de plus."""
    film = _film(tmp_path)
    fiche = film.with_suffix(".nfo")
    fiche.write_text(FICHE_TMM, encoding="utf-8")
    scanne = ScannedFile(
        path=film,
        size_bytes=1024,
        parsed=parse(film, ["media", film.parent.name]),
        probe=FileProbe(),
    )

    info = info_from_scan(scanne)
    deposit(
        film,
        "movie",
        info,
        LocalMetadataSettings(nfo=True, on_existing="overwrite"),
        trash_root=tmp_path / "corbeille",
    )

    assert info.tmdb_id == "438631"
    assert read_nfo_file(fiche).tmdb_id == "438631"


def test_un_episode_reprend_l_identifiant_de_la_serie_et_non_le_sien(tmp_path: Path) -> None:
    racine = tmp_path / "Severance (2022)"
    episode = racine / "Season 01" / "Severance - S01E01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"video")
    (racine / "tvshow.nfo").write_text(
        '<tvshow><title>Severance</title><uniqueid type="tmdb">95396</uniqueid></tvshow>',
        encoding="utf-8",
    )
    episode.with_suffix(".nfo").write_text(
        '<episodedetails><uniqueid type="tvdb">8000001</uniqueid></episodedetails>',
        encoding="utf-8",
    )
    scanne = ScannedFile(
        path=episode,
        size_bytes=1024,
        parsed=parse(episode, [racine.name, "Season 01"]),
        probe=FileProbe(),
    )

    info = info_from_scan(scanne)

    assert info.tmdb_id == "95396"
    assert info.tvdb_id == "", "l'identifiant d'un episode serait devenu celui de la serie"


def test_une_fiche_identifiee_n_est_jamais_remplacee_par_une_fiche_sans(tmp_path: Path) -> None:
    cible = tmp_path / "Dune.nfo"
    cible.write_text(FICHE_TMM, encoding="utf-8")

    resultat = write_nfo(
        cible, movie_xml(MediaInfo(title="Dune")), on_existing=OnExisting.OVERWRITE
    )

    assert resultat is Outcome.SKIPPED
    assert cible.read_text(encoding="utf-8") == FICHE_TMM


# --- G3 : une serie vivante n'est pas une coquille ----------------------------


def test_une_serie_vivante_n_est_pas_une_coquille(tmp_path: Path) -> None:
    racine = tmp_path / "Severance (2022)"
    saison = racine / "Season 01"
    saison.mkdir(parents=True)
    (racine / "tvshow.nfo").write_text("x", encoding="utf-8")
    (racine / "poster.jpg").write_bytes(b"x")
    (saison / "Severance - S01E01.mkv").write_bytes(b"v")

    trouves = [o.path for o in find_orphan_dirs([tmp_path])]

    assert racine not in trouves
    assert saison not in trouves


def test_le_manifeste_d_un_livre_parti_est_une_coquille(tmp_path: Path) -> None:
    dossier = tmp_path / "Auteur" / "Livre (2010)"
    dossier.mkdir(parents=True)
    (dossier / "metadata.opf").write_text("x", encoding="utf-8")
    (dossier / "cover.jpg").write_bytes(b"x")

    assert dossier in [o.path for o in find_orphan_dirs([tmp_path])]


# --- G4 : corriger une identification ne laisse pas la mauvaise fiche --------


def test_annuler_met_fiche_et_affiche_en_corbeille(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(nfo, "_telecharger_image", lambda url, http: JPEG)
    source = tmp_path / "dl" / "dune.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    destination = tmp_path / "media" / "Dune (2021)" / "Dune (2021).mkv"
    corbeille = tmp_path / "media" / TRASH_DIRNAME
    journal = Journal(tmp_path / "journal.jsonl")
    plan = _plan_film(source, destination, poster="https://image.tmdb.org/t/p/w185/p.jpg")

    resultat = apply_plan(
        plan,
        journal,
        dry_run=False,
        trash_root=corbeille,
        local_metadata=LocalMetadataSettings(nfo=True, artwork=True),
    )
    fiche = destination.with_suffix(".nfo")
    affiche = destination.parent / "poster.jpg"
    assert resultat.ok and fiche.is_file() and affiche.is_file()
    assert [r.kind for r in journal.read_all()].count("metadata") == 2

    resultats = undo_plans(journal, [plan.id])

    assert all(r.ok for r in resultats)
    assert source.read_bytes() == b"video"
    assert not fiche.exists()
    assert not affiche.exists()
    noms = [p.name for p in corbeille.rglob("*") if p.is_file()]
    assert any(n.endswith("Dune (2021).nfo") for n in noms)
    assert any(n.endswith("poster.jpg") for n in noms)
    assert journal.read_all() == []


def test_annuler_un_episode_garde_la_fiche_de_la_serie(tmp_path: Path) -> None:
    """« tvshow.nfo » sert toute la serie : un episode annule ne l'emporte pas."""
    racine = tmp_path / "media" / "Severance (2022)"
    corbeille = tmp_path / "media" / TRASH_DIRNAME
    journal = Journal(tmp_path / "journal.jsonl")
    plans = []
    for numero in (1, 2):
        source = tmp_path / "dl" / f"severance.s01e0{numero}.mkv"
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(b"video")
        plan = Plan(
            id=f"ep{numero}",
            source=source,
            destination=racine / "Season 01" / f"Severance - S01E0{numero}.mkv",
            kind="episode",
            score=0.95,
            decision=Decision.AUTO,
            title="Severance",
            provider="tmdb",
            external_id="95396",
            values={"title": "Severance", "season": 1, "episode": numero},
        )
        apply_plan(
            plan,
            journal,
            dry_run=False,
            trash_root=corbeille,
            local_metadata=LocalMetadataSettings(nfo=True),
        )
        plans.append(plan)

    undo_plans(journal, ["ep1"])

    assert (racine / "tvshow.nfo").is_file(), "l'episode 2 est encore la"
    assert not plans[0].destination.with_suffix(".nfo").exists()

    undo_plans(journal, ["ep1", "ep2"])

    assert not (racine / "tvshow.nfo").exists()
    assert journal.read_all() == []


def test_une_fiche_d_une_autre_oeuvre_est_remplacee_meme_en_skip(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(nfo, "_telecharger_image", lambda url, http: JPEG)
    source = tmp_path / "dl" / "dune.mkv"
    source.parent.mkdir()
    source.write_bytes(b"video")
    destination = tmp_path / "media" / "Dune (2021)" / "Dune (2021).mkv"
    destination.parent.mkdir(parents=True)
    fiche = destination.with_suffix(".nfo")
    fiche.write_text(movie_xml(MediaInfo(title="Dune", tmdb_id="841")), encoding="utf-8")
    affiche = destination.parent / "poster.jpg"
    affiche.write_bytes(b"affiche du Dune de 1984")
    corbeille = tmp_path / "media" / TRASH_DIRNAME

    resultat = apply_plan(
        _plan_film(source, destination, poster="https://image.tmdb.org/t/p/w185/p.jpg"),
        Journal(tmp_path / "journal.jsonl"),
        dry_run=False,
        trash_root=corbeille,
        local_metadata=LocalMetadataSettings(nfo=True, artwork=True),  # conduite : skip
    )

    assert resultat.ok
    assert read_nfo_file(fiche).tmdb_id == "438631"
    assert affiche.read_bytes() == JPEG
    assert "autre œuvre" in resultat.message
    contenus = _contenus(corbeille)
    assert b"affiche du Dune de 1984" in contenus
    assert any(b">841<" in c for c in contenus)


def test_un_journal_ancien_se_relit_et_s_annule(tmp_path: Path) -> None:
    origine = tmp_path / "dl" / "a.mkv"
    origine.parent.mkdir()
    arrivee = tmp_path / "media" / "a.mkv"
    arrivee.parent.mkdir()
    arrivee.write_bytes(b"video")
    chemin = tmp_path / "journal.jsonl"
    lignes = [
        # Avant l'ajout de la nature, du titre et de la corbeille.
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "plan_id": "vieux",
            "source": "/nulle/part",
            "destination": "/nulle/ailleurs",
            "method": "rename",
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00",
            "plan_id": "p1",
            "source": str(origine),
            "destination": str(arrivee),
            "method": "rename",
            "kind": "video",
            "title": "A",
            "work_kind": "movie",
        },
    ]
    chemin.write_text("".join(json.dumps(ligne) + "\n" for ligne in lignes), encoding="utf-8")
    journal = Journal(chemin)

    relues = journal.read_all()
    resultats = undo_last(journal, 1)

    assert len(relues) == 2
    assert all(r.trash_root == "" for r in relues)
    assert [r.ok for r in resultats] == [True]
    assert origine.read_bytes() == b"video"


# --- G5 et G6 : la remise en conformite des noms ------------------------------


def _en_bibliotheque(root: Path, relatif: str) -> ScannedFile:
    chemin = root / relatif
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_bytes(b"video")
    return ScannedFile(
        path=chemin,
        size_bytes=1024,
        parsed=parse(chemin, list(Path(relatif).parts[:-1])),
        probe=FileProbe(),
    )


GABARIT_SERIE = "{title}/Season {season:02}/{title} - S{season:02}E{episode:02}"


def test_le_renommage_emporte_un_sous_titre_sans_planter(tmp_path: Path) -> None:
    scanne = _en_bibliotheque(tmp_path, "Severance/severance.s01e01.mkv")
    sous_titre = scanne.path.with_name("severance.s01e01.fr.srt")
    sous_titre.write_text("1", encoding="utf-8")

    plans = rename_plans([scanne], template=GABARIT_SERIE, destination_root=tmp_path)

    destination = plans[0].destination
    assert plans[0].companions == [
        (sous_titre, destination.with_name(destination.stem + ".fr.srt"))
    ]


def test_le_renommage_emporte_la_fiche_et_n_en_depose_aucune(tmp_path: Path) -> None:
    scanne = _en_bibliotheque(tmp_path, "Severance/severance.s01e01.mkv")
    fiche = scanne.path.with_suffix(".nfo")
    contenu = '<episodedetails><uniqueid type="tvdb">8000001</uniqueid></episodedetails>'
    fiche.write_text(contenu, encoding="utf-8")
    journal = Journal(tmp_path / "journal.jsonl")
    plan = rename_plans([scanne], template=GABARIT_SERIE, destination_root=tmp_path)[0]

    resultat = apply_plan(
        plan,
        journal,
        dry_run=False,
        trash_root=tmp_path / TRASH_DIRNAME,
        local_metadata=LocalMetadataSettings(nfo=True, on_existing="overwrite"),
    )

    assert resultat.ok
    assert plan.destination.with_suffix(".nfo").read_text(encoding="utf-8") == contenu
    assert not fiche.exists()
    assert not list(tmp_path.rglob("tvshow.nfo")), "une fiche pauvre a ete deposee"
    assert "metadata" not in {r.kind for r in journal.read_all()}


def test_un_livre_sans_fournisseur_garde_son_manifeste(tmp_path: Path) -> None:
    """Le critere du plan de renommage ne doit pas attraper les livres."""
    livre = tmp_path / "Auteur - Titre.epub"
    livre.write_bytes(b"pas un vrai epub")
    plan = Plan(
        id="b1",
        source=tmp_path / "source.epub",
        destination=livre,
        kind="book",
        score=0.6,
        decision=Decision.REVIEW,
        title="Titre",
        identified_by="nom",
    )
    assert not plan.provider and not plan.values

    resume = _depose_metadonnees(plan, LocalMetadataSettings(opf=True))

    assert "manifeste" in resume
    assert (tmp_path / "metadata.opf").is_file()


def test_un_plan_identifie_sans_fournisseur_garde_sa_fiche(tmp_path: Path) -> None:
    """Seule la marque de ``rename_plans`` fait renoncer au depot : un plan
    sans fournisseur ni valeurs n'est pas pour autant un renommage."""
    film = _film(tmp_path)
    plan = Plan(
        id="p-main",
        source=tmp_path / "dl" / "dune.mkv",
        destination=film,
        kind="movie",
        score=0.97,
        decision=Decision.AUTO,
        title="Dune",
        year=2021,
    )

    resume = _depose_metadonnees(plan, LocalMetadataSettings(nfo=True))

    assert "fiche" in resume
    assert film.with_suffix(".nfo").is_file()


def test_la_marque_de_renommage_survit_a_la_confirmation(tmp_path: Path) -> None:
    """La confirmation reecrit score, decision et motifs : la marque, elle, reste."""
    from sortilege.core.renaming import RENAME_MARKER

    scanne = _en_bibliotheque(tmp_path, "Severance/severance.s01e01.mkv")
    plan = rename_plans([scanne], template=GABARIT_SERIE, destination_root=tmp_path)[0]
    plan.decision, plan.score = Decision.AUTO, 1.0
    plan.reasons = ["identification confirmee a la main"]

    assert plan.values.get(RENAME_MARKER) is True
    assert _depose_metadonnees(plan, LocalMetadataSettings(nfo=True)) == ""


# --- G9 : le doublon perdant emporte ses sous-titres --------------------------


def test_le_fichier_remplace_emporte_ses_sous_titres_en_corbeille(tmp_path: Path) -> None:
    source = tmp_path / "dl" / "Dune (2021).mkv"
    source.parent.mkdir()
    source.write_bytes(b"x" * 10)
    destination = _film(tmp_path)
    destination.write_bytes(b"x" * 100)
    sous_titre = destination.with_name("Dune (2021).fr.srt")
    sous_titre.write_text("ancien", encoding="utf-8")
    journal = Journal(tmp_path / "journal.jsonl")
    plan = Plan(
        id="p-doublon",
        source=source,
        destination=destination,
        kind="movie",
        score=1.0,
        decision=Decision.AUTO,
        title="Dune",
    )

    resultat = keep_by_size(plan, journal, tmp_path / "corbeille", keep="smaller")

    assert resultat.ok
    assert destination.read_bytes() == b"x" * 10
    assert not sous_titre.exists()
    assert "sous-titre" in resultat.message
    ecartes = [r for r in journal.read_all() if r.kind == "trash" and r.source == str(sous_titre)]
    assert len(ecartes) == 1
    assert Path(ecartes[0].destination).read_text(encoding="utf-8") == "ancien"


# --- F7 : la piste audio suffit, et les sous-titres integres comptent ---------


class _SourceMuette:
    def __init__(self) -> None:
        self.recherches: list[dict] = []

    async def find(self, **kwargs):
        self.recherches.append(kwargs)
        return []

    async def download(self, candidate):
        return None


async def test_la_piste_audio_suffit_quand_on_le_demande(tmp_path: Path) -> None:
    film = tmp_path / "Film (2024).mkv"
    film.write_bytes(b"\x01" * 1024)
    audio_francais = FileProbe(audio_languages=["fre"])
    source = _SourceMuette()

    assert (
        await fetch_missing(film, ["fr"], source, probe=audio_francais, audio_is_enough=True) == []
    )
    assert source.recherches == []

    await fetch_missing(film, ["fr"], source, probe=audio_francais)
    assert len(source.recherches) == 1, "sans le reglage, l'audio ne remplace pas un sous-titre"


def test_la_sonde_remonte_les_sous_titres_integres_hors_forces(tmp_path: Path) -> None:
    film = tmp_path / "Film.mkv"
    film.write_bytes(b"v")
    resultat = FileProbe()

    sonde._fill_streams(
        resultat,
        [
            {"codec_type": "subtitle", "tags": {"language": "fre"}},
            {"codec_type": "subtitle", "tags": {"language": "eng"}, "disposition": {"forced": 1}},
        ],
    )

    assert resultat.subtitle_languages == ["fre"]
    assert missing_languages(film, ["fr", "en"], probe=resultat) == ["en"]


# --- F18 : un fichier sans fiche est signale ----------------------------------


def test_un_episode_sans_numerotation_est_signale(tmp_path: Path) -> None:
    episode = tmp_path / "One Piece" / "One Piece - 1071.mkv"
    episode.parent.mkdir()
    episode.touch()

    depot = deposit(episode, "anime", MediaInfo(title="One Piece"), LocalMetadataSettings(nfo=True))

    assert depot.unnumbered == [episode]
    assert "sans fiche" in depot.summary()


def test_un_episode_sans_dossier_d_oeuvre_est_signale(tmp_path: Path) -> None:
    episode = tmp_path / "Divers" / "x.mkv"
    episode.parent.mkdir()
    episode.touch()
    info = MediaInfo(title="Severance", season=1, episode=1)

    depot = deposit(episode, "episode", info, LocalMetadataSettings(nfo=True))

    assert depot.unnumbered == [episode]
    assert depot.written == [episode.with_suffix(".nfo")]


# --- F23 : les telechargements sont lus en flux, sous plafond -----------------


def test_une_affiche_annoncee_trop_lourde_n_est_pas_lue(tmp_path: Path) -> None:
    cible = tmp_path / "poster.jpg"
    lus: list[int] = []

    def corps():
        lus.append(1)
        yield JPEG

    def repondre(requete: httpx.Request) -> httpx.Response:
        entetes = {"content-type": "image/jpeg", "content-length": str(nfo.MAX_IMAGE_BYTES + 1)}
        return httpx.Response(200, headers=entetes, content=corps())

    with httpx.Client(transport=httpx.MockTransport(repondre)) as client:
        assert fetch_image("https://x.example/p.jpg", cible, client=client) is False

    assert lus == []
    assert not cible.exists()


def test_une_affiche_sans_taille_annoncee_est_coupee_au_plafond(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(nfo, "MAX_IMAGE_BYTES", 100)
    cible = tmp_path / "poster.jpg"
    servis: list[int] = []

    def corps():
        for _ in range(1000):
            servis.append(1)
            yield b"x" * 40

    def repondre(requete: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=corps())

    with httpx.Client(transport=httpx.MockTransport(repondre)) as client:
        assert fetch_image("https://x.example/p.jpg", cible, client=client) is False

    assert len(servis) <= 4
    assert not cible.exists()


async def test_un_sous_titre_sans_taille_annoncee_est_coupe_au_plafond(monkeypatch) -> None:
    monkeypatch.setattr(opensubtitles, "TAILLE_MAX", 100)
    servis: list[int] = []

    async def corps():
        for _ in range(1000):
            servis.append(1)
            yield b"x" * 40

    transport = httpx.MockTransport(lambda requete: httpx.Response(200, content=corps()))
    async with httpx.AsyncClient(transport=transport) as client:
        fournisseur = opensubtitles.OpenSubtitlesProvider("cle", client=client)
        assert await fournisseur._retirer("https://dl.example/s.srt") is None

    assert len(servis) <= 4


async def test_un_sous_titre_annonce_trop_lourd_n_est_pas_lu() -> None:
    lus: list[int] = []

    async def corps():
        lus.append(1)
        yield b"1"

    def repondre(requete: httpx.Request) -> httpx.Response:
        entetes = {"content-length": str(opensubtitles.TAILLE_MAX + 1)}
        return httpx.Response(200, headers=entetes, content=corps())

    async with httpx.AsyncClient(transport=httpx.MockTransport(repondre)) as client:
        fournisseur = opensubtitles.OpenSubtitlesProvider("cle", client=client)
        assert await fournisseur._retirer("https://dl.example/s.srt") is None

    assert lus == []


async def test_un_sous_titre_normal_est_rendu_entier() -> None:
    transport = httpx.MockTransport(lambda requete: httpx.Response(200, content=b"1\n00:00"))
    async with httpx.AsyncClient(transport=transport) as client:
        fournisseur = opensubtitles.OpenSubtitlesProvider("cle", client=client)
        assert await fournisseur._retirer("https://dl.example/s.srt") == b"1\n00:00"


# --- F25 : un disque qui refuse se voit dans les journaux ---------------------


def test_une_couverture_non_ecrite_est_un_avertissement(tmp_path: Path, monkeypatch) -> None:
    livre = tmp_path / "x.epub"
    livre.write_bytes(b"")
    avertissements: list[str] = []

    def disque_plein(*args, **kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(opf, "_extract_cover", disque_plein)
    monkeypatch.setattr(opf.logger, "warning", lambda msg, *a: avertissements.append(msg % a))

    depot = opf.deposit(livre, BookMeta(title="T"))

    assert depot.manifest is Outcome.WRITTEN
    assert depot.cover is None
    assert any("disque plein" in a for a in avertissements)


def test_une_affiche_non_ecrite_est_un_avertissement(tmp_path: Path, monkeypatch) -> None:
    avertissements: list[str] = []

    def disque_plein(*args, **kwargs):
        raise OSError("disque plein")

    monkeypatch.setattr(nfo, "_telecharger_image", lambda url, http: JPEG)
    monkeypatch.setattr(nfo, "place_file", disque_plein)
    monkeypatch.setattr(nfo.logger, "warning", lambda msg, *a: avertissements.append(msg % a))

    assert fetch_image("https://x.example/p.jpg", tmp_path / "poster.jpg") is False
    assert any("disque plein" in a for a in avertissements)
