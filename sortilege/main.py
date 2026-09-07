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

from .api import library, review, settings, templates
from .config import get_settings
from .core.probe import ffprobe_available

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
    conf = get_settings()
    problems = conf.check_runtime()
    if problems:
        for p in problems:
            logger.error("configuration invalide : %s", p)
        raise RuntimeError("configuration invalide — voir les erreurs ci-dessus")

    if conf.dry_run:
        logger.info("MODE SIMULATION actif : aucun fichier ne sera deplace")
    if not ffprobe_available():
        logger.warning("ffprobe absent : duree et tags des conteneurs non lisibles")

    yield


app = FastAPI(
    title="Sortilège",
    version="0.1.0",
    docs_url=None,  # pas de surface d'API publique inutile
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(templates.router)
app.include_router(library.router)
app.include_router(review.router)
app.include_router(settings.router)


@app.get("/api/health")
def health() -> dict[str, object]:
    conf = get_settings()
    return {
        "status": "ok",
        "dry_run": conf.dry_run,
        "ai_enabled": conf.ai_enabled,
        "ffprobe": ffprobe_available(),
    }


# L'UI compilee n'existe qu'apres le build ; en developpement local elle est
# servie par Vite sur son propre port.
if STATIC_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def serve_spa(full_path: str) -> FileResponse:
        """Toutes les routes non-API renvoient l'index : routage cote client."""
        return FileResponse(STATIC_DIR / "index.html")
else:
    logger.warning("interface non compilee (%s absent) — API seule", STATIC_DIR)
