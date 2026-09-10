"""Strategies de qualite : quel exemplaire garder quand il y en a plusieurs.

Jusqu'ici, la reponse etait codee en dur — la plus haute resolution gagne, et a
resolution egale le plus gros fichier. C'est une preference deguisee en regle,
et elle ne convient pas a tout le monde : un remux 4K de soixante gigaoctets
n'a pas la meme valeur pour qui archive un film et pour qui garde deux cents
episodes d'une serie sur un NAS.

La strategie se choisit donc, ET par type d'oeuvre. C'est le point ou cette
approche depasse celle de Radarr, qui impose une echelle unique : en pratique
on veut souvent du 2160p pour un film qu'on regardera une fois avec attention,
et du 720p pour une serie de deux cents episodes qu'on laisse tourner.

Une strategie n'est qu'un ORDRE DE PREFERENCE sur les resolutions, plus une
regle pour departager a resolution egale. Rien de plus : ce qui rend le systeme
comprehensible, c'est qu'on peut lire la liste et savoir ce qui va se passer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Resolutions reconnues, de la plus haute a la plus basse. L'ordre sert de
# reference commune ; chaque strategie definit ensuite SA preference, qui n'a
# aucune raison de suivre celui-ci.
LADDER = ("2160p", "1080p", "720p", "576p", "480p")

_NORMALISE = {
    "4k": "2160p",
    "uhd": "2160p",
    "2160i": "2160p",
    "1080i": "1080p",
    "720i": "720p",
}

_HAUTEUR = re.compile(r"(?P<hauteur>\d{3,4})[pi]?$")


def normalise(brut: str | None) -> str:
    """Ramene une resolution ecrite n'importe comment a une valeur connue.

    « 4K », « UHD », « 2160i » designent la meme chose que « 2160p ». Les
    traiter comme des valeurs distinctes ferait echouer toute comparaison, et
    l'utilisateur ne comprendrait pas pourquoi son 4K n'est pas reconnu comme
    tel.
    """
    if not brut:
        return ""
    valeur = brut.strip().lower()
    if valeur in _NORMALISE:
        return _NORMALISE[valeur]
    if valeur in LADDER:
        return valeur
    # Une hauteur mesuree par ffprobe : on la rabat sur le palier connu le plus
    # proche par le bas, faute de quoi un 1088p ne serait rien.
    if trouve := _HAUTEUR.match(valeur):
        hauteur = int(trouve.group("hauteur"))
        for palier in LADDER:
            if hauteur >= int(palier[:-1]) - 40:
                return palier
    return ""


@dataclass(frozen=True, slots=True)
class Strategy:
    """Un ordre de preference, et une facon de departager a egalite."""

    key: str
    label: str
    summary: str
    order: tuple[str, ...]
    prefer_bigger: bool = True
    """A resolution egale, le plus gros a generalement le meilleur debit. On
    peut vouloir l'inverse quand la place prime."""

    def rank(self, resolution: str | None, size_bytes: int) -> tuple[int, int]:
        """Rang d'un fichier : plus c'est GRAND, mieux c'est.

        Une resolution absente arrive derniere. C'est deliberé : un fichier dont
        on ne sait rien ne doit pas l'emporter sur un fichier connu, meme
        modeste — on ne remplace pas une certitude par une inconnue.
        """
        valeur = normalise(resolution)
        try:
            position = len(self.order) - self.order.index(valeur)
        except ValueError:
            position = 0
        return (position, size_bytes if self.prefer_bigger else -size_bytes)


STRATEGIES: tuple[Strategy, ...] = (
    Strategy(
        key="quality",
        label="Qualité maximale",
        summary="La plus haute résolution disponible, sans considération de place.",
        order=("2160p", "1080p", "720p", "576p", "480p"),
    ),
    Strategy(
        key="balanced",
        label="Équilibré",
        summary=(
            "Le 1080p d'abord : le meilleur rapport qualité/place. La 4K passe "
            "APRÈS le 720p — elle triple le poids pour un gain que peu d'écrans "
            "restituent."
        ),
        order=("1080p", "720p", "2160p", "576p", "480p"),
    ),
    Strategy(
        key="compact",
        label="Économie de place",
        summary=(
            "Le 720p d'abord, suffisant sur la plupart des écrans pour une série "
            "qu'on laisse tourner. La 4K en dernier."
        ),
        order=("720p", "1080p", "576p", "480p", "2160p"),
        prefer_bigger=False,
    ),
)

BY_KEY = {s.key: s for s in STRATEGIES}
DEFAULT = "quality"


def strategy(key: str | None) -> Strategy:
    """Strategie nommee, ou celle par defaut. Ne leve jamais : un reglage
    devenu invalide ne doit pas empecher un rangement."""
    return BY_KEY.get(key or "", BY_KEY[DEFAULT])


@dataclass
class QualitySettings:
    """Strategie par type d'oeuvre.

    Par type et non globale, parce que c'est la que le besoin se pose : la
    meme personne veut souvent la meilleure image pour ses films et la plus
    petite empreinte pour ses series.
    """

    movie: str = DEFAULT
    episode: str = DEFAULT
    anime: str = DEFAULT

    def for_kind(self, kind: str) -> Strategy:
        if kind not in ("movie", "episode", "anime"):
            return strategy(None)
        return strategy(getattr(self, kind, None))


@dataclass
class Comparison:
    """Verdict lisible entre deux exemplaires."""

    keep_candidate: bool
    reason: str = ""
    fields: dict[str, str] = field(default_factory=dict)


def compare(
    strat: Strategy,
    *,
    candidate_resolution: str | None,
    candidate_size: int,
    incumbent_resolution: str | None,
    incumbent_size: int,
) -> Comparison:
    """Lequel des deux garder, et POURQUOI.

    Le motif compte autant que le verdict : remplacer un fichier de
    bibliotheque sans dire ce qui l'a emporte laisse l'utilisateur devant un
    resultat qu'il ne peut ni verifier ni contester.
    """
    rang_candidat = strat.rank(candidate_resolution, candidate_size)
    rang_place = strat.rank(incumbent_resolution, incumbent_size)

    res_candidat = normalise(candidate_resolution) or "inconnue"
    res_place = normalise(incumbent_resolution) or "inconnue"

    if rang_candidat == rang_place:
        return Comparison(False, "les deux exemplaires se valent pour cette strategie")

    garde = rang_candidat > rang_place
    gagnant, perdant = (res_candidat, res_place) if garde else (res_place, res_candidat)
    motif = (
        f"{gagnant} l'emporte sur {perdant}"
        if gagnant != perdant
        else f"a resolution egale ({gagnant}), la taille departage"
    )
    return Comparison(
        garde,
        f"{motif} — stratégie « {strat.label} »",
        {"candidate": res_candidat, "incumbent": res_place},
    )
