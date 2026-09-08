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
class AISettings:
    """Choix du fournisseur IA, modifiable depuis l'interface.

    La cle vit ici et non dans l'environnement : changer de fournisseur ne doit
    pas imposer de modifier la stack et de redemarrer. Elle n'est JAMAIS
    renvoyee au navigateur — l'API n'expose qu'un booleen « configuree ».
    """

    enabled: bool = False
    provider: str = "anthropic"
    model: str = ""
    """Vide = le modele par defaut du fournisseur."""

    base_url: str = ""
    """Vide = l'URL connue du fournisseur. A renseigner pour « custom »."""

    api_key: str = ""
    threshold: float = 0.80
    """En dessous de ce score, le resolveur est sollicite. Au-dessus, le
    resultat deterministe est deja bon : payer un appel n'apporterait rien."""

    batch_size: int = 12


@dataclass
class AutomationSettings:
    """Traitement automatique de bout en bout."""

    enabled: bool = False
    interval_minutes: int = 15
    quiet_seconds: int = 120
    """Un fichier doit n'avoir plus bouge depuis ce delai. Attendre trop coute
    un cycle ; traiter trop tot coute un fichier incomplet deplace."""

    apply_auto: bool = False
    """Deplacer reellement, ou seulement remplir la file. Desactive par
    defaut : ranger sans personne devant est un engagement plus lourd
    qu'identifier."""


