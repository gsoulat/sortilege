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


# --- Suffixe de saison ------------------------------------------------------
#
# Cas reel : « Walker Texas Ranger Saison 2 » et « Walker.Texas.Ranger.S02 »
# donnaient deux titres differents — « Walker Texas Ranger 2 » et « Walker
# Texas Ranger S02 » — donc DEUX oeuvres distinctes pour une seule serie. Le
# mot « saison » etait bien retire comme bruit, mais le numero restait.


@pytest.mark.parametrize(
    "dossier",
    [
        "Walker Texas Ranger Saison 2",
        "Walker.Texas.Ranger.S02",
        "Walker Texas Ranger Season 02",
        "Walker Texas Ranger S2",
    ],
)
def test_une_serie_ne_s_eclate_pas_selon_l_ecriture_de_sa_saison(dossier: str) -> None:
    """Toutes ces ecritures designent la meme serie : elles doivent donner le
    meme titre, sans quoi la bibliotheque en compte plusieurs."""
    lu = parse(Path("/src") / dossier / "02x01 - Episode.avi", [dossier])

    assert lu.title == "Walker Texas Ranger"
    assert lu.season == 2


@pytest.mark.parametrize(
    ("nom", "titre"),
    [
        # Le « s » de « Ocean's » suivi d'un nombre ressemble a s'y meprendre a
        # un numero de saison. Il est precede d'une apostrophe, pas d'un
        # separateur — c'est toute la difference, et sans elle le titre
        # devenait « Ocean' ».
        ("Ocean's 11 2001.mkv", "Ocean's 11"),
        ("Ocean's 8 2018.mkv", "Ocean's 8"),
        ("Le Cercle 2.mkv", "Le Cercle 2"),
        ("Star Wars 2.mkv", "Star Wars 2"),
        ("Jackass 3 2010.mkv", "Jackass 3"),
    ],
)
def test_un_nombre_du_titre_n_est_pas_pris_pour_une_saison(nom: str, titre: str) -> None:
    assert parse(Path("/src") / nom).title == titre


def test_un_dossier_de_saison_seul_garde_son_nom() -> None:
    """« Saison 2 » comme nom de dossier ne doit pas devenir vide : la remontee
    vers le dossier parent en depend."""
    lu = parse(Path("/src/Severance/Saison 2/S02E01.mkv"), ["Severance", "Saison 2"])

    assert lu.title == "Severance"


# --- Numerotation a trois chiffres ------------------------------------------
#
# Cas reel : « epz-the.vampire.diaries.109.le.cristal.de.la.discorde ». « 109 »
# vaut saison 1 episode 09, convention tres repandue dans les releases
# francaises et invisible pour tous les motifs qui cherchent un S ou un x.
#
# Sans elle, le fichier passe pour un FILM. Et si l'utilisateur corrige le type
# a la main, la destination devient « Season /Titre - SE.avi », avec ses trous.


@pytest.mark.parametrize(
    ("nom", "saison", "episode"),
    [
        ("epz-the.vampire.diaries.109.le.cristal.avi", 1, 9),
        ("epz-the.vampire.diaries.110.le.point.de.non-retour.avi", 1, 10),
        ("The.Wire.201.mkv", 2, 1),
        ("Friends 610 the one with the routine.avi", 6, 10),
    ],
)
def test_trois_chiffres_valent_saison_et_episode(nom: str, saison: int, episode: int) -> None:
    lu = parse(Path("/src") / nom)

    assert lu.kind is MediaKind.EPISODE
    assert (lu.season, lu.episode) == (saison, episode)


@pytest.mark.parametrize(
    "nom",
    [
        # L'episode 00 n'existe pas : c'est ce qui sauve « 300 ».
        "300 (2006).mkv",
        # Quatre chiffres, donc pas le motif.
        "Blade Runner 2049 (2017).mkv",
        # Deux chiffres seulement.
        "Ocean's 11 2001.mkv",
        "Taxi 5 2018.mkv",
        # Episode 65 : au-dela de ce qu'une saison contient.
        "Le.Cercle.365.mkv",
    ],
)
def test_un_nombre_de_titre_ne_devient_pas_un_episode(nom: str) -> None:
    """Les garde-fous tiennent a ce qu'on REFUSE. Se tromper ici ferait ranger
    un film parmi les episodes d'une serie qui n'existe pas."""
    assert parse(Path("/src") / nom).kind is not MediaKind.EPISODE


def test_un_motif_explicite_fait_toujours_foi() -> None:
    """« S01E09 » est sans ambiguite : le motif a trois chiffres n'est tente
    qu'en dernier recours, sans quoi il pourrait le contredire."""
    lu = parse(Path("/src/The.Vampire.Diaries.S02E15.1080p.mkv"))

    assert (lu.season, lu.episode) == (2, 15)


def test_un_anime_en_numerotation_absolue_n_est_pas_lu_a_trois_chiffres() -> None:
    """« [Erai-raws] Frieren - 147 » est l'episode 147 en numerotation absolue,
    pas la saison 1 episode 47. Les deux motifs se disputent les memes chiffres,
    et celui qui a un signal supplementaire — le groupe de fansub — gagne."""
    lu = parse(Path("/src/[Erai-raws] Frieren - 147 [1080p][VOSTFR].mkv"))

    assert lu.kind is MediaKind.ANIME
    assert lu.absolute_episode == 147
    assert lu.season is None


# --- « Ep » plutot que « E » -------------------------------------------------
#
# Cas reel : « Code Quantum S3- Ep18 FRENCH DVDrip Xvid ». Apres « S3 » et son
# separateur, le motif attendait un chiffre juste apres le « E » et butait sur
# le « p ». Le fichier passait pour un FILM, et le titre gardait « S3- Ep18 » :
# chaque episode devenait une oeuvre distincte, dix-huit films nommes « Code
# Quantum S3- EpNN ».


@pytest.mark.parametrize(
    "nom",
    [
        "Code Quantum S3- Ep18 FRENCH DVDrip Xvid.avi",
        "Code Quantum S3-Ep18.avi",
        "Code Quantum S03 Ep18.avi",
        "Code Quantum.S3.Ep18.avi",
        "Code Quantum S3 E18.avi",
    ],
)
def test_ep_vaut_e(nom: str) -> None:
    lu = parse(Path("/src") / nom)

    assert lu.kind is MediaKind.EPISODE
    assert (lu.season, lu.episode) == (3, 18)
    assert lu.title == "Code Quantum"


def test_le_double_episode_accepte_aussi_ep() -> None:
    lu = parse(Path("/src/Stranger Things S04 Ep01-Ep02.mkv"))

    assert (lu.season, lu.episode, lu.episode_end) == (4, 1, 2)
