"""Point d'entree : API + interface, un seul processus, un seul port.

L'interface Vue compilee est servie en statique par FastAPI. C'est le modele de
Radarr : l'utilisateur voit une application, pas deux services. Consequence
directe : aucune configuration CORS, aucun second port a publier.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import (
    auth,
    automation,
    books,
    collection,
    library,
    media,
    review,
    settings,
    templates,
    transcode,
    workspace,
)
from .api.deps import get_store
from .config import get_settings
from .core import transcode as reencodage
from .core.auth import SESSION_COOKIE, verify_session
from .core.probe import ffprobe_available

# Le niveau se regle sans reconstruire l'image : SORTILEGE_LOG_LEVEL=DEBUG fait
# apparaitre une ligne par fichier scanne — ce que le parseur a lu de chaque
# nom. C'est verbeux par construction, donc hors du defaut, mais c'est le seul
# moyen de comprendre pourquoi une oeuvre precise sort mal.
_NIVEAU = os.environ.get("SORTILEGE_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _NIVEAU, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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

    if not ffprobe_available():
        logger.warning("ffprobe absent : duree et tags des conteneurs non lisibles")

    # Reprise de l'etat de travail. Un scan de mille fichiers coute plusieurs
    # minutes de disque et les plans qui en decoulent des centaines d'appels
    # reseau : les refaire a chaque mise a jour d'image rendrait l'outil
    # penible. Un instantane illisible est ignore, pas fatal.
    if library.restore_scan():
        review.restore_plans()

    # Le menage promis a l'arret. Un ffmpeg interrompu laisse un fichier
    # partiel de plusieurs gigaoctets dans le dossier d'attente ; la file de
    # travaux, elle, ne survit pas au redemarrage — d'ou la liste vide : plus
    # aucun travail ne reclame ces fichiers, ils sont tous orphelins.
    #
    # Rien de ce qui suit ne doit empecher de demarrer : liberer de la place est
    # un confort, pas une condition de fonctionnement.
    try:
        efface = reencodage.purge_staging(conf.library_root, [])
        if efface:
            logger.info("demarrage : %s fichier(s) de reencodage orphelin(s) efface(s)", efface)
    except OSError as exc:
        logger.warning("menage du dossier de reencodage impossible : %s", exc)

    # La boucle tourne toujours ; elle consulte les preferences a chaque tour
    # et ne fait rien tant que l'automatisation est desactivee. La demarrer
    # conditionnellement obligerait a redemarrer le conteneur pour l'activer.
    automation.start()

    yield

    await automation.stop()
    # Le fil de reencodage laisse son ffmpeg finir : le resultat est ecrit a
    # part, donc au pire on laisse un fichier partiel que le menage ramassera au
    # demarrage suivant. Tuer l'encodage jetterait des heures de calcul.
    transcode.stop_worker()


app = FastAPI(
    title="Sortilège",
    # Une version en dur se perime des la premiere release. Elle vient donc
    # de __init__.py, seule source de verite, mise a jour avec le tag.
    version=__version__,
    docs_url=None,  # pas de surface d'API publique inutile
    redoc_url=None,
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(templates.router)
app.include_router(library.router)
app.include_router(review.router)
app.include_router(settings.router)
app.include_router(automation.router)
app.include_router(collection.router)
app.include_router(workspace.router)
app.include_router(media.router)
app.include_router(books.router)
app.include_router(transcode.router)

# Routes accessibles sans session. Liste blanche et non liste noire : oublier
# d'ajouter une exception rend une page inaccessible, ce qui se voit
# immediatement ; oublier de proteger une route ne se voit jamais.
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/logout", "/api/auth/me"})


@app.middleware("http")
async def require_session(request: Request, call_next):
    """Protege toute l'API derriere le mot de passe.

    L'interface elle-meme reste servie sans session : c'est elle qui affiche
    l'ecran de connexion. Seules les donnees et les actions sont fermees.
    """
    path = request.url.path
    if path.startswith("/api/") and path not in PUBLIC_PATHS:
        conf = get_settings()
        if not verify_session(request.cookies.get(SESSION_COOKIE), conf.secret_key):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentification requise."},
            )
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "version": __version__,
        # L'etat REEL du resolveur, celui que lit le pipeline. Ce badge
        # rapportait une variable d'environnement que plus rien ne consultait :
        # il annoncait « IA activee » sur une installation ou elle ne tournait
        # pas, et l'inverse des qu'on l'activait depuis l'interface.
        "ai_enabled": get_store().load().ai.enabled,
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
