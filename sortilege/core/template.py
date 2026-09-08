"""Moteur de gabarit : construit un chemin de destination depuis des jetons.

Volontairement PAUVRE. FileBot execute des expressions Groovy — c'est-a-dire du
code arbitraire — pour construire un nom de fichier. C'est puissant et c'est une
execution de code sur le NAS des qu'un gabarit vient d'ailleurs.

Ici : substitution de jetons, un conditionnel declaratif, rien d'autre.
Pas d'``eval``, pas d'appel de methode, pas de boucle.

Syntaxe
-------
``{jeton}``            substitue, vide si absent
``{jeton:02}``         entier zero-padde sur N chiffres
``{?jeton:texte}``     n'ecrit ``texte`` que si le jeton est renseigne ;
                       ``$`` dans ``texte`` est remplace par la valeur

Exemple
-------
    Series/{title}{? year: ($)}/Season {season:02}/
    {title} - S{season:02}E{episode:02}{? episode_title: - $}

(ecrit sur une seule ligne en pratique ; coupe ici pour la lisibilite)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .safety import sanitize_segment

# {jeton}, {jeton:02}, {?jeton:texte}
# Les espaces autour du nom de jeton sont tolerES : ecrire « {? year: ($)} »
# est bien plus lisible que « {?year: ($)} » dans le constructeur visuel.
# Le corps d'un conditionnel ne peut pas contenir d'accolade — pas de jeton
# imbrique, ce qui garde le motif non recursif et donc analysable en une passe.
_TOKEN = re.compile(
    r"\{(?:"
    r"\?\s*(?P<cond_name>[a-z_]+)\s*:(?P<cond_body>[^{}]*)"
    r"|"
    r"\s*(?P<name>[a-z_]+)\s*(?::(?P<pad>0\d))?"
    r")\}"
)

MAX_TEMPLATE_LEN = 500


@dataclass(frozen=True, slots=True)
class Token:
    """Un jeton, tel que l'UI l'affiche dans la palette de glisser-deposer."""

    name: str
    label: str
    example: str
    group: str


# Catalogue expose a l'UI par GET /api/tokens : la palette est construite
# depuis cette liste, elle n'est jamais dupliquee cote frontend.
TOKENS: tuple[Token, ...] = (
    Token("title", "Titre", "Le Fabuleux Destin d'Amélie Poulain", "identite"),
    Token("original_title", "Titre original", "Amélie", "identite"),
    Token("year", "Année", "2001", "identite"),
    # Saga / franchise, depuis belongs_to_collection chez TMDB. Vide pour un
    # film isole — et un segment vide disparait du chemin, donc le meme gabarit
    # sert aux deux cas sans conditionnel.
    # Films : belongs_to_collection chez TMDB. Series : deduit du prefixe du
    # titre (« Star Trek: Discovery » -> « Star Trek »), TMDB n'exposant aucun
    # champ de franchise pour les series.
    Token("collection", "Saga / Franchise", "Star Trek", "identite"),
    Token("season", "Saison", "2", "episode"),
    Token("episode", "Épisode", "7", "episode"),
    Token("episode_title", "Titre de l'épisode", "Le Silence", "episode"),
    Token("absolute_episode", "Épisode absolu", "147", "episode"),
    Token("resolution", "Résolution", "1080p", "technique"),
    Token("source", "Source", "bluray", "technique"),
    Token("codec", "Codec", "x265", "technique"),
    Token("language", "Langue", "multi", "technique"),
    Token("edition", "Édition", "Director's Cut", "technique"),
    Token("tmdb_id", "Identifiant TMDB", "194", "identifiants"),
    Token("imdb_id", "Identifiant IMDb", "tt0211915", "identifiants"),
)

TOKEN_NAMES = frozenset(t.name for t in TOKENS)

# Gabarits prets a l'emploi, proposes en un clic dans l'UI.
#
# Ils sont RELATIFS a la destination du type, reglee dans l'interface. Y
# remettre un prefixe « Films/ » ferait doublon : la destination vaut deja
# « Films », et changer ce reglage n'aurait plus aucun effet puisque le gabarit
# imposerait le sien.
PRESETS: dict[str, dict[str, str]] = {
    "jellyfin": {
        # L'annee passe par le conditionnel et non par « ({year}) » en dur :
        # un film sans annee produirait sinon un « () » vide dans le chemin.
        #
        # {collection} regroupe les sagas : tous les Hunger Games sous un meme
        # dossier. Aucun conditionnel n'est necessaire — un jeton vide produit
        # un segment vide, et un segment vide disparait du chemin. Le meme
        # gabarit sert donc au film isole comme au film de saga.
        "movie": ("{collection}/{title}{? year: ($)}/{title}{? year: ($)}{? resolution: [$]}"),
        # Franchise / serie / saison. Le premier segment disparait quand la
        # serie n'appartient a aucune franchise.
        "episode": (
            "{collection}/{title}{? year: ($)}/Season {season:02}/"
            "{title} - S{season:02}E{episode:02}{? episode_title: - $}"
        ),
        # Numerotation absolue : c'est ce que portent les releases de fansub, et
        # {episode} est souvent absent tant que la correspondance saison/episode
        # n'a pas ete resolue chez le provider.
        "anime": "{title}/{title} - {absolute_episode:03}{? episode_title: - $}",
    },
    "plex": {
        # Plex accepte un identifiant entre accolades dans le nom, mais les
        # accolades sont la syntaxe des jetons : on ne l'expose pas ici.
        "movie": "{collection}/{title}{? year: ($)}/{title}{? year: ($)}",
        "episode": ("{title}{? year: ($)}/Season {season:02}/{title} - s{season:02}e{episode:02}"),
        "anime": "{title}/{title} - {absolute_episode:03}",
    },
}


class TemplateError(Exception):
    """Gabarit invalide — refuse avant d'etre enregistre."""


