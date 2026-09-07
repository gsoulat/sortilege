"""Score de confiance et decision d'application.

C'est le coeur du comportement de Sortilege : ce qui le separe de Radarr (qui
importe toujours) et de FileBot (qui demande toujours). Tout converge ici.

Chaque candidat identifie porte un ensemble de signaux independants. On les
combine en une confiance 0 -> 1, puis on tranche : appliquer, faire revoir,
ou rejeter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Decision(StrEnum):
    AUTO = "auto"  # applique sans intervention
    REVIEW = "review"  # part dans la file de revue
    REJECT = "reject"  # trop douteux pour meme etre propose


@dataclass(slots=True)
class Signals:
    """Les signaux disponibles pour un candidat.

    Chacun est independant des autres : c'est ce qui rend la combinaison
    informative. Deux signaux forts qui se confirment valent plus que le meme
    signal compte deux fois.
    """

    title_similarity: float
    """Similarite floue entre le titre lu et le titre du candidat, 0 -> 1."""

    parse_quality: float
    """Confiance du parseur dans sa propre lecture, 0 -> 1. Voir parser.py."""

    year_match: bool | None
    """L'annee concorde. None = aucune annee dans le nom de fichier."""

    episode_match: bool | None
    """Saison/episode existent bien chez le provider. None = non applicable."""

    provider_agreement: int
    """Nombre de providers qui pointent la meme oeuvre (0, 1, 2, 3)."""

    provider_popularity: float
    """Notoriete du candidat chez le provider, 0 -> 1. Departage les
    homonymes : entre deux films du meme titre, le connu est plus probable."""

    ai_confidence: float | None
    """Certitude declaree par le resolveur IA. None s'il n'a pas ete appele."""

    ambiguous_candidates: int
    """Nombre de candidats proches ex aequo. >1 = homonymie non tranchee."""


@dataclass(slots=True)
class Policy:
    """Seuils, pilotes par l'environnement. Voir config.py."""

    auto_apply_threshold: float = 0.92
    reject_threshold: float = 0.40
    trust_ai: bool = True
    """Si False, une identification venue de l'IA ne peut jamais passer en
    AUTO — elle plafonne a REVIEW quelle que soit sa confiance."""


def compute_score(signals: Signals) -> float:
    """Combine les signaux en une confiance unique, entre 0.0 et 1.0.

    TODO(guillaume) — a implementer. C'est la piece qui definit le caractere
    de l'outil, elle t'appartient.

    Les arbitrages a trancher :

    * **Poids relatifs.** `title_similarity` est le signal le plus riche, mais
      seul il ne distingue pas « Dune (1984) » de « Dune (2021) ». `year_match`
      est binaire et tres discriminant quand il est disponible — mais il est
      souvent None.
    * **Signaux absents.** Un `year_match` a None doit-il penaliser, ou juste
      ne rien apporter ? Penaliser rend l'outil prudent sur les fichiers mal
      nommes (donc plus de revue manuelle) ; neutraliser le rend fluide mais
      plus exposé aux homonymes.
    * **Accord entre providers.** Deux sources independantes qui concordent
      valent bien plus que le double d'une seule. Une progression non lineaire
      (par ex. 0 / +0.10 / +0.25) traduit mieux cette realite qu'un multiple.
    * **Homonymie.** `ambiguous_candidates > 1` devrait probablement plafonner
      le score plutot que le reduire : deux candidats a egalite, c'est une
      incertitude structurelle qu'aucun autre signal ne compense.
    * **La confiance de l'IA n'est pas commensurable** avec les autres. Elle
      dit « je crois reconnaitre cette oeuvre », pas « les donnees concordent ».
      La traiter comme un signal parmi d'autres, ou comme un plafond ?

    Contrat : retourner une valeur dans [0.0, 1.0].
    """
    raise NotImplementedError("compute_score : a implementer")


def decide(score: float, signals: Signals, policy: Policy) -> Decision:
    """Traduit un score en action.

    Deliberement separe de ``compute_score`` : la mesure et la politique sont
    deux choses distinctes. On peut durcir les seuils sans retoucher au calcul,
    et rejouer d'anciens scores sous une nouvelle politique.
    """
    if signals.ai_confidence is not None and not policy.trust_ai:
        return Decision.REVIEW if score >= policy.reject_threshold else Decision.REJECT

    if score >= policy.auto_apply_threshold:
        return Decision.AUTO
    if score >= policy.reject_threshold:
        return Decision.REVIEW
    return Decision.REJECT


def explain(signals: Signals, score: float, decision: Decision) -> list[str]:
    """Rend la decision lisible dans l'UI.

    Un score nu n'aide pas a arbitrer une revue : ce qui aide, c'est de voir
    quel signal manque. « 0.71 » ne dit rien ; « aucune annee dans le nom,
    2 candidats a egalite » dit tout.
    """
    lines = [f"score {score:.2f} -> {decision.value}"]

    lines.append(f"similarite du titre : {signals.title_similarity:.2f}")
    lines.append(f"qualite de lecture du nom : {signals.parse_quality:.2f}")

    if signals.year_match is None:
        lines.append("aucune annee dans le nom de fichier")
    else:
        lines.append("annee concordante" if signals.year_match else "annee DIVERGENTE")

    if signals.episode_match is False:
        lines.append("saison/episode introuvables chez le provider")

    if signals.provider_agreement >= 2:
        lines.append(f"{signals.provider_agreement} providers concordent")
    elif signals.provider_agreement == 0:
        lines.append("aucun provider n'a confirme")

    if signals.ambiguous_candidates > 1:
        lines.append(f"{signals.ambiguous_candidates} candidats a egalite (homonymie)")

    if signals.ai_confidence is not None:
        lines.append(f"identification assistee par IA (confiance {signals.ai_confidence:.2f})")

    return lines
