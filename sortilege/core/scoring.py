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

    # --- Signaux issus du fichier lui-meme (voir core/probe.py) -------------
    # Leur interet n'est pas d'etre nombreux mais d'etre DECORRELES du nom :
    # un nom de release et une duree ne se trompent pas de la meme facon, donc
    # leur accord vaut confirmation, pas redondance.

    external_id_match: bool | None = None
    """Un identifiant TMDB/IMDb/TVDB declare dans un .nfo ou les tags du
    conteneur designe ce candidat. True = identite declaree, pas ressemblance :
    c'est le signal le plus fort du systeme. False = l'identifiant declare
    designe quelqu'un d'AUTRE, ce qui doit disqualifier. None = aucun
    identifiant disponible."""

    runtime_plausible: bool | None = None
    """La duree reelle du fichier est compatible avec le type retenu. Un
    « film » de 22 minutes est un episode mal classe. None = duree inconnue
    (ffprobe absent ou fichier illisible)."""

    container_title_similarity: float | None = None
    """Similarite entre le titre ecrit dans les tags du conteneur et celui du
    candidat. None = le conteneur ne porte pas de titre exploitable."""


AUTO_APPLY_THRESHOLD = 0.92
"""Au-dessus : on applique sans demander. Regle depuis l'environnement."""

REJECT_THRESHOLD = 0.40
"""En dessous : rejet direct, sans encombrer la file de revue."""

# Ces deux valeurs sont declarees ICI et nulle part ailleurs. ``config.py`` les
# reprend comme defaut de ses variables d'environnement : les ecrire aux deux
# endroits marchait tant que personne n'en changeait qu'un seul, et le jour ou
# cela arrive la divergence est muette — le comportement depend alors de qui,
# de l'appelant ou du defaut, a servi.


@dataclass(slots=True)
class Policy:
    """Seuils, pilotes par l'environnement. Voir config.py."""

    auto_apply_threshold: float = AUTO_APPLY_THRESHOLD
    reject_threshold: float = REJECT_THRESHOLD
    trust_ai: bool = True
    """Si False, une identification venue de l'IA ne peut jamais passer en
    AUTO — elle plafonne a REVIEW quelle que soit sa confiance."""

    trust_external_ids: bool = True
    """Si True, un identifiant declare concordant suffit a appliquer sans
    revue. Un tmdbid dans un .nfo n'est pas une ressemblance a evaluer, c'est
    une reponse : le faire passer par le calcul de score reviendrait a douter
    d'une certitude. Mettre a False pour un import prudent depuis une source
    dont on ne fait pas confiance aux .nfo."""


# --- Poids du calcul de confiance -------------------------------------------
#
# Isoles pour etre regles apres observation du comportement reel : ce sont des
# valeurs de depart raisonnables, pas des constantes physiques. La bonne facon
# de les ajuster est de lancer un scan, regarder ce qui atterrit en revue, et
# deplacer le curseur du signal qui a mal juge.

BASE_CREDIT = 0.12
"""Credit de depart accorde des lors qu'un candidat plausible existe.

Sans lui, un fichier au titre correct mais sans annee ni second fournisseur
tombait sous le seuil de rejet — donc ecarte en silence alors qu'il est
seulement ambigu. Or ``matching`` a deja elimine les candidats sous 0.35 de
similarite : tout ce qui arrive ici est deja une piste serieuse, et merite au
minimum un regard humain plutot qu'une poubelle."""

W_TITLE = 0.50
"""Le signal le plus riche, donc le plus lourd. Mais seul il ne distingue pas
« Dune (1984) » de « Dune (2021) » — d'ou tout le reste."""

W_PARSE_QUALITY = 0.10
"""Confiance du parseur dans sa propre lecture. Faible : elle mesure la
lisibilite du nom, pas la justesse de l'identification."""

BONUS_YEAR = 0.20
MALUS_YEAR = -0.35
"""Asymetrique a dessein. Une annee qui concorde confirme ; une annee qui
diverge CONTREDIT, et une contradiction pese plus lourd qu'une confirmation.
Une annee absente ne fait rien : « je ne sais pas » n'est pas « c'est faux »."""

BONUS_EPISODE = 0.05
MALUS_EPISODE = -0.30
"""Meme logique. Qu'un episode existe est banal ; qu'il n'existe PAS chez le
candidat retenu est un signal fort d'erreur d'identification."""

AGREEMENT_BONUS = {0: -0.10, 1: 0.0, 2: 0.12, 3: 0.18}
"""Progression non lineaire, et c'est le point important : deux sources
INDEPENDANTES qui convergent valent bien plus que le double d'une seule. Passer
de 2 a 3 apporte moins que passer de 1 a 2, parce que l'essentiel de
l'information est dans le fait meme qu'une seconde source confirme."""