@dataclass
class Preferences:
    """Ce que l'utilisateur choisit, par opposition a ce que l'admin deploie."""

    custom_sources: list[str] = field(default_factory=list)
    """Sources ajoutees depuis l'interface, en plus des racines montees.

    Chacune doit se trouver SOUS une racine declaree dans l'environnement : le
    conteneur ne voit que ses volumes, et laisser saisir un chemin libre
    permettrait de parcourir tout ce qui est monte."""

    enabled_sources: list[str] = field(default_factory=list)
    """Sous-ensemble des sources a scanner. Vide = toutes.

    Utile quand une source est un montage reseau lent qu'on ne veut pas
    reparcourir a chaque fois."""

    destinations: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DESTINATIONS))
    """Sous-chemin de destination par type, RELATIF a la racine de
    bibliotheque. Permet de separer films et series sur des arborescences
    differentes."""

    templates: dict[str, str] = field(default_factory=dict)
    """Gabarit par type. Vide = celui du prereglage Jellyfin."""

    ai: AISettings = field(default_factory=AISettings)
    automation: AutomationSettings = field(default_factory=AutomationSettings)

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

    def __init__(
        self,
        path: Path,
        library_root: Path,
        source_roots: list[Path],
        extra_roots: list[Path] | None = None,
    ) -> None:
        self._path = path
        self._library_root = library_root
        self._source_roots = source_roots
        self._extra_roots = extra_roots or []
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
                custom_sources=list(raw.get("custom_sources") or []),
                enabled_sources=list(raw.get("enabled_sources") or []),
                destinations={**DEFAULT_DESTINATIONS, **(raw.get("destinations") or {})},
                templates=dict(raw.get("templates") or {}),
                ai=AISettings(**{**asdict(AISettings()), **(raw.get("ai") or {})}),
                automation=AutomationSettings(
                    **{**asdict(AutomationSettings()), **(raw.get("automation") or {})}
                ),
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

    def all_sources(self, prefs: Preferences | None = None) -> list[Path]:
        """Racines montees + sources ajoutees, dedoublonnees et ordonnees."""
        prefs = prefs or self.load()
        seen: dict[str, Path] = {str(p): p for p in self._source_roots}
        for raw in prefs.custom_sources:
            path = Path(raw)
            seen.setdefault(str(path), path)
        return list(seen.values())

    @property
    def allowed_areas(self) -> list[Path]:
        """Zones que l'interface peut parcourir et proposer comme source.

        Les racines sources ET la racine de bibliotheque. Cette derniere n'est
        pas un oubli : scanner une bibliotheque deja rangee est un usage a part
        entiere — la normaliser, corriger d'anciens noms, rattraper ce qui a ete
        classe a la main. C'est le cas « rattrapage » pour lequel on utilise
        FileBot d'habitude.
        """
        areas: dict[str, Path] = {str(p): p for p in self._source_roots}
        areas.setdefault(str(self._library_root), self._library_root)
        for extra in self._extra_roots:
            areas.setdefault(str(extra), extra)
        return list(areas.values())

    def _assert_under_a_root(self, raw: str) -> Path:
        """Une source doit vivre sous une zone autorisee.

        Le conteneur ne voit que ses volumes ; accepter un chemin libre
        reviendrait a offrir un parcours de tout ce qui est monte, y compris
        ce qui n'a rien a voir avec des medias.
        """
        if not raw.strip():
            raise PreferenceError("chemin de source vide")
        if any(part in ("..", ".") for part in raw.replace("\\", "/").split("/")):
            raise PreferenceError(f"« {raw} » : « .. » et « . » sont interdits.")

        candidate = Path(raw)
        if not candidate.is_absolute():
            raise PreferenceError(f"« {raw} » doit etre un chemin absolu.")

        resolved = candidate.resolve(strict=False)
        for root in self.allowed_areas:
            root = root.resolve()
            if resolved == root or root in resolved.parents:
                return resolved

        allowed = ", ".join(str(p) for p in self.allowed_areas) or "(aucune)"
        raise PreferenceError(
            f"« {raw} » est hors des zones montees. Zones autorisees : {allowed}. "
            "Pour en ouvrir une autre, declare-la dans SORTILEGE_ALLOWED_ROOTS "
            "(le volume doit deja etre monte dans le conteneur)."
        )

    def validate(self, prefs: Preferences) -> None:
        """Refuse une preference dangereuse ou inapplicable."""
        for raw in prefs.custom_sources:
            self._assert_under_a_root(raw)

        known = {str(p) for p in self.all_sources(prefs)}
        for source in prefs.enabled_sources:
            if source not in known:
                raise PreferenceError(
                    f"« {source} » n'est ni une racine montee ni une source ajoutee."
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

        from .ai import BY_KEY

        if prefs.ai.provider not in BY_KEY:
            raise PreferenceError(f"fournisseur IA inconnu : {prefs.ai.provider}")
        if prefs.automation.interval_minutes < 1:
            raise PreferenceError("l'intervalle doit valoir au moins une minute")
        if prefs.automation.quiet_seconds < 0:
            raise PreferenceError("le delai de stabilite ne peut pas etre negatif")

        if not 0.0 <= prefs.ai.threshold <= 1.0:
            raise PreferenceError("le seuil IA doit etre compris entre 0 et 1")
        if prefs.ai.base_url and not prefs.ai.base_url.startswith(("http://", "https://")):
            raise PreferenceError("l'URL du service IA doit commencer par http:// ou https://")
        if prefs.ai.provider == "custom" and prefs.ai.enabled and not prefs.ai.base_url:
            raise PreferenceError(
                "un fournisseur « Autre » demande une URL de base : sans elle, "
                "on ne sait pas qui appeler."
            )

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
        """Sources effectivement a scanner."""
        prefs = self.load()
        available = self.all_sources(prefs)
        if not prefs.enabled_sources:
            return available
        selected = set(prefs.enabled_sources)
        return [p for p in available if str(p) in selected]

    def browse(self, raw: str | None) -> dict[str, object]:
        """Sous-dossiers d'un chemin, pour l'explorateur de l'interface.

        Sans argument, renvoie les racines montees. Toujours confine : on ne
        peut descendre que sous une racine declaree, et jamais remonter
        au-dessus.
        """
        if not raw:
            return {
                "path": None,
                "parent": None,
                "entries": [
                    {
                        "path": str(p),
                        "name": p.name or str(p),
                        "exists": p.is_dir(),
                        "is_root": True,
                        # Signale une zone qui est aussi une destination : y
                        # scanner sert a normaliser l'existant, pas a importer.
                        "is_library": p == self._library_root,
                    }
                    for p in self.allowed_areas
                ],
            }

        current = self._assert_under_a_root(raw)
        if not current.is_dir():
            raise PreferenceError(f"« {raw} » n'est pas un dossier accessible.")

        entries = []
        try:
            for child in sorted(current.iterdir()):
                if not child.is_dir() or child.name.startswith("."):
                    continue
                entries.append(
                    {
                        "path": str(child),
                        "name": child.name,
                        "exists": True,
                        "is_root": False,
                        "is_library": False,
                    }
                )
        except OSError as exc:
            raise PreferenceError(f"lecture impossible : {exc}") from exc

        # Le parent n'est propose que s'il reste dans le perimetre autorise.
        parent: str | None = None
        try:
            parent = str(self._assert_under_a_root(str(current.parent)))
        except PreferenceError:
            parent = None

        return {"path": str(current), "parent": parent, "entries": entries}

    def destination_root(self, kind: str) -> Path:
        """Chemin absolu ou ranger ce type de media."""
        prefs = self.load()
        return resolve_within(self._library_root, prefs.destination_for(kind))

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None
