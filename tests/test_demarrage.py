"""Tests du demarrage face a un fichier de reglages abime ou devenu invalide.

Le demarrage est le seul moment ou Sortilege ecrit sans personne devant l'ecran :
la cle d'API y est generee. Ecrire sur un fichier illisible y remplacait tous
les reglages par les defauts ; ecrire en validant tout y faisait redemarrer le
conteneur en boucle pour une source demontee. Ces tests passent par le vrai
cycle de vie de l'application, pas par le magasin seul.

Aucun ne touche au dossier « data/ » du depot : ``deps.DATA_DIR`` est detourne.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps
from sortilege.core import apikey
from sortilege.core.preferences import (
    PreferenceError,
    PreferenceStore,
    SubtitleSettings,
    _bloc,
)
from sortilege.main import app

PASSWORD = "mot-de-passe-de-test"

VIRGULE_EN_TROP = (
    "{\n"
    '  "destinations": {"movie": "Cinema a moi"},\n'
    '  "metadata": {"tmdb_api_key": "cle-tmdb-precieuse", "language": "en-US"},\n'
    "}\n"
)
"""Le JSON d'un humain qui a edite le fichier a la main : une virgule de trop."""


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Volume de donnees propre : jamais le dossier « data/ » du depot."""
    monkeypatch.setattr(deps, "DATA_DIR", tmp_path)
    for accesseur in (deps.get_store, deps.get_memory, deps.get_journal):
        accesseur.cache_clear()
    yield tmp_path
    for accesseur in (deps.get_store, deps.get_memory, deps.get_journal):
        accesseur.cache_clear()


def connecte(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def test_un_fichier_illisible_n_est_pas_remplace_par_les_defauts(volume: Path) -> None:
    fichier = volume / "preferences.json"
    fichier.write_text(VIRGULE_EN_TROP, encoding="utf-8")

    with TestClient(app) as client:
        connecte(client)
        # Le demarrage n'a rien ecrit par-dessus.
        assert fichier.read_text(encoding="utf-8") == VIRGULE_EN_TROP

        # La cle existe malgre tout, en memoire : l'integration reste possible le
        # temps de reparer, et la lire n'ecrit rien non plus.
        cle = client.get("/api/settings/api-key").json()["api_key"]
        assert len(cle) >= apikey.MIN_LENGTH
        assert fichier.read_text(encoding="utf-8") == VIRGULE_EN_TROP
        assert not list(volume.glob("preferences.json.illisible-*"))

        # Premier enregistrement : l'original est mis de cote AVANT d'etre remplace.
        reponse = client.put("/api/settings/preferences", json={"scan": {"min_size_mb": 42}})
        assert reponse.status_code == 200

    copies = list(volume.glob("preferences.json.illisible-*"))
    assert len(copies) == 1
    assert re.fullmatch(r"preferences\.json\.illisible-\d{8}-\d{6}", copies[0].name)
    assert copies[0].read_text(encoding="utf-8") == VIRGULE_EN_TROP

    relu = json.loads(fichier.read_text(encoding="utf-8"))
    assert relu["scan"]["min_size_mb"] == 42
    assert relu["integration"]["api_key"] == cle


def test_un_fichier_valide_que_la_validation_refuse_ne_bloque_pas_le_demarrage(
    volume: Path,
) -> None:
    """Une source demontee depuis : le fichier se lit, mais ne passe plus la
    validation. Le demarrage levait une PreferenceError, conteneur en boucle."""
    fichier = volume / "preferences.json"
    fichier.write_text(
        json.dumps(
            {
                "custom_sources": ["/ailleurs/disque-demonte"],
                "destinations": {"movie": "Cinema"},
            }
        ),
        encoding="utf-8",
    )
    magasin = deps.get_store()
    with pytest.raises(PreferenceError):
        magasin.validate(magasin.load())

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200

    relu = json.loads(fichier.read_text(encoding="utf-8"))
    # La cle est ecrite, et rien d'autre n'a bouge.
    assert len(relu["integration"]["api_key"]) >= apikey.MIN_LENGTH
    assert relu["custom_sources"] == ["/ailleurs/disque-demonte"]
    assert relu["destinations"]["movie"] == "Cinema"


def test_un_bloc_de_la_mauvaise_nature_ne_fait_pas_tomber_le_demarrage(volume: Path) -> None:
    """« "subtitles": "oops" » levait une AttributeError a la relecture, donc au
    demarrage ; une valeur mal typee faisait ensuite refuser tout enregistrement."""
    (volume / "preferences.json").write_text(
        json.dumps(
            {
                "destinations": {"movie": "Cinema"},
                "subtitles": "oops",
                "notifications": {"enabled": "false"},
                "vpn": {"policy": 3},
            }
        ),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        connecte(client)
        corps = client.get("/api/settings/preferences").json()
        assert corps["destinations"]["movie"] == "Cinema"
        assert corps["subtitles"]["languages"] == SubtitleSettings().languages
        assert corps["notifications"]["enabled"] is False
        assert corps["vpn"]["policy"] == "free"
        reponse = client.put("/api/settings/preferences", json={"scan": {"min_size_mb": 10}})
        assert reponse.status_code == 200


def test_bloc_ecarte_une_langue_en_texte_seul_et_un_bloc_qui_n_est_pas_un_objet() -> None:
    relu = _bloc(SubtitleSettings, {"languages": "fr", "overwrite": True})
    assert relu.languages == SubtitleSettings().languages
    # Le reste du bloc, bien type, est garde.
    assert relu.overwrite is True
    assert _bloc(SubtitleSettings, "oops") == SubtitleSettings()


def test_un_refus_du_magasin_au_demarrage_est_journalise_et_le_demarrage_continue(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Rien dans la cle d'API ne justifie un conteneur qui redemarre en boucle :
    la session reste une porte ouverte pour corriger."""

    def _refus(self: PreferenceStore) -> str:
        raise PreferenceError("refus simulé")

    monkeypatch.setattr(PreferenceStore, "ensure_api_key", _refus)
    with caplog.at_level(logging.ERROR, logger="sortilege.main"), TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        connecte(client)
    assert "refus simulé" in caplog.text
