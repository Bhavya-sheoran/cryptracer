"""Authentication endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.postgres import get_db
from app.deps import get_current_user, oauth2_scheme
from app.models import User
from app.services.auth import (
    AuthError,
    authenticate,
    create_access_token,
    decode_token,
    seed_demo_users,
)
from app.services.sessions import revoke_all_for_user, revoke_token

router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()
logger = logging.getLogger(__name__)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    role: str
    full_name: str


class UserResponse(BaseModel):
    id: str
    username: str
    full_name: str
    role: str
    can_approve: bool


@router.post("/login", response_model=TokenResponse)
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    try:
        user = authenticate(db, form.username, form.password)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    return TokenResponse(
        access_token=create_access_token(user),
        username=user.username,
        role=user.role,
        full_name=user.full_name,
    )


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(get_current_user)):
    from app.services.auth import can_approve

    return UserResponse(
        id=str(user.id),
        username=user.username,
        full_name=user.full_name,
        role=user.role,
        can_approve=can_approve(user),
    )


@router.post("/seed-demo-users")
def seed(db: Session = Depends(get_db)):
    """Create the demonstration accounts.

    Published credentials for a demo only - see app/services/auth.py. Not a
    security model for any real deployment.

    Gated on `demo_auth_enabled`, which requires ALLOW_DEMO_AUTH *and*
    DEMO_MODE. Disabled it answers 404 rather than 403: a 403 confirms the
    endpoint exists and is worth attacking, while a 404 is indistinguishable
    from a build that never had it.
    """
    if not settings.demo_auth_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    created = seed_demo_users(db)
    return {
        "created": created,
        "accounts": [
            {"username": "investigator", "password": "investigator123", "role": "investigator"},
            {"username": "supervisor", "password": "supervisor123", "role": "supervisor"},
        ],
        "notice": "Demonstration credentials. Replace before any real deployment.",
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    token: str | None = Depends(oauth2_scheme),
    user: User = Depends(get_current_user),
):
    """End this session immediately.

    Revokes the presented token by its `jti`, so the officer's other sessions -
    a second browser, a phone - are untouched. Signing out on a shared machine
    should not sign you out everywhere.

    Idempotent: revoking an already-revoked token is a no-op, so a double click
    is not an error.
    """
    payload = decode_token(token)
    revoke_token(payload["jti"], payload["exp"])
    logger.info("officer signed out", extra={"username": user.username})


@router.post("/logout-everywhere", status_code=status.HTTP_204_NO_CONTENT)
def logout_everywhere(user: User = Depends(get_current_user)):
    """End every session this officer holds.

    The response to a suspected compromise: a token you cannot see cannot be
    revoked by id, so this records a cutoff and refuses anything issued before
    it. Includes the session making the request.
    """
    revoke_all_for_user(str(user.id))
    logger.warning(
        "all sessions revoked", extra={"username": user.username, "user_id": str(user.id)}
    )
