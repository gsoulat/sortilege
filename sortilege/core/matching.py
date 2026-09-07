"""Confronte ce qu'on a lu du fichier aux candidats des fournisseurs.

C'est le chainon entre le scan et le scoring : il ne decide rien, il MESURE.
Il produit un objet ``Signals`` que ``scoring.compute_score`` combinera, et le
candidat le plus plausible.

La separation est deliberee : mesurer et trancher sont deux operations
differentes, et pouvoir rejouer d'anciennes mesures sous une nouvelle politique
vaut cher quand on ajuste des seuils.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from ..providers.base import Candidate
from .parser import MediaKind, ParsedName
from .probe import FileProbe, runtime_plausible
from .scoring import Signals

# Deux candidats dont les scores different de moins de ca sont consideres
# comme ex aequo : l'homonymie n'est pas tranchee.
AMBIGUITY_DELTA = 0.06

# En dessous, un candidat n'est meme pas retenu comme plausible.
MIN_SIMILARITY = 0.35

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")

# Articles de tete : « Le Fabuleux Destin » et « Fabuleux Destin » designent la
# meme oeuvre, et les releases les omettent souvent.
_LEADING_ARTICLES = ("le ", "la ", "les ", "l ", "the ", "a ", "an ", "un ", "une ")


@dataclass(slots=True)
class MatchResult:
    """Le meilleur candidat et tout ce qu'on a mesure a son sujet."""

    candidate: Candidate
    signals: Signals
    similarity: float
    runner_up: Candidate | None = None


def normalize_title(value: str) -> str:
    """Forme comparable d'un titre.

    Retire accents, ponctuation et casse. « Amélie Poulain » et « amelie
    poulain » doivent se ressembler a 100 %, sinon une bibliotheque francaise
    perd systematiquement quelques points de similarite pour rien.
    """
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    cleaned = _PUNCT.sub(" ", without_accents.lower())
    return _SPACES.sub(" ", cleaned).strip()


def _strip_article(value: str) -> str:
    for article in _LEADING_ARTICLES:
        if value.startswith(article):
            return value[len(article) :]
    return value


def _pair_similarity(left: str, right: str) -> float:
    """Similarite entre deux titres normalises.

    Combine deux mesures qui echouent differemment :

    - ``SequenceMatcher`` est sensible a l'ordre, donc excellent sur les fautes
      de frappe et les variantes de ponctuation, mauvais sur les inversions.
    - Le recouvrement de mots ignore l'ordre, donc rattrape « Dune Part Two »
      face a « Dune: Part Two » comme face a « Part Two Dune ».

    On prend le maximum : chacune couvre l'angle mort de l'autre.
    """
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    sequence = SequenceMatcher(None, left, right).ratio()

    left_words = set(left.split())
    right_words = set(right.split())
    if left_words and right_words:
        overlap = len(left_words & right_words) / len(left_words | right_words)
    else:
        overlap = 0.0

    return max(sequence, overlap)


def title_similarity(parsed_title: str, candidate: Candidate) -> float:
    """Meilleure similarite entre le titre lu et TOUS les libelles du candidat.

    Le maximum, pas la moyenne : un fichier nomme « Very Bad Trip » doit matcher
    « The Hangover » a plein score des lors que l'un de ses alias correspond.
    Faire la moyenne punirait un candidat riche en alias, ce qui est exactement
    l'inverse du signal recherche.
    """
    left = normalize_title(parsed_title)
    if not left:
        return 0.0

    best = 0.0
    for label in candidate.all_titles():
        right = normalize_title(label)
        best = max(best, _pair_similarity(left, right))
        # Second essai sans article de tete, des deux cotes.
        best = max(best, _pair_similarity(_strip_article(left), _strip_article(right)))
        if best >= 1.0:
            break
    return best


def _external_id_match(probe: FileProbe, candidate: Candidate) -> bool | None:
    """L'identifiant declare designe-t-il CE candidat ?

    True  : l'identifiant du fichier et celui du candidat coincident.
    False : ils se contredisent — le candidat est disqualifie.
    None  : aucune comparaison possible (pas d'identifiant, ou pas dans le meme
            referentiel). C'est le cas le plus frequent, et il ne doit surtout
            pas etre confondu avec False.
    """
    declared = {
        "tmdb": probe.tmdb_id,
        "tvdb": probe.tvdb_id,
        "anilist": None,
    }.get(candidate.provider)

    if not declared or not candidate.external_id:
        return None
    return str(declared) == str(candidate.external_id)


