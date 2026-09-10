"""Regroupement des series d'une meme franchise.

Le probleme : « Star Trek », « Star Trek: Discovery », « Star Trek: Picard » et
« Star Trek: Strange New Worlds » sont quatre series distinctes chez TMDB, sans
aucun lien declare. Il n'existe pas d'equivalent series de
``belongs_to_collection`` — c'est une lacune de TMDB, pas un oubli de notre
part.

La regle qui marche en pratique : le sous-titre suit un separateur. « Star
Trek: Discovery » se lit « franchise : episode de la franchise ». On prend donc
la partie gauche.

Limite assumee, a connaitre avant d'activer le jeton dans un gabarit : une
serie unique dont le titre porte un sous-titre (« The Witcher: Blood Origin »)
se retrouvera seule dans un dossier de franchise. Ce n'est pas faux — c'est
bien la meme franchise — mais cela cree un niveau pour un seul element. On ne
peut pas faire mieux sans connaitre toute la bibliotheque au moment du
renommage.
"""

from __future__ import annotations

import re

# Separateurs de sous-titre, par ordre de fiabilite. Le deux-points est de loin
# le plus sur ; le tiret entoure d'espaces est plus risque (« Kaamelott - Livre
# I » n'est pas une franchise) mais reste majoritairement correct.
# Le tiret cadratin est intentionnel : les titres francais l'utilisent.
_SPLITTERS = (": ", " : ", " - ", " – ")  # noqa: RUF001

MIN_FRANCHISE_LEN = 3

# Prefixes trop generiques pour constituer un regroupement utile : ils
# rassembleraient des series sans aucun rapport.
_TOO_GENERIC = {
    "the",
    "le",
    "la",
    "les",
    "un",
    "une",
    "a",
    "an",
    "new",
    "young",
    "little",
    "big",
    "my",
    "our",
    "saison",
    "season",
    "serie",
    "series",
    "part",
    "chapter",
    "chapitre",
    "documentaire",
    "documentary",
    "special",
    "live",
}

_TRAILING_PUNCT = re.compile(r"[\s:\-–—,.]+$")  # noqa: RUF001


def derive_franchise(title: str) -> str | None:
    """Franchise deduite d'un titre de serie, ou None.

    >>> derive_franchise("Star Trek: Discovery")
    'Star Trek'
    >>> derive_franchise("Severance") is None
    True
    """
    if not title:
        return None

    for splitter in _SPLITTERS:
        head, sep, tail = title.partition(splitter)
        if not sep or not tail.strip():
            continue

        candidate = _TRAILING_PUNCT.sub("", head).strip()
        if len(candidate) < MIN_FRANCHISE_LEN:
            continue

        # Un prefixe d'un seul mot generique ne regroupe rien d'utile.
        words = candidate.lower().split()
        if len(words) == 1 and words[0] in _TOO_GENERIC:
            continue
        if all(w in _TOO_GENERIC for w in words):
            continue

        return candidate

    return None


def franchise_for(title: str, siblings: list[str] | None = None) -> str | None:
    """Franchise d'une serie, avec confirmation par la bibliotheque si possible.

    ``siblings`` est la liste des autres titres de series deja connus. Quand
    elle est fournie, la franchise n'est retenue que si AU MOINS UNE autre
    serie la partage — ce qui evite le dossier a un seul element.

    C'est la meme deduction que ``derive_franchise``, simplement corroboree :
    un regroupement n'a de sens que s'il regroupe effectivement.
    """
    candidate = derive_franchise(title)

    if candidate is None:
        # La serie d'ORIGINE n'a pas de sous-titre : « Star Trek » (1966) ne
        # revele rien de lui-meme. Il n'est une franchise que parce que
        # « Star Trek: Discovery » existe a cote. Sans ce cas, la serie
        # fondatrice se retrouvait rangee A COTE du dossier portant son nom —
        # le regroupement laissait dehors ce qu'il regroupait.
        if siblings is None:
            return None
        folded = title.casefold()
        for other in siblings:
            if other == title:
                continue
            derived = derive_franchise(other)
            if derived is not None and derived.casefold() == folded:
                return title
            # Les voisins lus sur le DISQUE ont perdu leur deux-points en
            # devenant des noms de dossier : « Star Trek: Discovery » y figure
            # sous « Star Trek Discovery », dont plus rien ne se derive. Le
            # prefixe suffit alors — et il faut qu'il reste quelque chose
            # apres, sans quoi « Star Trek » se declarerait sa propre franchise.
            if other.casefold().startswith(f"{folded} ") and len(other) > len(title) + 1:
                return title
        return None

    if siblings is None:
        return candidate

    needle = candidate.casefold()
    for other in siblings:
        if other == title:
            continue
        other_folded = other.casefold()
        # Soit l'autre serie porte la meme franchise en prefixe, soit elle EST
        # la franchise (la serie d'origine, « Star Trek » tout court).
        if other_folded == needle or derive_franchise(other) == candidate:
            return candidate

    return None
