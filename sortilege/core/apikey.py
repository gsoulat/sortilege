"""Cle d'API : ouvrir Sortilege a ce qui n'est pas un navigateur.

Le cookie de session repond a un humain devant un ecran, et a rien d'autre. Un
client de telechargement qui vient de finir un transfert, un script de nuit, un
bouton de tableau de bord domotique n'ont ni navigateur ni formulaire de
connexion : sans deuxieme porte, Sortilege ne peut etre declenche que par un
clic. C'est le manque que Radarr, Sonarr et tinyMediaManager n'ont jamais eu.

**Cette cle se LIT, contrairement a toutes les autres du produit.** La cle
TheMovieDB, celle du resolveur IA et le webhook Discord sont en ecriture seule
parce qu'ils appartiennent a un service tiers : Sortilege s'en sert, personne
n'a besoin de les relire ici. Celle-ci va dans l'autre sens — elle n'a de
valeur que recopiee ailleurs, dans une ligne de commande ou dans les reglages
d'un client de telechargement. Une cle qu'on ne peut pas lire est une cle qu'on
ne peut pas donner, donc une fonction qui n'existe pas.

Le compromis reste borne, et c'est ce qui le rend acceptable : elle sort par
une route DEDIEE, jamais dans la reponse generale des reglages — celle-la est
demandee a chaque ouverture de l'ecran et finit dans le cache du navigateur.
Et il faut deja une session pour la demander, c'est-a-dire deja tout pouvoir.

Elle vaut ensuite exactement ce que vaut le mot de passe : porteuse, elle ouvre
toute l'API. Ce modele tient parce qu'elle se regenere en un clic, la ou
changer le mot de passe impose d'editer un .env et de redemarrer la pile.
"""

from __future__ import annotations

import hmac
import secrets

HEADER = "X-Api-Key"
"""En-tete attendu. Volontairement celui de Radarr et Sonarr : les scripts et
les modeles de configuration qui circulent deja le connaissent, et personne
n'aura a inventer une convention de plus."""

PREFIX = "srtl_"
"""Prefixe reconnaissable. Une cle oubliee dans un historique de shell ou
poussee par megarde dans un depot se rattache alors a Sortilege d'un coup
d'oeil, au lieu d'etre une chaine anonyme que personne ne pense a revoquer."""

MIN_LENGTH = 24
"""Plancher a la verification, pas seulement a la generation. Une cle courte —
tronquee par un copier-coller, ou bricolee a la main dans le fichier de
preferences — serait devinable : mieux vaut la refuser que la traiter comme un
secret."""


def generate() -> str:
    """Une cle neuve. ``secrets`` et non ``random`` : ce tirage est un secret."""
    return PREFIX + secrets.token_urlsafe(32)


def verify(candidate: str | None, expected: str) -> bool:
    """Comparaison en temps constant. Toute anomalie vaut refus.

    Le cas vraiment dangereux est l'attendu vide — installation dont la cle a
    ete effacee : un `==` sur deux chaines vides aurait ouvert l'API a une
    requete sans en-tete du tout.
    """
    if not candidate or not expected or len(expected) < MIN_LENGTH:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def masked(key: str) -> str:
    """Forme journalisable : de quoi reconnaitre la cle, pas de quoi s'en servir."""
    if not key:
        return "(aucune)"
    if len(key) <= len(PREFIX) + 8:
        return f"{PREFIX}..."
    return f"{key[: len(PREFIX) + 4]}...{key[-4:]}"
