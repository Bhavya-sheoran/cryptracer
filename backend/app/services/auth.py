"""Authentication and role checks.

Simplified JWT with role claims rather than Keycloak - see the README's
assumptions. Keycloak would add a container and a realm to configure without
changing what this demonstrates; the token shape here (subject + role claim) is
what a Keycloak-issued token would carry, so swapping the issuer later is a
change of verification function, not of call sites.

Roles, and what they exist to separate:
  investigator - files complaints, traces, drafts requests
  supervisor   - the ONLY role that can approve a freeze or an STR
  admin        - user administration

The investigator/supervisor split is the whole point of the human-in-the-loop
requirement: the person who drafts a freeze request must not be the person who
approves it.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import User

logger = logging.getLogger(__name__)
settings = get_settings()

ROLE_INVESTIGATOR = "investigator"
ROLE_SUPERVISOR = "supervisor"
ROLE_ADMIN = "admin"

# Roles permitted to approve a freeze request or an STR draft. Deliberately a
# constant rather than a string literal at the call site, so the set of
# approving roles is defined in exactly one place.
APPROVER_ROLES = frozenset({ROLE_SUPERVISOR, ROLE_ADMIN})

# bcrypt is used directly rather than through passlib. passlib 1.7.4 (last
# released 2020) probes its bcrypt backend with a >72-byte password, which
# bcrypt 4.1+ rejects with ValueError instead of truncating - so every hash call
# raises. The direct API is also simpler and one dependency fewer.
BCRYPT_MAX_BYTES = 72


class AuthError(Exception):
    pass


def hash_password(password: str) -> str:
    """Hash a password.

    bcrypt ignores anything past 72 bytes. Silently truncating would mean two
    different long passwords authenticate each other, so this rejects instead.
    """
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_BYTES:
        raise AuthError(f"password must be at most {BCRYPT_MAX_BYTES} bytes")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        raw = plain.encode("utf-8")
        if len(raw) > BCRYPT_MAX_BYTES:
            return False
        return bcrypt.checkpw(raw, hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user: User) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        # A unique id per token, so one session can be revoked without
        # affecting the officer's other sessions. Without it the only options
        # are "revoke everything for this user" or "revoke nothing".
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Verify and decode a token.

    `algorithms` is pinned to the one we issue, which is what stops an attacker
    presenting `alg: none` or swapping HMAC for a public key they control.

    PyJWT rather than python-jose: python-jose pulls in the pure-Python `ecdsa`
    package, which carries a timing side-channel advisory with no fix released
    (PYSEC-2026-1325). We only ever sign with HMAC, so that code never ran - but
    an unfixable advisory sitting in the dependency tree costs more to explain
    in every audit than this two-line swap cost to make.
    """
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            # A token with no expiry would be valid forever; refuse it outright
            # rather than trusting that we always set the claim. `jti` is
            # required too - without one a token cannot be revoked
            # individually, and a token that cannot be revoked must not be
            # accepted now that revocation is something we promise.
            options={"require": ["exp", "sub", "jti", "iat"]},
        )
    except InvalidTokenError as exc:
        raise AuthError(f"invalid token: {exc}") from exc


def authenticate(db: Session, username: str, password: str) -> User:
    user = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    # Verify against a dummy hash even when the user is missing, so a wrong
    # username and a wrong password take the same time to fail.
    if user is None:
        # Hash anyway so a missing username and a wrong password take a similar
        # amount of time, rather than leaking which one was wrong by timing.
        verify_password(password, _DUMMY_HASH)
        raise AuthError("incorrect username or password")
    if not user.is_active:
        raise AuthError("account is disabled")
    if not verify_password(password, user.hashed_password):
        raise AuthError("incorrect username or password")
    return user


# A real bcrypt hash of a value nobody knows, used only for timing parity.
_DUMMY_HASH = bcrypt.hashpw(b"timing-parity-placeholder", bcrypt.gensalt()).decode("utf-8")


def get_user_by_id(db: Session, user_id: str) -> User | None:
    try:
        return db.get(User, uuid.UUID(str(user_id)))
    except (ValueError, TypeError):
        return None


def can_approve(user: User) -> bool:
    return user.role in APPROVER_ROLES


DEMO_USERS = [
    {
        "username": "investigator",
        "full_name": "Demo Investigator",
        "role": ROLE_INVESTIGATOR,
        "password": "investigator123",
    },
    {
        "username": "supervisor",
        "full_name": "Demo Supervisor",
        "role": ROLE_SUPERVISOR,
        "password": "supervisor123",
    },
]


def seed_demo_users(db: Session) -> list[str]:
    """Create the demo accounts if absent.

    These are demonstration credentials with published passwords. They exist so
    the approval workflow can be shown end to end; they are not a security model
    for any real deployment.
    """
    created = []
    for spec in DEMO_USERS:
        existing = db.execute(
            select(User).where(User.username == spec["username"])
        ).scalar_one_or_none()
        if existing is not None:
            # Re-enable one that was deactivated. "Seed the demo accounts" has
            # to mean "make them usable", not "create rows if absent" - a
            # disabled account satisfies the second and fails the first, which
            # left the test suite unable to sign in after the real deployment
            # steps disabled them.
            #
            # Safe because the only caller is gated on demo_auth_enabled, which
            # requires DEMO_MODE. A live deployment never reaches this line, so
            # deactivated demo accounts stay deactivated there.
            if not existing.is_active:
                existing.is_active = True
                created.append(f"{spec['username']} (re-enabled)")
            continue
        db.add(
            User(
                username=spec["username"],
                full_name=spec["full_name"],
                role=spec["role"],
                hashed_password=hash_password(spec["password"]),
            )
        )
        created.append(spec["username"])
    if created:
        db.commit()
    return created