def _year_match(parsed: ParsedName, probe: FileProbe, candidate: Candidate) -> bool | None:
    """Concordance d'annee, avec une tolerance d'un an.

    La tolerance n'est pas de la complaisance : une sortie decembre/janvier
    fait diverger d'un an la date de sortie salle et celle du support, sans
    qu'aucune des deux soit fausse.
    """
    expected = parsed.year or probe.nfo_year
    if expected is None or candidate.year is None:
        return None
    return abs(expected - candidate.year) <= 1


def _agreement(best: Candidate, candidates: list[Candidate]) -> int:
    """Nombre de fournisseurs DISTINCTS designant la meme oeuvre.

    On regroupe par titre normalise et annee, faute d'identifiant commun entre
    referentiels. Deux sources independantes qui convergent constituent le
    signal le plus solide dont on dispose apres un identifiant declare.
    """
    key = (normalize_title(best.title), best.year)
    providers = {
        c.provider
        for c in candidates
        if (normalize_title(c.title), c.year) == key or title_similarity(best.title, c) >= 0.92
    }
    return len(providers)


def _container_similarity(probe: FileProbe, candidate: Candidate) -> float | None:
    if not probe.container_title:
        return None
    return title_similarity(probe.container_title, candidate)


def build_signals(
    parsed: ParsedName,
    probe: FileProbe,
    candidate: Candidate,
    all_candidates: list[Candidate],
    *,
    episode_match: bool | None = None,
) -> Signals:
    """Mesure tout ce qu'on peut dire d'un candidat, sans rien trancher."""
    similarity = title_similarity(parsed.title or probe.nfo_title or "", candidate)

    scored = sorted((title_similarity(parsed.title, c) for c in all_candidates), reverse=True)
    ambiguous = sum(1 for s in scored if abs(s - similarity) <= AMBIGUITY_DELTA)

    kind = "movie" if parsed.kind is MediaKind.MOVIE else "episode"

    return Signals(
        title_similarity=similarity,
        parse_quality=parsed.quality,
        year_match=_year_match(parsed, probe, candidate),
        episode_match=episode_match,
        provider_agreement=_agreement(candidate, all_candidates),
        provider_popularity=candidate.popularity,
        ai_confidence=None,
        ambiguous_candidates=max(ambiguous, 1),
        external_id_match=_external_id_match(probe, candidate),
        runtime_plausible=runtime_plausible(probe.duration_seconds, kind),
        container_title_similarity=_container_similarity(probe, candidate),
    )


def best_match(
    parsed: ParsedName,
    probe: FileProbe,
    candidates: list[Candidate],
    *,
    episode_match: bool | None = None,
) -> MatchResult | None:
    """Choisit le candidat le plus plausible et mesure ses signaux.

    Le classement ici est volontairement grossier — similarite de titre, puis
    identifiant declare, puis notoriete. La ponderation fine appartient a
    ``compute_score`` ; ce tri ne sert qu'a designer QUI mesurer en detail.
    """
    if not candidates:
        return None

    def rank(c: Candidate) -> tuple[float, float, float]:
        similarity = title_similarity(parsed.title or probe.nfo_title or "", c)
        # Un identifiant declare concordant place le candidat en tete quoi
        # qu'il arrive ; un identifiant contredit le relegue.
        id_match = _external_id_match(probe, c)
        id_rank = 1.0 if id_match is True else (-1.0 if id_match is False else 0.0)
        return (id_rank, similarity, c.popularity)

    ordered = sorted(candidates, key=rank, reverse=True)
    winner = ordered[0]

    similarity = title_similarity(parsed.title or probe.nfo_title or "", winner)
    if similarity < MIN_SIMILARITY and _external_id_match(probe, winner) is not True:
        return None

    return MatchResult(
        candidate=winner,
        signals=build_signals(parsed, probe, winner, candidates, episode_match=episode_match),
        similarity=similarity,
        runner_up=ordered[1] if len(ordered) > 1 else None,
    )
