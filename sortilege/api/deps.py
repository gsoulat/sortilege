"""Dependances partagees entre routeurs."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from ..core.preferences import PreferenceStore


@lru_cache
def get_store() -> PreferenceStore:
    """Magasin de preferences, instance unique.

    Le chemin est derive de la base de donnees : les deux vivent dans le meme
    volume monte, celui qui doit survivre a une reconstruction de l'image.
    """
    conf = get_settings()
    data_dir = Path("data")
    return PreferenceStore(
        path=data_dir / "preferences.json",
        library_root=conf.library_root,
        source_roots=list(conf.source_roots),
    )
