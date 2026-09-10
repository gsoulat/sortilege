"""Tests du parseur de noms de release.

Les cas sont des noms reels, pas des exemples inventes : c'est la seule facon
de savoir si le parseur tient face a ce que produisent vraiment les groupes.
"""

from pathlib import Path

import pytest

from sortilege.core.parser import MediaKind, is_video, parse


@pytest.mark.parametrize(
    ("filename", "title", "year"),
    [
        ("Films/Dune.Part.Two.2024.1080p.BluRay.x265-GROUPE.mkv", "Dune Part Two", 2024),
        (
            "Films/Le.Fabuleux.Destin.d.Amelie.Poulain.2001.FRENCH.1080p.mkv",
            "Le Fabuleux Destin d Amelie Poulain",
            2001,
        ),
        ("Films/Blade Runner 2049 (2017) [2160p] [HDR].mkv", "Blade Runner 2049", 2017),
    ],
)
def test_films(filename: str, title: str, year: int) -> None:
    p = parse(Path(filename))
    assert p.kind is MediaKind.MOVIE
    assert p.title == title
    assert p.year == year


@pytest.mark.parametrize(
    ("filename", "season", "episode"),
    [
        ("Severance.S02E07.MULTI.1080p.WEB-DL.x264.mkv", 2, 7),
        ("Kaamelott - S01E12 - Le Banquet.avi", 1, 12),
        ("The.Wire.3x11.HDTV.mkv", 3, 11),
        ("Engrenages.Saison.4.Episode.03.FRENCH.mkv", 4, 3),
    ],
)
def test_episodes(filename: str, season: int, episode: int) -> None:
    p = parse(Path(filename))
    assert p.kind is MediaKind.EPISODE
    assert p.season == season
    assert p.episode == episode


def test_anime_numerotation_absolue() -> None:
    p = parse(Path("jd-nas/[Erai-raws] Frieren - 147 [1080p][VOSTFR].mkv"))
    assert p.kind is MediaKind.ANIME
    assert p.absolute_episode == 147
    assert p.fansub_group == "Erai-raws"
    assert "Frieren" in p.title


def test_champs_techniques() -> None:
    p = parse(Path("Dune.2024.1080p.BluRay.x265.MULTI.mkv"))
    assert p.resolution == "1080p"
    assert p.source == "bluray"
    assert p.codec == "x265"
    assert p.language == "multi"


def test_titre_repris_du_dossier_parent() -> None:
    """Beaucoup de releases ne mettent le titre que sur le dossier."""
    p = parse(Path("Interstellar (2014)/vlc-record-0001.mkv"))
    assert p.year == 2014


def test_qualite_reflete_l_incertitude() -> None:
    riche = parse(Path("Dune.Part.Two.2024.1080p.mkv"))
    pauvre = parse(Path("aa.mkv"))
    assert riche.quality > pauvre.quality
    assert 0.0 <= pauvre.quality <= 1.0
    assert 0.0 <= riche.quality <= 1.0


def test_annee_d_encodage_non_confondue_avec_resolution() -> None:
    """1080 ne doit jamais etre lu comme une annee."""
    p = parse(Path("Un.Film.1080p.x264.mkv"))
    assert p.year != 1080


@pytest.mark.parametrize(
    ("filename", "title", "year"),
    [
        # Le titre contient un nombre qui ressemble a une annee.
        ("Blade Runner 2049 (2017) [2160p].mkv", "Blade Runner 2049", 2017),
        ("2012 (2009) 1080p BluRay.mkv", "2012", 2009),
        ("Blade.Runner.2049.2017.1080p.BluRay.x265.mkv", "Blade Runner 2049", 2017),
    ],
)
def test_nombre_du_titre_non_pris_pour_l_annee(filename: str, title: str, year: int) -> None:
    """Regression : « 2049 » appartient au titre, pas a la date de sortie.

    Deux regles : une annee entre parentheses gagne ; sinon on prend la
    derniere, puisqu'un titre precede toujours son annee.
    """
    p = parse(Path(filename))
    assert p.year == year
    assert p.title == title


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.mkv", True), ("a.MP4", True), ("a.nfo", False), ("a.srt", False), ("a", False)],
)
def test_detection_video(name: str, expected: bool) -> None:
    assert is_video(Path(name)) is expected


# --- Signatures de sites de telechargement ----------------------------------
#
# Cas reel, releve dans une vraie source : les sites de telechargement direct
# signent les fichiers qu'ils diffusent et y collent un mot au hasard. Quand la
# release porte une annee, la coupure a l'annee suffisait ; SANS annee, le
# titre gardait « Wawacity ec » et aucun fournisseur ne reconnaissait rien.


@pytest.mark.parametrize(
    ("nom", "titre"),
    [
        ("ange ou démon Wawacity bz.mp4", "ange ou démon"),
        ("Asterix Aux Jeux Olympiques Wawacity ec.avi", "Asterix Aux Jeux Olympiques"),
        ("Bruce tout puissant Wawacity ec.mkv", "Bruce tout puissant"),
        ("Le.Grand.Bleu.Zone-Telechargement.mkv", "Le Grand Bleu"),
        ("Heat.1080p.x264-Torrent9.mkv", "Heat"),
    ],
)
def test_la_signature_du_site_et_sa_suite_disparaissent(nom: str, titre: str) -> None:
    """Le suffixe aleatoire n'appartient a aucune liste : il ne peut etre
    reconnu que par sa POSITION, apres la signature. D'ou une coupure et non un
    retrait de mot."""
    assert parse(Path("/src") / nom).title == titre


def test_un_titre_reduit_a_la_signature_est_conserve() -> None:
    """Mieux vaut un fichier nomme d'apres le site que pas nomme du tout : sans
    titre, il ne serait plus rattachable a rien."""
    assert parse(Path("/src/Wawacity.mp4")).title == "Wawacity"


def test_l_annee_reste_lue_malgre_la_signature() -> None:
    nom = "Almost Family 2025 Multi WEBRIP-Wawacity motorcycles.mp4"
    lu = parse(Path("/src") / nom)

    assert lu.title == "Almost Family"
    assert lu.year == 2025
