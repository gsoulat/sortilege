"""Tests de la lecture du fichier lui-meme.

Aucun test ne depend de ffprobe : le parsing .nfo, la fusion et les bornes de
duree sont purs, donc la CI ne devient pas dependante d'un binaire externe.
"""

from pathlib import Path

import pytest

from sortilege.core.probe import (
    FileProbe,
    inspect,
    merge,
    read_nfo,
    runtime_plausible,
)

NFO_KODI = """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>Dune : Deuxieme Partie</title>
  <originaltitle>Dune: Part Two</originaltitle>
  <year>2024</year>
  <uniqueid type="tmdb">693134</uniqueid>
  <uniqueid type="imdb">tt15239678</uniqueid>
</movie>
"""

NFO_RADARR = """<?xml version="1.0" encoding="UTF-8"?>
<movie>
  <title>Severance</title>
  <year>2022</year>
  <tmdbid>95396</tmdbid>
  <imdbid>tt11280740</imdbid>
</movie>
"""

NFO_EPISODE = """<?xml version="1.0" encoding="UTF-8"?>
<episodedetails>
  <title>Chikhai Bardo</title>
  <season>2</season>
  <episode>7</episode>
  <uniqueid type="tvdb">371980</uniqueid>
</episodedetails>
"""


def _write(tmp_path: Path, nfo: str, stem: str = "film") -> Path:
    media = tmp_path / f"{stem}.mkv"
    media.touch()
    (tmp_path / f"{stem}.nfo").write_text(nfo, encoding="utf-8")
    return media


def test_nfo_uniqueid_kodi(tmp_path: Path) -> None:
    probe = read_nfo(_write(tmp_path, NFO_KODI))
    assert probe.tmdb_id == "693134"
    assert probe.imdb_id == "tt15239678"
    assert probe.nfo_year == 2024
    assert probe.has_declared_id


def test_nfo_champs_plats_radarr(tmp_path: Path) -> None:
    probe = read_nfo(_write(tmp_path, NFO_RADARR))
    assert probe.tmdb_id == "95396"
    assert probe.imdb_id == "tt11280740"


def test_nfo_episode(tmp_path: Path) -> None:
    probe = read_nfo(_write(tmp_path, NFO_EPISODE))
    assert probe.container_season == 2
    assert probe.container_episode == 7
    assert probe.tvdb_id == "371980"


def test_nfo_absent_ne_leve_pas(tmp_path: Path) -> None:
    media = tmp_path / "seul.mkv"
    media.touch()
    probe = read_nfo(media)
    assert probe.has_declared_id is False


def test_nfo_illisible_retombe_sur_le_texte_brut(tmp_path: Path) -> None:
    """Beaucoup de .nfo de release sont de l'ASCII art, pas du XML."""
    media = tmp_path / "film.mkv"
    media.touch()
    (tmp_path / "film.nfo").write_text(
        "=== RELEASE GROUP ===\nSource: BluRay\nIMDB: tt0211915\n", encoding="utf-8"
    )
    probe = read_nfo(media)
    assert probe.imdb_id == "tt0211915"


def test_champ_id_generique_ignore_si_pas_un_imdb(tmp_path: Path) -> None:
    """<id>42</id> ne doit pas devenir un identifiant IMDb."""
    media = tmp_path / "film.mkv"
    media.touch()
    (tmp_path / "film.nfo").write_text(
        '<?xml version="1.0"?><movie><id>42</id><title>X</title></movie>', encoding="utf-8"
    )
    assert read_nfo(media).imdb_id is None


def test_nfo_dossier_movie_nfo(tmp_path: Path) -> None:
    media = tmp_path / "quelconque.mkv"
    media.touch()
    (tmp_path / "movie.nfo").write_text(NFO_KODI, encoding="utf-8")
    assert read_nfo(media).tmdb_id == "693134"


# --- Plausibilite de duree --------------------------------------------------


@pytest.mark.parametrize(
    ("duration", "kind", "expected"),
    [
        (120 * 60, "movie", True),
        (22 * 60, "movie", False),  # un « film » de 22 min est un episode
        (22 * 60, "episode", True),
        (52 * 60, "episode", True),
        (150 * 60, "episode", False),  # un « episode » de 2h30 est un film
        (None, "movie", None),  # inconnu n'est pas faux
        (0, "movie", None),
    ],
)
def test_plausibilite_de_duree(duration, kind: str, expected) -> None:
    assert runtime_plausible(duration, kind) is expected


# --- Resolution reelle ------------------------------------------------------


@pytest.mark.parametrize(
    ("height", "label"),
    [(2160, "2160p"), (1080, "1080p"), (800, "720p"), (576, "576p"), (None, None)],
)
def test_libelle_de_resolution(height, label) -> None:
    assert FileProbe(height=height).resolution_label == label


def test_resolution_reelle_contredit_le_nom() -> None:
    """Un fichier annonce 1080p mais encode en 1280x720 est frequent."""
    assert FileProbe(width=1280, height=720).resolution_label == "720p"


# --- Fusion -----------------------------------------------------------------


def test_le_nfo_gagne_sur_les_tags_du_conteneur() -> None:
    media = FileProbe(imdb_id="tt0000001", container_season=9, duration_seconds=3600.0)
    nfo = FileProbe(imdb_id="tt1234567", container_season=2, nfo_title="Vrai Titre")
    out = merge(media, nfo)
    assert out.imdb_id == "tt1234567"
    assert out.container_season == 2
    # Les mesures techniques restent celles du fichier.
    assert out.duration_seconds == 3600.0


def test_fusion_conserve_le_tag_si_le_nfo_est_muet() -> None:
    out = merge(FileProbe(container_episode=7), FileProbe())
    assert out.container_episode == 7


def test_inspect_sans_ffprobe_ne_leve_pas(tmp_path: Path) -> None:
    """Le pipeline doit tourner meme sans binaire externe."""
    probe = inspect(_write(tmp_path, NFO_KODI))
    assert probe.tmdb_id == "693134"
