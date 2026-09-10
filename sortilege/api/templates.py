"""Catalogue de jetons, prereglages et apercu de gabarit."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ..core.template import PRESETS, TOKENS, TemplateError, render, validate

router = APIRouter(prefix="/api", tags=["gabarits"])

# Exemples de l'apercu live. Choisis pour exposer les cas qui cassent : titre
# sans annee, episode sans titre, anime en numerotation absolue.
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
            "title": "Hunger Games : L'Embrasement",
            "year": 2013,
            "collection": "Hunger Games - Saga",
            "resolution": "1080p",
            "language": "vff",
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
            "title": "Star Trek: Discovery",
            "year": 2017,
            "collection": "Star Trek",
            "season": 3,
            "episode": 4,
            "episode_title": "Forget Me Not",
            "resolution": "1080p",
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
    "book": [
        {
            "title": "Le Nom du Vent",
            "author": "Patrick Rothfuss",
            "series": "Chronique du Tueur de Roi",
            "volume": 1,
            "year": 2007,
            "publisher": "Bragelonne",
            "isbn": "9782266021196",
        },
        # Un roman isole : le segment de serie disparait de lui-meme, ce qui
        # permet au meme gabarit de servir aux deux cas.
        {"title": "L'Étranger", "author": "Albert Camus", "year": 1942},
        # Sans auteur : le cas d'un fichier dont on n'a lu que le titre.
        {"title": "Manuscrit Sans Nom"},
    ],
}


class PreviewRequest(BaseModel):
    template: str
    kind: str = "movie"


@router.get("/tokens")
def list_tokens() -> dict[str, object]:
    """Catalogue des jetons et gabarits predefinis.

    L'UI construit sa palette depuis cette reponse : la liste n'existe qu'a un
    seul endroit, dans core/template.py.
    """
    return {
        "tokens": [
            {"name": t.name, "label": t.label, "example": t.example, "group": t.group}
            for t in TOKENS
        ],
        "presets": PRESETS,
    }


@router.post("/templates/preview")
def preview_template(req: PreviewRequest) -> dict[str, object]:
    """Rend un gabarit sur des exemples.

    L'UI appelle cet endpoint plutot que de reimplementer le rendu en
    JavaScript : l'apercu montre exactement ce que produira l'application.
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
