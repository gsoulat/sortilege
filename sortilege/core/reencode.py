"""Ce qui ne respecte pas la strategie, et ce que le reencodage rendrait.

Choisir une strategie de qualite ne change rien aux fichiers deja ranges. Une
bibliotheque constituee avant le reglage — ou constituee au hasard de ce qui
etait disponible — contient des remux 4K de soixante gigaoctets alors que la
strategie dit « 1080p ». Ces fichiers ne sont pas des doublons : il n'y a rien
a arbitrer, rien a supprimer. Ils sont simplement plus lourds que ce qui a ete
demande.

Ce module ne fait qu'une chose : DIRE lesquels, et combien de place ils
coutent. Il ne touche a aucun fichier. C'est deliberé — reencoder est long,
irreversible sur la qualite, et personne ne doit lancer ca sans avoir vu
d'abord ce que ca porte.

**La regle de violation ne s'invente pas ici** : elle se deduit de la strategie
deja choisie. Un fichier viole sa strategie s'il existe une resolution PLUS
BASSE que la sienne que cette strategie classe MIEUX. C'est exactement ce que
l'utilisateur a demande en choisissant « Équilibré » ou « Économie de place »,
et cela a une consequence qu'on veut : « Qualité maximale » ne propose jamais
rien, puisque aucune resolution inferieure n'y est mieux classee.

On ne remonte jamais. Reencoder vers le haut ne cree pas de detail, ne fait
que gonfler le fichier — c'est la seule operation qui perd sur les deux
tableaux.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .quality import QualitySettings, Strategy, normalise

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterable

    from .collection import Work

# En dessous, l'operation ne vaut pas son cout. Une heure de calcul et un
# reencodage avec perte pour trois cents megaoctets est un mauvais marche : on
# depense de la qualite pour une place qu'on ne verra pas.
MIN_SAVINGS_BYTES = 1024**3

# Le debit ne baisse pas aussi vite que le nombre de pixels. Diviser la hauteur
# par deux divise les pixels par quatre, mais le poids par trois environ
# seulement : moins de pixels, mais chacun porte plus de detail apres
# redimensionnement, et l'encodeur y consacre davantage de bits.
#
# Applique a la hauteur, cela donne un exposant de 1,5 (pixels au carre de la
# hauteur, puis 0,75 pour l'effet ci-dessus). L'estimation est volontairement
# PRUDENTE : mieux vaut annoncer moins de place recuperee que promettre une
# economie qui ne viendra pas.
_EXPOSANT = 1.5


def _hauteur(resolution: str) -> int:
    """Hauteur en pixels d'un palier normalise. 0 si inconnu."""
    return int(resolution[:-1]) if resolution and resolution[:-1].isdigit() else 0


def target_for(strat: Strategy, resolution: str | None) -> str:
    """Resolution visee par la strategie, ou chaine vide si le fichier convient.

    On ne considere que les paliers STRICTEMENT plus bas : reencoder vers le
    haut n'ajoute aucun detail et ne fait que gonfler le fichier.
    """
    courante = normalise(resolution)
    hauteur = _hauteur(courante)
    if not hauteur:
        # Une resolution inconnue ne se juge pas. Proposer un reencodage sur une
        # supposition serait detruire de la qualite au hasard.
        return ""

    try:
        rang_courant = strat.order.index(courante)
    except ValueError:
        return ""

    meilleurs = [
        palier
        for palier in strat.order[:rang_courant]
        if 0 < _hauteur(palier) < hauteur  # strictement plus bas, et connu
    ]
    return meilleurs[0] if meilleurs else ""


def estimate_bytes(size_bytes: int, resolution: str, target: str) -> int:
    """Poids attendu apres reencodage. Estimation, jamais une promesse."""
    depart, arrivee = _hauteur(normalise(resolution)), _hauteur(target)
    if not depart or not arrivee or arrivee >= depart:
        return size_bytes
    return int(size_bytes * (arrivee / depart) ** _EXPOSANT)


@dataclass(slots=True)
class Candidate:
    """Un fichier plus lourd que ce que la strategie demande."""

    relative_path: str
    title: str
    kind: str
    resolution: str
    target: str
    codec: str
    size_bytes: int
    estimated_bytes: int
    strategy_label: str

    @property
    def savings_bytes(self) -> int:
        return max(0, self.size_bytes - self.estimated_bytes)

    @property
    def reason(self) -> str:
        return f"{self.resolution} alors que « {self.strategy_label} » prefere {self.target}"


def audit(
    works: Iterable[Work],
    settings: QualitySettings | None = None,
    *,
    min_savings_bytes: int = MIN_SAVINGS_BYTES,
) -> list[Candidate]:
    """Fichiers de la bibliotheque qui ne respectent pas leur strategie.

    Tries par place recuperable decroissante : c'est l'ordre dans lequel on
    veut agir, puisque le premier fichier de la liste rend a lui seul plus que
    les vingt suivants.
    """
    reglage = settings or QualitySettings()
    candidats: list[Candidate] = []

    for work in works:
        strat = reglage.for_kind(work.kind)
        for fichiers in work.slots.values():
            for ref in fichiers:
                vise = target_for(strat, ref.resolution)
                if not vise:
                    continue
                estime = estimate_bytes(ref.size_bytes, ref.resolution, vise)
                candidat = Candidate(
                    relative_path=ref.relative_path,
                    title=work.title,
                    kind=work.kind,
                    resolution=normalise(ref.resolution),
                    target=vise,
                    codec=ref.codec,
                    size_bytes=ref.size_bytes,
                    estimated_bytes=estime,
                    strategy_label=strat.label,
                )
                if candidat.savings_bytes >= min_savings_bytes:
                    candidats.append(candidat)

    candidats.sort(key=lambda c: c.savings_bytes, reverse=True)
    return candidats


def recoverable_bytes(candidats: list[Candidate]) -> int:
    return sum(c.savings_bytes for c in candidats)


def by_target(candidats: list[Candidate]) -> dict[str, int]:
    """Combien de fichiers par resolution visee. Sert a annoncer l'ampleur du
    travail avant de le lancer : « 12 vers 1080p » se decide, « 340 fichiers »
    ne se decide pas."""
    compte: dict[str, int] = {}
    for c in candidats:
        compte[c.target] = compte.get(c.target, 0) + 1
    return compte


__all__ = [
    "MIN_SAVINGS_BYTES",
    "Candidate",
    "audit",
    "by_target",
    "estimate_bytes",
    "recoverable_bytes",
    "target_for",
]
