"""Tests des fichiers compagnons et du nettoyage.

Le test le plus important est celui des sous-titres : les oublier est une perte
de donnees silencieuse, que personne ne remarque avant de lancer la lecture.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sortilege.core.companions import (
    CompanionKind,
    directory_now_empty,
    find_companions,
    find_leftovers,
    trash_destination,
)
from sortilege.core.journal import Journal, apply_plan, undo_last
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """Un dossier de release realiste."""
    folder = tmp_path / "dl" / "Dune.2024.1080p-GROUPE"
    folder.mkdir(parents=True)
    (folder / "Dune.2024.1080p-GROUPE.mkv").write_text("video")
    (folder / "Dune.2024.1080p-GROUPE.fr.srt").write_text("sous-titres fr")
    (folder / "Dune.2024.1080p-GROUPE.en.srt").write_text("sous-titres en")
    (folder / "RARBG.txt").write_text("pub")
    (folder / "poster.jpg").write_text("image")
    return folder


def video_of(folder: Path) -> Path:
    return folder / "Dune.2024.1080p-GROUPE.mkv"


# --- Detection --------------------------------------------------------------


def test_les_sous_titres_sont_trouves(release: Path) -> None:
    companions = find_companions(video_of(release))
    subs = [c for c in companions if c.kind is CompanionKind.SUBTITLE]
    assert len(subs) == 2
    assert {c.suffix for c in subs} == {".fr.srt", ".en.srt"}


def test_le_suffixe_de_langue_est_preserve(release: Path) -> None:
    """Sans lui, les deux pistes se recouvriraient sous le meme nom."""
    companions = find_companions(video_of(release))
    destination = Path("/media/Films/Dune (2024)/Dune (2024).mkv")
    noms = {c.destination_for(destination).name for c in companions}
    assert "Dune (2024).fr.srt" in noms
    assert "Dune (2024).en.srt" in noms


def test_la_jaquette_canonique_est_emportee(release: Path) -> None:
    companions = find_companions(video_of(release))
    assert any(c.kind is CompanionKind.ARTWORK for c in companions)


def test_jaquette_ignoree_si_plusieurs_videos(tmp_path: Path) -> None:
    """« poster.jpg » n'appartient a personne quand le dossier a deux videos."""
    folder = tmp_path / "saison"
    folder.mkdir()
    (folder / "S01E01.mkv").write_text("a")
    (folder / "S01E02.mkv").write_text("b")
    (folder / "poster.jpg").write_text("image")

    companions = find_companions(folder / "S01E01.mkv")
    assert companions == []


def test_les_jaquettes_peuvent_etre_refusees(release: Path) -> None:
    companions = find_companions(video_of(release), artwork=False)
    assert all(c.kind is CompanionKind.SUBTITLE for c in companions)


def test_un_sous_titre_d_une_autre_video_n_est_pas_pris(tmp_path: Path) -> None:
    folder = tmp_path / "melange"
    folder.mkdir()
    (folder / "Film.A.mkv").write_text("a")
    (folder / "Film.B.fr.srt").write_text("appartient a B")

    assert find_companions(folder / "Film.A.mkv") == []


# --- Restes -----------------------------------------------------------------


def test_les_dechets_connus_sont_reperes(release: Path) -> None:
    video = video_of(release)
    leftovers = find_leftovers(video, find_companions(video))
    assert [p.name for p in leftovers] == ["RARBG.txt"]


def test_un_fichier_inconnu_est_laisse_en_place(tmp_path: Path) -> None:
    """La liste des dechets est conservatrice a dessein."""
    folder = tmp_path / "x"
    folder.mkdir()
    video = folder / "film.mkv"
    video.write_text("v")
    (folder / "notes-importantes.odt").write_text("precieux")

    assert find_leftovers(video, []) == []


def test_un_compagnon_n_est_jamais_un_reste(release: Path) -> None:
    video = video_of(release)
    companions = find_companions(video)
    leftovers = find_leftovers(video, companions)
    assert not {p for p in leftovers} & {c.path for c in companions}


def test_chemin_de_corbeille_aplati() -> None:
    target = trash_destination(Path("/media/.corbeille"), "2026-09-08", Path("/dl/rel/RARBG.txt"))
    assert target.parent.name == "2026-09-08"
    assert "dl_rel_RARBG.txt" in target.name


