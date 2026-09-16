"""Dependances partagees entre routeurs."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..config import get_settings
from ..core.journal import Journal
from ..core.preferences import (
    EffectiveThresholds,
    Preferences,
    PreferenceStore,
    effective_thresholds,
)
from ..core.scanner import ScanRules
from ..core.scoring import Policy
from ..core.store import Store

DATA_DIR = Path("data")


@lru_cache
def get_journal() -> Journal:
    """Journal d'annulation, instance unique.

    Il vit dans le volume monte : c'est le seul etat vraiment precieux de
    l'application, le seul qui doive survivre a une reconstruction de l'image.
    """
    return Journal(DATA_DIR / "journal.jsonl")


@lru_cache
def get_memory() -> Store:
    """Base des decisions et de l'etat de travail.

    Dans le meme volume que le journal : c'est l'etat qui doit survivre a une
    reconstruction de l'image.
    """
    return Store(DATA_DIR / "sortilege.db")


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
        extra_roots=list(conf.allowed_roots),
    )


def tmdb_key() -> str:
    """Cle TheMovieDB effective : les preferences d'abord, l'environnement en repli.

    Cet ordre est le seul qui rende le champ de l'interface credible. L'inverse
    — l'environnement gagnant — donnerait une saisie acceptee, enregistree, et
    sans effet sur une installation qui a un .env : le pire des trois etats.

    Le repli, lui, protege l'existant : une installation qui porte sa cle dans
    le .env depuis le premier jour continue d'identifier sans que personne
    n'ait a rouvrir les reglages.
    """
    return get_store().load().metadata.tmdb_api_key.strip() or get_settings().tmdb_api_key.strip()


def decision_thresholds(prefs: Preferences | None = None) -> EffectiveThresholds:
    """Seuils de decision EFFECTIFS : le reglage d'abord, l'environnement en repli.

    Seul endroit ou les deux sources se rencontrent. Tout ce qui decide d'un
    plan passe par ici, via ``decision_policy`` : un calcul qui lirait encore
    ``get_settings()`` directement ignorerait l'ecran sans que rien ne le dise.

    ``prefs`` evite une relecture quand l'appelant les a deja — et laisse le
    cycle automatique, dont le magasin se remplace en test, fournir les siennes.
    Ne leve jamais : un melange incoherent retombe sur l'environnement, et le
    motif voyage dans ``ignored_reason``.
    """
    conf = get_settings()
    reglages = prefs if prefs is not None else get_store().load()
    return effective_thresholds(
        reglages.identification, conf.auto_apply_threshold, conf.reject_threshold
    )


def decision_policy(prefs: Preferences | None = None) -> Policy:
    """La politique de decision a passer au pipeline. Voir ``decision_thresholds``."""
    seuils = decision_thresholds(prefs)
    return Policy(auto_apply_threshold=seuils.auto_apply, reject_threshold=seuils.reject)


def tmdb_language() -> str:
    """Langue demandee au fournisseur de metadonnees."""
    return get_store().load().metadata.language


def scan_rules() -> ScanRules:
    """Regles de parcours effectives : le socle livre, complete par l'utilisateur.

    Construites ici, a la frontiere HTTP, parce que c'est le seul endroit qui
    connait a la fois les preferences et le scanner — lequel doit pouvoir
    tourner sans configuration du tout.
    """
    reglages = get_store().load().scan
    return ScanRules.extended(
        min_size_bytes=reglages.min_size_bytes(),
        extra_dirs=reglages.extra_skip_dirs,
        extra_hints=reglages.extra_skip_hints,
    )
