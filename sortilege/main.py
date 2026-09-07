"""Point d'entree : API + interface, un seul processus, un seul port.

L'interface Vue compilee est servie en statique par FastAPI. C'est le modele de
Radarr : l'utilisateur voit une application, pas deux services. Consequence
directe : aucune configuration CORS, aucun second port a publier.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import get_settings
from .core.template import PRESETS, TOKENS, TemplateError, render, validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Refuse de demarrer sur une configuration incomplete.

    Une application qui range des fichiers ne doit jamais tourner en mode
    degrade silencieux : mieux vaut un conteneur qui redemarre en boucle avec
    un message clair qu'une instance sans mot de passe exposee sur le LAN.
    """
    settings = get_settings()
    problems = settings.check_runtime()
    if problems:
        for p in problems:
            logger.error("configuration invalide : %s", p)
        raise RuntimeError("configuration invalide — voir les erreurs ci-dessus")

    if settings.dry_run:
        logger.info("MODE SIMULATION actif : aucun fichier ne sera deplace")

    yield


app = FastAPI(
    title="Sortilège",
    version="0.1.0",
    docs_url=None,  # pas de surface d'API publique inutile
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/api/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "dry_run": settings.dry_run,
        "ai_enabled": settings.ai_enabled,
    }


@app.get("/api/tokens")
def list_tokens() -> dict[str, object]:
    """Catalogue des jetons et gabarits predefinis.

    L'UI construit sa palette de glisser-deposer depuis cette reponse : la
    liste n'existe qu'a un seul endroit, dans core/template.py.
    """
    return {
        "tokens": [
            {"name": t.name, "label": t.label, "example": t.example, "group": t.group}
            for t in TOKENS
        ],
        "presets": PRESETS,
    }


# Exemples utilises pour l'apercu live du constructeur de gabarit. Choisis pour
# exposer les cas qui cassent : titre sans annee, episode sans titre, anime en
# numerotation absolue.
PREVIEW_SAMPLES: dict[str, list[dict[str, object]]] = {
    "movie": [
        {
            "title": "Dune",
            "year": 2024,
            "resolution": "1080p",
            "source": "bluray",
            "codec": "x265",
            "language": "multi",
            "tmdb_id": 693134,
            "imdb_id": "tt15239678",
        },
        {
            "title": "Le Fabuleux Destin d'Amélie Poulain",
            "year": 2001,
            "resolution": "1080p",
            "language": "vff",
            "tmdb_id": 194,
        },
        {"title": "Sans Titre Connu", "resolution": "720p"},
    ],
    "episode": [
        {
            "title": "Severance",
            "year": 2022,
            "season": 2,
            "episode": 7,
            "episode_title": "Chikhai Bardo",
            "resolution": "1080p",
            "language": "multi",
        },
        {
            "title": "Kaamelott",
            "year": 2005,
            "season": 1,
            "episode": 12,
            "resolution": "576p",
            "language": "vff",
        },
    ],
    "anime": [
        {
            "title": "Sousou no Frieren",
            "year": 2023,
            "episode": 12,
            "absolute_episode": 12,
            "episode_title": "Le Village des Épées",
            "resolution": "1080p",
            "language": "vostfr",
        },
        {"title": "One Piece", "absolute_episode": 1088, "resolution": "1080p"},
    ],
}


class PreviewRequest(BaseModel):
    template: str
    kind: str = "movie"


@app.post("/api/templates/preview")
def preview_template(req: PreviewRequest) -> dict[str, object]:
    """Rend un gabarit sur des exemples.

    L'UI appelle cet endpoint plutot que de reimplementer le rendu en
    JavaScript : l'apercu montre exactement ce que produira l'application, pas
    une approximation qui divergerait au premier changement du moteur.
    """
    try:
        validate(req.template)
    except TemplateError as exc:
        return {"valid": False, "error": str(exc), "results": []}

    samples = PREVIEW_SAMPLES.get(req.kind, PREVIEW_SAMPLES["movie"])
    return {
        "valid": True,
        "error": None,
        "results": [{"values": s, "path": render(req.template, s)} for s in samples],
    }


# L'UI compilee n'existe qu'apres le build Docker ; en developpement local elle
# est servie par Vite sur son propre port.
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str) -> FileResponse:
        """Toutes les routes non-API renvoient l'index : routage cote client."""
        return FileResponse(STATIC_DIR / "index.html")
else:
    logger.warning("interface non compilee (%s absent) — API seule", STATIC_DIR)