def test_dossier_vide_detecte(tmp_path: Path) -> None:
    vide = tmp_path / "vide"
    vide.mkdir()
    (vide / ".DS_Store").write_text("x")
    assert directory_now_empty(vide) is True

    plein = tmp_path / "plein"
    plein.mkdir()
    (plein / "film.mkv").write_text("x")
    assert directory_now_empty(plein) is False


# --- Application de bout en bout --------------------------------------------


def make_plan(video: Path, destination: Path, companions, leftovers) -> Plan:
    return Plan(
        id="p1",
        source=video,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
        companions=companions,
        leftovers=leftovers,
    )


def test_les_sous_titres_suivent_la_video(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    journal = Journal(tmp_path / "j.jsonl")
    result = apply_plan(plan, journal, dry_run=False, trash_root=lib / ".corbeille")

    assert result.ok
    assert (lib / "Films" / "Dune (2024)" / "Dune (2024).fr.srt").read_text() == "sous-titres fr"
    assert (lib / "Films" / "Dune (2024)" / "Dune (2024).en.srt").read_text() == "sous-titres en"


def test_les_restes_vont_en_corbeille_sans_etre_supprimes(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    trash = lib / ".corbeille"
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    apply_plan(plan, Journal(tmp_path / "j.jsonl"), dry_run=False, trash_root=trash)

    survivants = list(trash.rglob("*RARBG.txt"))
    assert len(survivants) == 1
    assert survivants[0].read_text() == "pub"


def test_le_dossier_vide_disparait(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    apply_plan(
        plan,
        Journal(tmp_path / "j.jsonl"),
        dry_run=False,
        trash_root=lib / ".corbeille",
        source_roots=[tmp_path],
    )

    assert not release.exists()


def test_une_racine_source_n_est_jamais_supprimee(tmp_path: Path) -> None:
    """Un fichier pose directement dans une source : la vider ne doit pas la
    faire disparaitre. Sans cette borne, le scan suivant echouerait sur un
    dossier absent — et l'ancien code n'en avait aucune."""
    from sortilege.core.journal import prune_empty_dirs

    source = tmp_path / "Download"
    source.mkdir()

    assert prune_empty_dirs(source, [source]) == 0
    assert source.is_dir()


def test_le_nettoyage_remonte_plusieurs_niveaux(tmp_path: Path) -> None:
    """Une release occupe souvent deux niveaux ; ne remonter que d'un cran
    laissait une carcasse par episode."""
    from sortilege.core.journal import prune_empty_dirs

    source = tmp_path / "Download"
    profond = source / "Serie" / "Serie S01E01 GROUPE"
    profond.mkdir(parents=True)

    assert prune_empty_dirs(profond, [source]) == 2
    assert not (source / "Serie").exists()
    assert source.is_dir(), "la source elle-meme est intacte"


def test_un_dossier_non_vide_arrete_la_remontee(tmp_path: Path) -> None:
    from sortilege.core.journal import prune_empty_dirs

    source = tmp_path / "Download"
    profond = source / "Serie" / "Serie S01E01"
    profond.mkdir(parents=True)
    (source / "Serie" / "S01E02.mkv").write_text("autre episode", encoding="utf-8")

    assert prune_empty_dirs(profond, [source]) == 1
    assert (source / "Serie").is_dir()


def test_l_annulation_ramene_aussi_les_compagnons(tmp_path: Path, release: Path) -> None:
    """Une annulation partielle serait pire que pas d'annulation du tout."""
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    journal = Journal(tmp_path / "j.jsonl")
    apply_plan(plan, journal, dry_run=False, trash_root=lib / ".corbeille")

    total = len(journal.read_all())
    undo_last(journal, total)

    assert video.is_file()
    assert (release / "Dune.2024.1080p-GROUPE.fr.srt").read_text() == "sous-titres fr"
    assert (release / "RARBG.txt").read_text() == "pub"


def test_la_simulation_ne_touche_a_aucun_compagnon(tmp_path: Path, release: Path) -> None:
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Films" / "Dune (2024)" / "Dune (2024).mkv"

    companions = find_companions(video)
    plan = make_plan(
        video,
        destination,
        [(c.path, c.destination_for(destination)) for c in companions],
        find_leftovers(video, companions),
    )

    apply_plan(plan, Journal(tmp_path / "j.jsonl"), dry_run=True, trash_root=lib / ".corbeille")

    assert (release / "Dune.2024.1080p-GROUPE.fr.srt").is_file()
    assert (release / "RARBG.txt").is_file()
    assert release.is_dir()


def test_sans_corbeille_configuree_rien_n_est_evacue(tmp_path: Path, release: Path) -> None:
    """Mieux vaut laisser du desordre que d'inventer une destination."""
    video = video_of(release)
    lib = tmp_path / "media"
    lib.mkdir()
    destination = lib / "Dune.mkv"

    plan = make_plan(video, destination, [], [release / "RARBG.txt"])
    apply_plan(plan, Journal(tmp_path / "j.jsonl"), dry_run=False, trash_root=None)

    assert (release / "RARBG.txt").is_file()


# --- Balayage des dossiers vides --------------------------------------------
#
# Le nettoyage a la volee ne rattrape que ce qu'il vient de vider. Une
# bibliotheque constituee garde les carcasses des rangements anterieurs, et le
# client de telechargement en cree de son cote.


def test_un_dossier_vide_est_trouve(tmp_path: Path) -> None:
    from sortilege.core.companions import find_empty_dirs

    (tmp_path / "Serie" / "S01E01").mkdir(parents=True)
    trouves = find_empty_dirs([tmp_path])

    assert tmp_path / "Serie" / "S01E01" in trouves


def test_un_parent_de_dossiers_vides_est_vide_aussi(tmp_path: Path) -> None:
    """« Serie/Serie S01E01/ » compte pour DEUX : sans parcours remontant, le
    parent ne serait jamais vu, et une carcasse resterait par serie."""
    from sortilege.core.companions import find_empty_dirs

    (tmp_path / "Serie" / "S01E01").mkdir(parents=True)
    (tmp_path / "Serie" / "S01E02").mkdir()

    trouves = find_empty_dirs([tmp_path])

    assert tmp_path / "Serie" in trouves
    assert len(trouves) == 3


def test_un_dossier_avec_un_fichier_est_epargne(tmp_path: Path) -> None:
    from sortilege.core.companions import find_empty_dirs

    plein = tmp_path / "Occupe"
    plein.mkdir()
    (plein / "film.mkv").write_text("video", encoding="utf-8")

    assert plein not in find_empty_dirs([tmp_path])


def test_les_fichiers_systeme_du_nas_ne_comptent_pas(tmp_path: Path) -> None:
    """Sinon aucun dossier ne serait jamais considere vide sur un NAS."""
    from sortilege.core.companions import find_empty_dirs

    d = tmp_path / "AvecDSStore"
    d.mkdir()
    (d / ".DS_Store").write_text("x", encoding="utf-8")

    assert d in find_empty_dirs([tmp_path])


def test_une_racine_n_est_jamais_proposee(tmp_path: Path) -> None:
    """La supprimer ferait echouer le scan suivant sur un dossier absent."""
    from sortilege.core.companions import find_empty_dirs

    assert tmp_path not in find_empty_dirs([tmp_path])


def test_les_plus_profonds_viennent_en_premier(tmp_path: Path) -> None:
    """C'est l'ordre dans lequel il faut supprimer : un parent ne se retire
    qu'une fois ses enfants partis."""
    from sortilege.core.companions import find_empty_dirs

    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    trouves = find_empty_dirs([tmp_path])

    profondeurs = [len(d.parts) for d in trouves]
    assert profondeurs == sorted(profondeurs, reverse=True)


def test_une_racine_absente_ne_leve_pas(tmp_path: Path) -> None:
    from sortilege.core.companions import find_empty_dirs

    assert find_empty_dirs([tmp_path / "jamais-creee"]) == []


# --- Coquilles : des fichiers, mais plus de video ---------------------------
#
# Ranger un film emporte la video et ses compagnons, mais le dossier de release
# garde ce qui n'accompagnait RIEN : jaquette au nom de la release, .nfo, .xml
# de metadonnees. Le dossier n'est donc jamais vide au sens strict, et le
# balayage des dossiers vides ne le voit pas.


def coquille(root: Path, nom: str, fichiers: list[str]) -> Path:
    d = root / nom
    d.mkdir(parents=True)
    for f in fichiers:
        (d / f).write_text("x" * 50, encoding="utf-8")
    return d


def test_un_dossier_sans_video_est_propose(tmp_path: Path) -> None:
    """Le cas reel : un film range, ses accessoires restes derriere."""
    from sortilege.core.companions import find_orphan_dirs

    d = coquille(
        tmp_path,
        "Alice in Wonderland (1951)",
        ["poster.jpg", "fanart.jpg", "movie.xml", "Alice in Wonderland (1951).nfo"],
    )

    trouves = [o.path for o in find_orphan_dirs([tmp_path])]

    assert d in trouves


def test_un_dossier_contenant_encore_la_video_est_epargne(tmp_path: Path) -> None:
    """C'est le garde-fou principal : un film pas encore range ne doit pas
    perdre sa jaquette."""
    from sortilege.core.companions import find_orphan_dirs

    d = coquille(tmp_path, "Pas Encore Range", ["film.mkv", "poster.jpg"])

    assert d not in [o.path for o in find_orphan_dirs([tmp_path])]


def test_un_type_inattendu_fait_s_abstenir(tmp_path: Path) -> None:
    """Mieux vaut laisser un residu que supprimer ce qu'on n'a pas su
    reconnaitre. Une archive peut contenir n'importe quoi."""
    from sortilege.core.companions import find_orphan_dirs

    d = coquille(tmp_path, "Avec Archive", ["poster.jpg", "bonus.zip"])

    assert d not in [o.path for o in find_orphan_dirs([tmp_path])]


def test_un_dossier_vide_n_est_pas_une_coquille(tmp_path: Path) -> None:
    """Il releve de l'autre balayage : les deux ne doivent pas se marcher
    dessus."""
    from sortilege.core.companions import find_orphan_dirs

    d = tmp_path / "Vide"
    d.mkdir()

    assert d not in [o.path for o in find_orphan_dirs([tmp_path])]


def test_les_sous_titres_orphelins_comptent(tmp_path: Path) -> None:
    """Un .srt sans sa video ne sert plus a rien."""
    from sortilege.core.companions import find_orphan_dirs

    d = coquille(tmp_path, "Sous-titres seuls", ["film.srt", "film.idx"])

    assert d in [o.path for o in find_orphan_dirs([tmp_path])]


def test_la_taille_est_rapportee(tmp_path: Path) -> None:
    """Pour annoncer la place recuperee avant d'agir."""
    from sortilege.core.companions import find_orphan_dirs

    coquille(tmp_path, "Coquille", ["poster.jpg", "fanart.jpg"])

    orphelins = find_orphan_dirs([tmp_path])

    assert orphelins[0].bytes == 100
    assert len(orphelins[0].files) == 2


def test_une_racine_n_est_jamais_une_coquille(tmp_path: Path) -> None:
    from sortilege.core.companions import find_orphan_dirs

    (tmp_path / "poster.jpg").write_text("x", encoding="utf-8")

    assert tmp_path not in [o.path for o in find_orphan_dirs([tmp_path])]


# --- Longueur du nom en corbeille -------------------------------------------
#
# La corbeille aplatit le chemin d'origine dans le nom. Sur une release 4K au
# titre a rallonge, cela depassait les 255 octets qu'un systeme de fichiers
# accorde a un composant, et l'evacuation echouait sur « File name too long » —
# un echec d'autant plus deroutant qu'il ressemble a un manque de place.

EXORCISTE = (
    "/storage/Download/JDownloader2/The Exorcist Believer 2023 MULTi VFF 2160p 10bit "
    "4KLight DV HDR BluRay TrueHD 7 1 Atmos x265-QTZ-Wawacity boats/"
    "The.Exorcist.Believer.2023.MULTi.VFF.2160p.10bit.4KLight.DV.HDR.BluRay.TrueHD.7.1."
    "Atmos.x265-QTZ-Wawacity.boats.mkv"
)


def test_un_nom_trop_long_est_borne() -> None:
    from sortilege.core.companions import MAX_NAME_BYTES, trash_destination

    cible = trash_destination(Path("/lib/.corbeille"), "2026-09-10", Path(EXORCISTE))

    assert len(cible.name.encode("utf-8")) <= MAX_NAME_BYTES


def test_la_fin_du_nom_est_preservee() -> None:
    """C'est elle qui identifie le fichier ; le debut n'est que le chemin des
    dossiers parents."""
    from sortilege.core.companions import trash_destination

    cible = trash_destination(Path("/lib/.corbeille"), "2026-09-10", Path(EXORCISTE))

    assert cible.name.endswith("Wawacity.boats.mkv")


def test_un_nom_court_n_est_pas_touche() -> None:
    from sortilege.core.companions import trash_destination

    cible = trash_destination(Path("/t"), "j", Path("/storage/Download/film.mkv"))

    assert cible.name == "storage_Download_film.mkv"


def test_deux_chemins_tronques_ne_se_recouvrent_pas() -> None:
    """Sans empreinte, deux fichiers de meme fin ecraseraient l'un l'autre en
    corbeille — exactement la perte qu'elle existe pour eviter."""
    from sortilege.core.companions import trash_destination

    a = Path("/storage/Download/" + "A" * 150 + "/meme-fin.mkv")
    b = Path("/storage/Download/" + "B" * 150 + "/meme-fin.mkv")

    assert trash_destination(Path("/t"), "j", a).name != trash_destination(Path("/t"), "j", b).name


def test_un_nom_accentue_reste_valide() -> None:
    """La coupe se fait sur les OCTETS : tronquer au milieu d'un accent
    produirait un nom invalide."""
    from sortilege.core.companions import trash_destination

    chemin = Path("/storage/Download/" + "é" * 200 + "/film.mkv")
    cible = trash_destination(Path("/t"), "j", chemin)

    cible.name.encode("utf-8").decode("utf-8")  # ne doit pas lever


def test_la_corbeille_n_est_jamais_une_coquille(tmp_path: Path) -> None:
    """La corbeille ne contient pas de video : le nettoyage des coquilles la
    proposait, et le mode « supprimer » detruisait ce qu'elle gardait."""
    from sortilege.core.companions import TRASH_DIRNAME, find_orphan_dirs

    lot = tmp_path / TRASH_DIRNAME / "2026-09-11"
    lot.mkdir(parents=True)
    (lot / "Film.nfo").write_text("<movie/>", encoding="utf-8")
    coquille = tmp_path / "Release.2020"
    coquille.mkdir()
    (coquille / "Release.2020.nfo").write_text("<movie/>", encoding="utf-8")

    trouves = find_orphan_dirs([tmp_path])

    assert "Release.2020" in {o.path.name for o in trouves}, "temoin : une vraie coquille"
    assert not any(TRASH_DIRNAME in o.path.parts for o in trouves)


def test_une_saison_en_lien_symbolique_protege_la_serie(tmp_path: Path) -> None:
    """os.walk ne suit pas un lien symbolique : la saison n'est pas parcourue.

    La prendre pour vide faisait proposer au nettoyage la fiche d'une serie
    vivante."""
    from sortilege.core.companions import find_orphan_dirs

    ailleurs = tmp_path / "ailleurs" / "Saison"
    ailleurs.mkdir(parents=True)
    (ailleurs / "Serie.S01E01.mkv").write_bytes(b"x")
    racine = tmp_path / "bibliotheque"
    serie = racine / "Serie (2020)"
    serie.mkdir(parents=True)
    (serie / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")
    (serie / "Season 01").symlink_to(ailleurs, target_is_directory=True)

    assert "Serie (2020)" not in {o.path.name for o in find_orphan_dirs([racine])}


def test_une_saison_illisible_protege_la_serie(tmp_path: Path) -> None:
    import os

    import pytest

    from sortilege.core.companions import find_orphan_dirs

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root lit tout : le cas illisible ne se reproduit pas")
    racine = tmp_path / "bibliotheque"
    serie = racine / "Serie (2020)"
    saison = serie / "Season 01"
    saison.mkdir(parents=True)
    (saison / "Serie.S01E01.mkv").write_bytes(b"x")
    (serie / "tvshow.nfo").write_text("<tvshow/>", encoding="utf-8")
    saison.chmod(0)
    try:
        noms = {o.path.name for o in find_orphan_dirs([racine])}
    finally:
        saison.chmod(0o755)

    assert "Serie (2020)" not in noms
