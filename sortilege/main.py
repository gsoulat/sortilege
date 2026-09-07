"""Point d'entree : API + interface, un seul processus, un seul port.

L'interface Vue compilee est servie en statique par FastAPI. C'est le modele de
Radarr : l'utilisateur voit une application, pas deux services. Consequence
directe : aucune configuration CORS, aucun second port a publier.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .core.template import PRESETS, TOKENS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web" / "static"

app = FastAPI(
    title="Sortilège",
    version="0.1.0",
    docs_url=None,   # pas de surface d'API publique inutile
    redoc_url=None,
)


@app.on_event("startup")
def _validate_configuration() -> None:
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
