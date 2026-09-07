"""Connexion, deconnexion, etat de session."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..config import get_settings
from ..core.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    LoginThrottle,
    check_password,
    issue_session,
    verify_session,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["authentification"])

_throttle = LoginThrottle()


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginRequest, response: Response) -> dict[str, object]:
    conf = get_settings()

    if (wait := _throttle.remaining_lockout()) > 0:
        # 429 et non 401 : le probleme n'est plus le mot de passe.
        raise HTTPException(
            status_code=429,
            detail=f"Trop de tentatives. Réessaie dans {wait} secondes.",
        )

    if not check_password(body.password, conf.admin_password):
        _throttle.record_failure()
        logger.warning("tentative de connexion refusee")
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")

    _throttle.record_success()
    response.set_cookie(
        SESSION_COOKIE,
        issue_session(conf.secret_key),
        max_age=SESSION_MAX_AGE,
        httponly=True,  # inaccessible au JavaScript : limite le vol par XSS
        samesite="lax",  # bloque l'envoi depuis un site tiers
        # `secure` reste a False : l'application tourne le plus souvent en HTTP
        # sur un LAN. Derriere un reverse proxy TLS, le proxy peut le forcer.
        secure=False,
        path="/",
    )
    return {"authenticated": True}


@router.post("/logout")
def logout(response: Response) -> dict[str, object]:
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"authenticated": False}


@router.get("/me")
def me(request: Request) -> dict[str, object]:
    conf = get_settings()
    return {"authenticated": verify_session(request.cookies.get(SESSION_COOKIE), conf.secret_key)}
