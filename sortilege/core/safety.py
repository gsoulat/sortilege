"""Confinement des chemins.

Regle non negociable : aucune destination calculee ne doit pouvoir sortir de la
racine de bibliotheque declaree. Un titre de metadonnees est une chaine venant
d'une API tierce ou d'un LLM — donc une entree non fiable. Un titre contenant
``../..``, un separateur, ou un octet nul ne doit jamais devenir un chemin.

C'est la faille numero un de ce type d'application : on fait confiance au
provider, il renvoie un titre exotique, et on ecrit hors de la bibliotheque.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

# Caracteres interdits dans un segment de chemin, tous OS confondus.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Noms reserves Windows — inoffensifs sous Linux mais la bibliotheque est
# souvent exposee en SMB, autant ne pas creer de dossier impossible a lire.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_SEGMENT_LEN = 180


class PathConfinementError(Exception):
    """La destination calculee sort de la racine autorisee."""


def sanitize_segment(value: str) -> str:
    """Rend une valeur utilisable comme UN segment de chemin.

    Ne renvoie jamais une chaine contenant un separateur : un titre malveillant
    ou simplement bizarre est aplati, pas interprete.
    """
    value = unicodedata.normalize("NFC", value)
    value = _ILLEGAL.sub("", value)
    value = value.replace("‮", "")  # override droite-a-gauche
    value = value.strip(" .")            # un segment finissant par . ou espace casse SMB

    if value.upper().split(".")[0] in _RESERVED:
        value = f"_{value}"

    if len(value) > MAX_SEGMENT_LEN:
        value = value[:MAX_SEGMENT_LEN].rstrip(" .")

    return value or "_"


def resolve_within(root: Path, relative: str) -> Path:
    """Resout ``relative`` sous ``root`` en garantissant le confinement.

    Chaque segment est assaini individuellement, puis le resultat est verifie
    une seconde fois avec ``resolve()``. La double verification est volontaire :
    l'assainissement seul ne couvre pas les liens symboliques deja presents dans
    la bibliotheque.
    """
    root = root.resolve()

    segments = [sanitize_segment(s) for s in relative.split("/") if s not in ("", ".", "..")]
    if not segments:
        raise PathConfinementError("chemin relatif vide apres assainissement")

    candidate = root.joinpath(*segments)

    # strict=False : la destination n'existe pas encore, c'est normal.
    resolved = candidate.resolve(strict=False)
    if resolved != root and root not in resolved.parents:
        raise PathConfinementError(
            f"destination hors racine : {resolved} n'est pas sous {root}"
        )

    return resolved


def assert_readable_source(path: Path, allowed_roots: list[Path]) -> Path:
    """Verifie qu'un fichier source appartient bien a une racine declaree."""
    resolved = path.resolve(strict=True)
    for root in allowed_roots:
        root = root.resolve()
        if resolved == root or root in resolved.parents:
            return resolved
    raise PathConfinementError(f"source hors des racines autorisees : {resolved}")
