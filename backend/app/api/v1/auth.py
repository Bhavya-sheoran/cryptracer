"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db.postgres import get_db
from app.deps import get_current_user
from app.models import User
from app.services.auth import AuthError, authenticate, create_access_token, seed_demo_users

router = APIRouter(prefix="/auth", tags=["auth"])


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
    """
    created = seed_demo_users(db)
    return {
        "created": created,
        "accounts": [
            {"username": "investigator", "password": "investigator123", "role": "investigator"},
            {"username": "supervisor", "password": "supervisor123", "role": "supervisor"},
        ],
        "notice": "Demonstration credentials. Replace before any real deployment.",
    }
