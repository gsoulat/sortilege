"""Dependances partagees entre routeurs."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from ..core.journal import Journal
from ..core.preferences import PreferenceStore

DATA_DIR = Path("data")


@lru_cache
def get_journal() -> Journal:
    """Journal d'annulation, instance unique.

    Il vit dans le volume monte : c'est le seul etat vraiment precieux de
    l'application, le seul qui doive survivre a une reconstruction de l'image.
    """
    return Journal(DATA_DIR / "journal.jsonl")


@lru_cache
def get_store() -> PreferenceStore:
    """Magasin de preferences, instance unique.

    Le chemin est derive de la base de donnees : les deux vivent dans le meme
    volume monte, celui qui doit survivre a une reconstruction de l'image.
    """
    conf = get_settings()
    return PreferenceStore(
        path=DATA_DIR / "preferences.json",
        library_root=conf.library_root,
        source_roots=list(conf.source_roots),
    )