W_POPULARITY = 0.05
"""Departage les homonymes — entre deux films du meme titre, le connu est plus
probable. Poids volontairement minuscule : c'est un argument de dernier
recours, pas une preuve."""

W_CONTAINER = 0.08
"""Titre lu dans les tags du conteneur. Modeste mais precieux car DECORRELE du
nom de fichier : quand les deux concordent, ce n'est pas une redondance."""

AMBIGUITY_CEILING = 0.75
"""Plafond en cas d'homonymie non tranchee. Un plafond et non un malus : deux
candidats a egalite constituent une incertitude structurelle qu'aucun autre
signal ne compense. Accumuler des bonus ailleurs ne doit pas permettre de
franchir le seuil d'application automatique."""


def compute_score(signals: Signals) -> float:
    """Combine les signaux en une confiance unique, entre 0.0 et 1.0.

    Trois etages, dans cet ordre :

    1. **Une base** de preuves positives ponderees (titre, lisibilite du nom,
       notoriete, tags du conteneur).
    2. **Des modificateurs** signes, qui confirment ou contredisent. Les
       contradictions pesent plus lourd que les confirmations : trouver
       l'annee juste est ordinaire, trouver une annee fausse est alarmant.
    3. **Des plafonds**, pour les incertitudes qu'aucune accumulation de bonus
       ne doit pouvoir effacer.

    Version de depart a ajuster. Les poids sont nommes juste au-dessus.
    """
    score = (
        BASE_CREDIT
        + W_TITLE * signals.title_similarity
        + W_PARSE_QUALITY * signals.parse_quality
        + W_POPULARITY * signals.provider_popularity
    )

    if signals.container_title_similarity is not None:
        score += W_CONTAINER * signals.container_title_similarity

    if signals.year_match is True:
        score += BONUS_YEAR
    elif signals.year_match is False:
        score += MALUS_YEAR

    if signals.episode_match is True:
        score += BONUS_EPISODE
    elif signals.episode_match is False:
        score += MALUS_EPISODE

    score += AGREEMENT_BONUS.get(min(signals.provider_agreement, 3), 0.0)

    # --- Plafonds ---

    if signals.ambiguous_candidates > 1:
        score = min(score, AMBIGUITY_CEILING)

    if signals.ai_confidence is not None:
        # Plafond et non addition : la confiance de l'IA n'est pas
        # commensurable avec les autres signaux. Elle dit « je crois
        # reconnaitre cette oeuvre », pas « les donnees concordent ». Un modele
        # hesitant doit donc borner le resultat, pas s'y ajouter.
        score = min(score, signals.ai_confidence)

    return max(0.0, min(1.0, score))


def decide(score: float, signals: Signals, policy: Policy) -> Decision:
    """Traduit un score en action.

    Deliberement separe de ``compute_score`` : la mesure et la politique sont
    deux choses distinctes. On peut durcir les seuils sans retoucher au calcul,
    et rejouer d'anciens scores sous une nouvelle politique.
    """
    # Un identifiant declare qui pointe AILLEURS disqualifie, quel que soit le
    # score : la ressemblance des titres ne peut pas l'emporter sur une
    # identite explicitement contredite.
    if signals.external_id_match is False and policy.trust_external_ids:
        return Decision.REJECT

    # A l'inverse, un identifiant concordant est une reponse, pas un indice.
    # Court-circuiter le score ici evite qu'un titre exotique ou une duree
    # atypique fasse douter d'une certitude.
    if signals.external_id_match is True and policy.trust_external_ids:
        return Decision.AUTO

    if signals.ai_confidence is not None and not policy.trust_ai:
        return Decision.REVIEW if score >= policy.reject_threshold else Decision.REJECT

    # Un « film » de 22 minutes est un episode mal classe : la duree contredit
    # frontalement le type retenu, on ne l'applique pas sans regard humain.
    if signals.runtime_plausible is False and score >= policy.auto_apply_threshold:
        return Decision.REVIEW

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

    if signals.external_id_match is True:
        lines.append("identifiant declare (.nfo ou tags) concordant — certitude")
    elif signals.external_id_match is False:
        lines.append("identifiant declare pointant vers une AUTRE oeuvre")

    if signals.runtime_plausible is False:
        lines.append("duree du fichier incompatible avec le type retenu")

    if signals.container_title_similarity is not None:
        lines.append(f"titre du conteneur : {signals.container_title_similarity:.2f}")

    if signals.ai_confidence is not None:
        lines.append(f"identification assistee par IA (confiance {signals.ai_confidence:.2f})")

    return lines
