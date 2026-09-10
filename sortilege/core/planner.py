"""Construit un plan : ce fichier ira la, sous ce nom, avec ce score.

Un plan est un OBJET, pas une action. C'est le choix d'architecture qui separe
Sortilege de Radarr : rien ne touche au disque tant qu'un plan n'a pas ete
applique, et un plan reste consultable, modifiable et refusable.

Ce module ne deplace rien. Il calcule une destination et un verdict.
"""

from __future__ import annotations

import hashlib
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
    MediaKind.BOOK: "book",
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
    poster_url: str = ""
    """Affiche de l'oeuvre retenue. Une vignette rend une liste de plans
    lisible d'un coup d'oeil : on reconnait une erreur d'identification bien
    plus vite sur une image que sur un titre."""

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

    identified_by: str = ""
    """D'ou vient l'identification : « tmdb », « ia », « memoire », « manuel »,
    « fichier », ou vide quand seul le nom a parle.

    Deux plans a 82 % ne se valent pas selon qu'ils viennent d'une fiche TMDB
    ou d'une supposition d'un modele de langage. Sans cette information, un
    score nu demande de faire confiance sans savoir a qui — et c'est
    exactement ce qu'on refuse de demander pour une operation qui deplace des
    fichiers."""

    @property
    def is_noop(self) -> bool:
        """Le fichier est deja exactement la ou il devrait etre.

        Frequent quand on rescanne sa bibliotheque : appliquer ces plans ne
        changerait rien mais remplirait le journal d'annulation d'operations
        vides, le rendant inutilisable le jour ou l'on veut vraiment revenir en
        arriere."""
        return self.destination is not None and self.source == self.destination


def plan_id_for(source: Path) -> str:
    """Identifiant DERIVE du fichier source, et non tire au hasard.

    Un fichier n'a qu'un plan a la fois. Avec un identifiant aleatoire, tout
    recalcul en produisait un second : la seconde passe IA, qui remplace un
    plan par un meilleur, creait ainsi un doublon des lors que le premier avait
    deja ete publie. Deriver l'identifiant de la source rend le remplacement
    naturel — le recalcul ecrase, il n'ajoute pas.

    Effet de bord bienvenu : une selection cochee dans l'interface survit a un
    recalcul, et un plan relu depuis le disque garde le meme identifiant.
    """
    return hashlib.blake2s(str(source).encode("utf-8"), digest_size=6).hexdigest()


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
        # La conversion depuis la numerotation absolue prime : elle vient du
        # fournisseur et vaut mieux que l'absence de saison lue dans le nom.
        "season": (
            (candidate.extra.get("resolved_season") if candidate else None)
            or (parsed.season if parsed.season is not None else probe.container_season)
        ),
        "episode": (
            (candidate.extra.get("resolved_episode") if candidate else None)
            or (parsed.episode if parsed.episode is not None else probe.container_episode)
        ),
        # Uniquement pour un fichier couvrant plusieurs episodes. Sans lui,
        # « S01E01E02 » et « S01E01 » viseraient le meme nom de destination et
        # le second ecraserait le premier.
        "episode_end": parsed.episode_end,
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
    destination_root: Path,
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
    plan_id = plan_id_for(scanned.path)
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
        destination = resolve_within(destination_root, relative)
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
        identified_by=match.candidate.provider,
        provider=match.candidate.provider,
        external_id=match.candidate.external_id,
        poster_url=match.candidate.poster_url,
        companions=[(c.path, c.destination_for(destination)) for c in companions],
        leftovers=leftovers,
        alternatives=_alternatives(match, all_candidates),
    )


# --- Livres -----------------------------------------------------------------


def build_book_values(scanned: ScannedFile) -> dict[str, Any]:
    """Valeurs du gabarit pour un livre.

    Aucun fournisseur n'intervient : le fichier fait autorite. Un EPUB porte un
    manifeste renseigne par son editeur, la ou un nom de release ment. Aller
    interroger une base pour confirmer ce que l'editeur a lui-meme inscrit
    serait depenser un appel reseau pour rien.
    """
    livre = scanned.book
    parsed = scanned.parsed
    return {
        "title": (livre.title if livre else "") or parsed.title,
        "original_title": parsed.title,
        "author": (livre.author if livre else "") or "",
        "series": (livre.series if livre else "") or "",
        "volume": (livre.volume if livre else None),
        "publisher": (livre.publisher if livre else "") or "",
        "isbn": (livre.isbn if livre else "") or "",
        "year": (livre.year if livre else None) or parsed.year,
        "language": (livre.language if livre else "") or "",
        # Jetons video : vides, mais presents. Un gabarit de livre qui
        # mentionnerait {resolution} par erreur produira un segment vide plutot
        # qu'une erreur de rendu.
        "collection": "",
        "season": None,
        "episode": None,
        "episode_end": None,
        "absolute_episode": None,
        "episode_title": "",
        "resolution": "",
        "source": "",
        "codec": "",
        "edition": "",
        "tmdb_id": "",
        "imdb_id": "",
    }


def build_book_plan(
    scanned: ScannedFile,
    *,
    template: str,
    destination_root: Path,
    with_cleanup: bool = True,
) -> Plan:
    """Plan de rangement d'un livre, sans identification distante.

    Le verdict ne vient pas d'un score de correspondance — il n'y a rien a
    faire correspondre — mais de la QUALITE de ce que le fichier declare :

    - titre ET auteur lus dans le fichier : rien de plus a apprendre ailleurs,
      le rangement peut partir seul ;
    - titre seul, ou devine d'apres le nom : il manque de quoi ranger sous le
      bon auteur, donc un humain regarde ;
    - rien du tout : refuse, avec le motif.
    """
    plan_id = plan_id_for(scanned.path)
    livre = scanned.book
    valeurs = build_book_values(scanned)

    if not valeurs["title"]:
        return Plan(
            id=plan_id,
            source=scanned.path,
            destination=None,
            kind="book",
            score=0.0,
            decision=Decision.REJECT,
            reasons=["aucun titre lisible, ni dans le fichier ni dans son nom"],
            error="non identifie",
        )

    sur = bool(livre and livre.read and livre.trustworthy)
    score = 0.95 if sur else 0.6
    motifs = (
        ["titre et auteur lus dans le fichier"]
        if sur
        else ["metadonnees incompletes : le nom de fichier a servi de secours"]
    )

    try:
        validate(template)
        relative = render(template, valeurs)
        destination = resolve_within(destination_root, relative)
    except (TemplateError, PathConfinementError) as exc:
        return Plan(
            id=plan_id,
            source=scanned.path,
            destination=None,
            kind="book",
            score=score,
            decision=Decision.REJECT,
            reasons=[*motifs, f"chemin impossible : {exc}"],
            title=valeurs["title"],
            error=str(exc),
        )

    destination = destination.with_suffix(scanned.path.suffix)
    compagnons = find_companions(scanned.path, artwork=False)
    return Plan(
        id=plan_id,
        source=scanned.path,
        destination=destination,
        kind="book",
        score=score,
        decision=Decision.AUTO if sur else Decision.REVIEW,
        reasons=motifs,
        identified_by="fichier" if sur else "nom",
        title=valeurs["title"],
        year=valeurs["year"],
        companions=compagnons,
        leftovers=find_leftovers(scanned.path, compagnons) if with_cleanup else [],
    )
