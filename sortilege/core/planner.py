"""Construit un plan : ce fichier ira la, sous ce nom, avec ce score.

Un plan est un OBJET, pas une action. C'est le choix d'architecture qui separe
Sortilege de Radarr : rien ne touche au disque tant qu'un plan n'a pas ete
applique, et un plan reste consultable, modifiable et refusable.

Ce module ne deplace rien. Il calcule une destination et un verdict.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .companions import find_companions, find_leftovers
from .matching import MatchResult
from .parser import MediaKind
from .safety import PathConfinementError, resolve_within
from .scanner import ScannedFile
from .scoring import Decision, Policy, compute_score, explain
from .template import TemplateError, render, validate

# Correspondance entre le type lu et la cle de gabarit / destination.
KIND_KEYS: dict[MediaKind, str] = {
    MediaKind.MOVIE: "movie",
    MediaKind.EPISODE: "episode",
    MediaKind.ANIME: "anime",
    MediaKind.UNKNOWN: "movie",
}


@dataclass(slots=True)
class Plan:
    """Une intention de rangement, pas encore executee."""

    id: str
    source: Path
    destination: Path | None
    kind: str
    score: float
    decision: Decision
    reasons: list[str] = field(default_factory=list)
    title: str = ""
    year: int | None = None
    provider: str = ""
    external_id: str = ""
    error: str | None = None

    companions: list[tuple[Path, Path]] = field(default_factory=list)
    """Fichiers a emporter avec la video, sous forme (source, destination).

    Sous-titres surtout : ne pas les deplacer est une perte de donnees
    silencieuse, personne ne s'en apercoit avant de lancer la lecture."""

    leftovers: list[Path] = field(default_factory=list)
    """Dechets de release evacuables vers la corbeille une fois la video
    partie. Jamais supprimes."""

    alternatives: list = field(default_factory=list)
    """Les autres candidats plausibles, gardes pour l'arbitrage humain.

    Ils etaient calcules puis jetes. Or c'est precisement quand le score est
    bas que l'utilisateur a besoin de VOIR les autres possibilites : entre
    « Dark Matter » 2015 et 2024, aucun signal automatique ne tranche, mais une
    affiche le fait instantanement."""

    manual: bool = False
    """Le candidat a ete choisi par un humain. Un choix explicite vaut mieux
    que n'importe quel score : on ne le repasse pas au calcul."""

    @property
    def is_noop(self) -> bool:
        """Le fichier est deja exactement la ou il devrait etre.

        Frequent quand on rescanne sa bibliotheque : appliquer ces plans ne
        changerait rien mais remplirait le journal d'annulation d'operations
        vides, le rendant inutilisable le jour ou l'on veut vraiment revenir en
        arriere."""
        return self.destination is not None and self.source == self.destination


def build_values(scanned: ScannedFile, match: MatchResult | None) -> dict[str, Any]:
    """Assemble les valeurs que le gabarit consommera.

    Ordre de preference constant : ce que dit le FOURNISSEUR d'abord, puis ce
    que dit le FICHIER, puis ce que dit le NOM. Un titre officiel vaut mieux
    qu'un tag de conteneur, qui vaut mieux qu'un nom de release.
    """
    parsed = scanned.parsed
    probe = scanned.probe
    candidate = match.candidate if match else None

    values: dict[str, Any] = {
        "title": (candidate.title if candidate else None) or probe.nfo_title or parsed.title,
        "original_title": (candidate.original_title if candidate else "") or parsed.title,
        "year": (candidate.year if candidate else None) or parsed.year or probe.nfo_year,
        "collection": (candidate.extra.get("collection") if candidate else None) or "",
        "season": parsed.season if parsed.season is not None else probe.container_season,
        "episode": parsed.episode if parsed.episode is not None else probe.container_episode,
        "absolute_episode": parsed.absolute_episode,
        "episode_title": (candidate.extra.get("episode_title") if candidate else None) or "",
        # La resolution MESUREE prime sur celle annoncee dans le nom : les
        # releases mentent regulierement.
        "resolution": probe.resolution_label or parsed.resolution,
        "source": parsed.source,
        "codec": probe.video_codec or parsed.codec,
        "language": parsed.language,
        "edition": "",
        "tmdb_id": probe.tmdb_id
        or (candidate.external_id if candidate and candidate.provider == "tmdb" else ""),
        "imdb_id": probe.imdb_id or "",
    }
    return values


def _alternatives(match: MatchResult | None, candidates: list) -> list:
    """Les autres candidats, sans celui retenu, tries par notoriete.

    Limite a huit : au-dela, une grille de jaquettes cesse d'aider a decider et
    redevient une liste a lire.
    """
    if not candidates:
        return []
    chosen = (match.candidate.provider, match.candidate.external_id) if match else (None, None)
    others = [c for c in candidates if (c.provider, c.external_id) != chosen]
    return sorted(others, key=lambda c: c.popularity, reverse=True)[:8]


def build_plan(
    scanned: ScannedFile,
    match: MatchResult | None,
    *,
    template: str,
    library_root: Path,
    policy: Policy,
    with_artwork: bool = True,
    with_cleanup: bool = True,
    all_candidates: list | None = None,
) -> Plan:
    """Calcule destination et verdict pour un fichier.

    Un echec (aucun candidat, gabarit invalide, destination hors racine) ne
    leve pas : il produit un plan REJECT porteur du motif. Une exception
    interromprait le traitement de toute la bibliotheque pour un fichier.
    """
    plan_id = uuid.uuid4().hex[:12]
    kind = KIND_KEYS.get(scanned.parsed.kind, "movie")
    all_candidates = all_candidates or []

    if match is None:
        return Plan(
            id=plan_id,
            source=scanned.path,
            destination=None,
            kind=kind,
            score=0.0,
            decision=Decision.REJECT,
            reasons=["aucun candidat plausible chez les fournisseurs"],
            title=scanned.parsed.title,
            year=scanned.parsed.year,
            error="non identifie",
            alternatives=_alternatives(None, all_candidates),
        )

    score = compute_score(match.signals)
    from .scoring import decide  # import local : evite un cycle a la lecture

    decision = decide(score, match.signals, policy)
    reasons = explain(match.signals, score, decision)

    try:
        validate(template)
        relative = render(template, build_values(scanned, match))
        destination = resolve_within(library_root, relative)
    except (TemplateError, PathConfinementError) as exc:
        return Plan(
            id=plan_id,
            source=scanned.path,
            destination=None,
            kind=kind,
            score=score,
            decision=Decision.REJECT,
            reasons=[*reasons, f"chemin impossible : {exc}"],
            title=match.candidate.title,
            year=match.candidate.year,
            provider=match.candidate.provider,
            external_id=match.candidate.external_id,
            error=str(exc),
        )

    # L'extension du fichier d'origine est conservee : le gabarit decrit un
    # chemin, pas un format de conteneur.
    destination = destination.with_suffix(scanned.path.suffix)

    companions = find_companions(scanned.path, artwork=with_artwork)
    leftovers = find_leftovers(scanned.path, companions) if with_cleanup else []

    return Plan(
        id=plan_id,
        source=scanned.path,
        destination=destination,
        kind=kind,
        score=score,
        decision=decision,
        reasons=reasons,
        title=match.candidate.title,
        year=match.candidate.year,
        provider=match.candidate.provider,
        external_id=match.candidate.external_id,
        companions=[(c.path, c.destination_for(destination)) for c in companions],
        leftovers=leftovers,
        alternatives=_alternatives(match, all_candidates),
    )