def validate(template: str) -> None:
    """Rejette un gabarit malforme. Appele a l'enregistrement, pas au rendu."""
    if not template.strip():
        raise TemplateError("gabarit vide")
    if len(template) > MAX_TEMPLATE_LEN:
        raise TemplateError(f"gabarit trop long (max {MAX_TEMPLATE_LEN})")
    if template.startswith("/"):
        raise TemplateError("le gabarit doit etre relatif a la destination du type")

    for match in _TOKEN.finditer(template):
        name = match.group("name") or match.group("cond_name")
        if name not in TOKEN_NAMES:
            raise TemplateError(f"jeton inconnu : {{{name}}}")

    # Une accolade orpheline signale presque toujours une faute de frappe.
    residue = _TOKEN.sub("", template)
    if "{" in residue or "}" in residue:
        raise TemplateError("accolade non appariee ou jeton malforme")


def _format_value(value: Any, pad: str | None) -> str:
    if value is None or value == "":
        return ""
    if pad and isinstance(value, int):
        return str(value).zfill(int(pad[1:]))
    return str(value)


def render(template: str, values: dict[str, Any]) -> str:
    """Rend un chemin RELATIF. Le confinement reste a la charge de safety.py.

    Chaque valeur substituee est assainie : une metadonnee vient d'une API
    tierce ou d'un LLM, elle ne doit jamais introduire de separateur de chemin.
    Les separateurs du gabarit lui-meme, eux, sont conserves — c'est l'auteur
    du gabarit qui decide de l'arborescence.
    """

    def replace(match: re.Match[str]) -> str:
        if cond_name := match.group("cond_name"):
            value = values.get(cond_name)
            if value is None or value == "":
                return ""
            body = match.group("cond_body")
            return body.replace("$", sanitize_segment(str(value)))

        name = match.group("name")
        rendered = _format_value(values.get(name), match.group("pad"))
        return sanitize_segment(rendered) if rendered else ""

    path = _TOKEN.sub(replace, template)

    # Un jeton vide peut laisser « //  » ou des espaces en trop.
    path = re.sub(r"\s{2,}", " ", path)
    path = "/".join(seg.strip() for seg in path.split("/") if seg.strip())
    return path


def preview(template: str, samples: list[dict[str, Any]]) -> list[str]:
    """Rend le gabarit sur des exemples — alimente l'apercu live de l'UI."""
    validate(template)
    return [render(template, s) for s in samples]
