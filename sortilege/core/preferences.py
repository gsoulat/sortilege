"""Preferences modifiables depuis l'interface.

Distinction importante avec ``config.py`` :

- ``config.py`` decrit le DEPLOIEMENT : points de montage, cles d'API, secrets.
  Il vient de l'environnement et n'est pas modifiable a chaud — ces valeurs
  doivent correspondre aux volumes du conteneur, les changer depuis l'UI
  produirait une configuration qui ne survit pas a un redemarrage.
- ce module decrit l'USAGE : quelles sources scanner, ou ranger chaque type,
  avec quel gabarit. Ce sont des choix qui appartiennent a l'utilisateur et qui
  changent souvent.

Garde-fou : une destination est toujours resolue SOUS la racine de
bibliotheque declaree dans l'environnement. Sans cela, l'interface deviendrait
un moyen d'ecrire n'importe ou sur le NAS.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

from .safety import PathConfinementError, resolve_within
from .template import PRESETS, TemplateError, validate

logger = logging.getLogger(__name__)

KINDS = ("movie", "episode", "anime")

DEFAULT_DESTINATIONS: dict[str, str] = {
    "movie": "Films",
    "episode": "Series",
    "anime": "Animes",
}


class PreferenceError(ValueError):
    """Preference refusee — message destine a l'utilisateur."""


@dataclass
class Preferences:
    """Ce que l'utilisateur choisit, par opposition a ce que l'admin deploie."""

    enabled_sources: list[str] = field(default_factory=list)
    """Sous-ensemble des racines sources a scanner. Vide = toutes.

    Utile quand une racine est un montage reseau lent qu'on ne veut pas
    reparcourir a chaque fois."""

    destinations: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DESTINATIONS))
    """Sous-chemin de destination par type, RELATIF a la racine de
    bibliotheque. Permet de separer films et series sur des arborescences
    differentes."""

    templates: dict[str, str] = field(default_factory=dict)
    """Gabarit par type. Vide = celui du prereglage Jellyfin."""

    def template_for(self, kind: str) -> str:
        return self.templates.get(kind) or PRESETS["jellyfin"].get(kind, "")

    def destination_for(self, kind: str) -> str:
        return self.destinations.get(kind) or DEFAULT_DESTINATIONS.get(kind, "")


class PreferenceStore:
    """Persistance JSON.

    Un fichier plutot qu'une table : ces preferences forment un seul document,
    elles se lisent a chaque requete et s'ecrivent rarement. Une table
    n'apporterait rien et il faudrait la migrer.
    """

    def __init__(self, path: Path, library_root: Path, source_roots: list[Path]) -> None:
        self._path = path
        self._library_root = library_root
        self._source_roots = source_roots
        self._lock = Lock()
        self._cache: Preferences | None = None

    def load(self) -> Preferences:
        with self._lock:
            if self._cache is not None:
                return self._cache

            if not self._path.is_file():
                self._cache = Preferences()
                return self._cache

            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # Un fichier corrompu ne doit pas empecher l'application de
                # demarrer : on repart des defauts en le signalant.
                logger.warning("preferences illisibles (%s), retour aux defauts", exc)
                self._cache = Preferences()
                return self._cache

            self._cache = Preferences(
                enabled_sources=list(raw.get("enabled_sources") or []),
                destinations={**DEFAULT_DESTINATIONS, **(raw.get("destinations") or {})},
                templates=dict(raw.get("templates") or {}),
            )
            return self._cache

    def save(self, prefs: Preferences) -> Preferences:
        self.validate(prefs)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(asdict(prefs), indent=2, ensure_ascii=False), encoding="utf-8"
            )
            self._cache = prefs
        return prefs

    def validate(self, prefs: Preferences) -> None:
        """Refuse une preference dangereuse ou inapplicable."""
        known = {str(p) for p in self._source_roots}
        for source in prefs.enabled_sources:
            if source not in known:
                raise PreferenceError(
                    f"« {source} » n'est pas une racine source declaree. "
                    "Les racines viennent de SORTILEGE_SOURCE_ROOTS."
                )

        for kind, sub in prefs.destinations.items():
            if kind not in KINDS:
                raise PreferenceError(f"type de media inconnu : {kind}")
            if not sub.strip():
                raise PreferenceError(f"destination vide pour « {kind} »")

            # ``resolve_within`` NEUTRALISE une remontee au lieu de la refuser,
            # ce qui est le bon comportement pour un titre venu d'une API mais
            # pas pour une saisie humaine : l'utilisateur qui tape « ../../etc »
            # doit lire un refus, pas decouvrir plus tard que ses fichiers sont
            # partis dans « media/etc ». On refuse donc explicitement en amont.
            if sub.startswith("/"):
                raise PreferenceError(
                    f"« {kind} » : la destination doit etre relative a la racine de "
                    "bibliotheque, sans « / » initial."
                )
            if any(part in ("..", ".") for part in sub.replace("\\", "/").split("/")):
                raise PreferenceError(
                    f"« {kind} » : « .. » et « . » sont interdits dans une destination."
                )

            try:
                resolve_within(self._library_root, sub)
            except PathConfinementError as exc:
                raise PreferenceError(f"destination refusee pour « {kind} » : {exc}") from exc

        for kind, tpl in prefs.templates.items():
            if kind not in KINDS:
                raise PreferenceError(f"type de media inconnu : {kind}")
            if not tpl.strip():
                continue
            try:
                validate(tpl)
            except TemplateError as exc:
                raise PreferenceError(f"gabarit invalide pour « {kind} » : {exc}") from exc

    def resolved_sources(self) -> list[Path]:
        """Racines effectivement a scanner."""
        prefs = self.load()
        if not prefs.enabled_sources:
            return list(self._source_roots)
        selected = set(prefs.enabled_sources)
        return [p for p in self._source_roots if str(p) in selected]

    def destination_root(self, kind: str) -> Path:
        """Chemin absolu ou ranger ce type de media."""
        prefs = self.load()
        return resolve_within(self._library_root, prefs.destination_for(kind))

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None
