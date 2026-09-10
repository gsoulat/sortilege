"""Lecteur de livres : servir un EPUB chapitre par chapitre, ou un PDF entier.

Un livre range n'est pas un livre lu. Sans lecteur, verifier qu'un fichier est
bien ce qu'il pretend etre demanderait de le telecharger et d'ouvrir une autre
application — pour une question a laquelle trois lignes de texte repondent.

Le livre est designe par son chemin RELATIF a la racine de bibliotheque, resolu
et confine cote serveur. Un chemin absolu venu du navigateur serait une porte
ouverte sur tout le systeme de fichiers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..config import get_settings
from ..core import epubreader
from ..core.ebook import BOOK_EXTENSIONS
from ..core.ebook import read as read_book

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/books", tags=["livres"])

# Le contenu d'un EPUB est du code etranger : le format autorise le JavaScript,
# et un livre telecharge n'est pas plus digne de confiance qu'une page web
# quelconque. Cet en-tete interdit TOUT — script, cadre, requete sortante — en
# plus du nettoyage deja fait a la lecture. Deux protections independantes,
# parce que le nettoyage est du filtrage, donc faillible par nature.
_CSP_CHAPITRE = (
    "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self'; sandbox"
)


def _resolve(relative: str) -> Path:
    """Chemin absolu d'un livre, confine sous la racine de bibliotheque."""
    if relative.startswith("/") or any(
        part in ("..", ".") for part in relative.replace("\\", "/").split("/")
    ):
        raise HTTPException(400, "chemin refuse")

    racine = get_settings().library_root
    chemin = (racine / relative).resolve(strict=False)
    if racine.resolve() not in chemin.parents:
        raise HTTPException(400, "chemin hors de la bibliotheque")
    if chemin.suffix.lower() not in BOOK_EXTENSIONS:
        raise HTTPException(415, "ce fichier n'est pas un livre")
    if not chemin.is_file():
        raise HTTPException(404, "livre introuvable")
    return chemin


@router.get("/info")
def info(path: str) -> dict[str, object]:
    """Ce qu'on sait du livre, et son fil de lecture.

    Repondre AVANT d'afficher le lecteur evite d'ouvrir une fenetre vide sur un
    format qu'on ne sait pas ouvrir — un MOBI, un CBZ — et de laisser
    l'utilisateur devant un ecran blanc sans explication.
    """
    chemin = _resolve(path)
    meta = read_book(chemin)
    format_ = chemin.suffix.lower().lstrip(".")

    chapitres = epubreader.spine(chemin) if format_ == "epub" else []
    return {
        "path": path,
        "format": format_,
        "title": meta.title or chemin.stem,
        "author": meta.author,
        "series": meta.series,
        "volume": meta.volume,
        "publisher": meta.publisher,
        "year": meta.year,
        "isbn": meta.isbn,
        "size_bytes": chemin.stat().st_size,
        # Lisible DANS l'application. Le PDF s'affiche nativement, l'EPUB par
        # chapitres ; les autres formats se telechargent, faute de savoir les
        # ouvrir sans imposer une dependance lourde.
        "readable": format_ in ("epub", "pdf"),
        "chapters": [{"index": c.index, "title": c.title} for c in chapitres],
    }


@router.get("/chapter", response_class=HTMLResponse)
def chapter(path: str, index: int = 0) -> HTMLResponse:
    """Un chapitre d'EPUB, nettoye et mis en page pour la lecture."""
    chemin = _resolve(path)
    if chemin.suffix.lower() != ".epub":
        raise HTTPException(415, "seul l'EPUB se lit par chapitres")

    from urllib.parse import quote

    base = f"/api/books/asset?path={quote(path)}&interne="
    corps = epubreader.chapter_html(chemin, index, base)
    if not corps:
        raise HTTPException(404, "chapitre introuvable")

    return HTMLResponse(
        _PAGE.format(corps=corps),
        headers={"Content-Security-Policy": _CSP_CHAPITRE, "Cache-Control": "no-store"},
    )


@router.get("/asset")
def asset(path: str, interne: str) -> Response:
    """Une image ou une feuille de style contenue dans le livre."""
    chemin = _resolve(path)
    trouve = epubreader.asset(chemin, interne)
    if trouve is None:
        raise HTTPException(404, "ressource introuvable")
    contenu, type_mime = trouve
    return Response(
        contenu,
        media_type=type_mime,
        headers={"Cache-Control": "no-store", "Content-Security-Policy": "sandbox"},
    )


@router.get("/file")
def file(path: str) -> FileResponse:
    """Le fichier lui-meme. Sert au lecteur PDF du navigateur."""
    chemin = _resolve(path)
    return FileResponse(
        chemin,
        media_type="application/pdf" if chemin.suffix.lower() == ".pdf" else None,
        headers={"Cache-Control": "no-store"},
    )


# Mise en page du chapitre. Volontairement sobre et LARGEUR LIMITEE : une ligne
# de texte qui traverse un ecran large devient penible a suivre, l'oeil perdant
# le debut de la ligne suivante. Les typographes s'accordent sur 60 a 80
# caracteres ; 34em y correspond a la taille choisie.
_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<style>
  :root {{ color-scheme: dark; }}
  body {{
    margin: 0 auto; padding: 28px 22px 60px; max-width: 34em;
    background: #14141b; color: #d8d8e0;
    font-family: Georgia, 'Iowan Old Style', serif;
    font-size: 17px; line-height: 1.75; text-align: justify;
    hyphens: auto; -webkit-hyphens: auto;
  }}
  h1, h2, h3 {{ font-family: system-ui, sans-serif; line-height: 1.3; text-align: left; }}
  img, svg {{ max-width: 100%; height: auto; }}
  a {{ color: #8b8bf0; }}
  p {{ margin: 0 0 1em; }}
</style></head><body>{corps}</body></html>"""
