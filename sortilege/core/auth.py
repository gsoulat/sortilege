"""Authentification par mot de passe unique.

Modele volontairement minimal : un seul mot de passe, fourni par
l'environnement. Pas de comptes, pas d'inscription, pas de base d'utilisateurs
— l'application est destinee a un NAS personnel, et une gestion de comptes
serait de la surface d'attaque sans usage.

Le mot de passe n'est pas stocke : il EST la variable d'environnement. Le
hacher n'apporterait rien puisque la source de verite est deja en clair dans le
.env — cela deplacerait le probleme sans le resoudre. La comparaison est
neanmoins faite en temps constant, pour ne pas laisser fuir le prefixe correct
par la duree de la reponse.

Ce qui est signe et transmis au navigateur, en revanche, ne contient jamais le
mot de passe : seulement un jeton de session date, signe avec la cle secrete.
"""

from __future__ import annotations

import hmac
import logging
import time
from dataclasses import dataclass, field
from threading import Lock

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

logger = logging.getLogger(__name__)

SESSION_COOKIE = "sortilege_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 jours
SALT = "sortilege.session.v1"

# Limitation des tentatives : sans elle, un mot de passe faible tombe en
# quelques minutes sur un LAN. Le verrouillage est par processus et en memoire,
# ce qui suffit — l'application est mono-instance.
MAX_ATTEMPTS = 8
LOCKOUT_SECONDS = 300


@dataclass
class LoginThrottle:
    """Compteur d'echecs, avec verrouillage temporaire."""

    failures: int = 0
    locked_until: float = 0.0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def remaining_lockout(self) -> int:
        with self._lock:
            remaining = self.locked_until - time.monotonic()
            return max(0, int(remaining))

    def record_failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= MAX_ATTEMPTS:
                self.locked_until = time.monotonic() + LOCKOUT_SECONDS
                self.failures = 0
                logger.warning(
                    "trop de tentatives de connexion : verrouillage %s s", LOCKOUT_SECONDS
                )

    def record_success(self) -> None:
        with self._lock:
            self.failures = 0
            self.locked_until = 0.0


def check_password(candidate: str, expected: str) -> bool:
    """Comparaison en temps constant.

    Un `==` classique s'arrete au premier caractere different, ce qui laisse
    deduire le prefixe correct en mesurant la duree des reponses.
    """
    if not expected:
        # Aucun mot de passe configure : on refuse tout plutot que d'ouvrir.
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def make_serializer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt=SALT)


def issue_session(secret_key: str) -> str:
    """Cree un jeton de session signe et date."""
    return make_serializer(secret_key).dumps({"v": 1})


def verify_session(token: str | None, secret_key: str) -> bool:
    """Valide un jeton. Toute anomalie vaut refus."""
    if not token:
        return False
    try:
        make_serializer(secret_key).loads(token, max_age=SESSION_MAX_AGE)
    except SignatureExpired:
        return False
    except BadSignature:
        # Signature invalide : jeton forge, ou SECRET_KEY changee depuis
        # l'emission. Dans les deux cas on redemande le mot de passe.
        return False
    return True
