"""La version est affichee : elle doit etre juste, et unique.

Deux endroits la declarent — pyproject.toml pour le paquet, __init__.py pour le
code. Tant qu'ils sont mis a jour a la main, ils divergeront un jour. Ce test
transforme cet oubli silencieux en echec bruyant.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from fastapi.testclient import TestClient

from sortilege import __version__
from sortilege.main import app

ROOT = Path(__file__).resolve().parent.parent


def test_les_deux_declarations_concordent() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_la_version_a_la_forme_attendue() -> None:
    """semantic-release et les tags Docker supposent du MAJEUR.MINEUR.CORRECTIF."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_la_version_est_exposee_sans_authentification() -> None:
    """L'entete l'affiche avant meme la connexion : /api/health doit la porter."""
    with TestClient(app) as client:
        body = client.get("/api/health").json()
    assert body["version"] == __version__


def test_l_application_declare_la_meme_version() -> None:
    assert app.version == __version__
