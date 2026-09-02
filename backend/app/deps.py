"""Shared FastAPI dependencies: current user, role guards, audit logging."""

from __future__ import annotations

import ipaddress
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.db.postgres import get_db
from app.models import AuditLog, User
from app.services.auth import APPROVER_ROLES, AuthError, decode_token, get_user_by_id

logger = logging.getLogger(__name__)

# auto_error=False so endpoints can distinguish "no credentials" from "bad
# credentials" and return a useful message either way.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str | None = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> User:
    if not token:
        raise _UNAUTHENTICATED
    try:
        payload = decode_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = get_user_by_id(db, payload.get("sub"))
    if user is None or not user.is_active:
        raise _UNAUTHENTICATED

    # The role is re-read from the database rather than trusted from the token,
    # so revoking a role takes effect immediately instead of when the token
    # happens to expire.
    return user


def require_approver(user: User = Depends(get_current_user)) -> User:
    """Gate for actions that must be authorised by a supervisor.

    Used by the freeze-request and STR approval endpoints. This is the technical
    expression of the human-in-the-loop rule: there is no code path that
    approves either without passing through here.
    """
    if user.role not in APPROVER_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Role '{user.role}' cannot approve this action. "
                f"Requires one of: {', '.join(sorted(APPROVER_ROLES))}."
            ),
        )
    return user


def _client_ip(request: Request | None) -> str | None:
    """Extract a genuine IP address, or None.

    `request.client.host` is not guaranteed to be an IP - it is "testclient"
    under Starlette's TestClient, and can be a hostname behind some proxies.
    audit_log.ip_address is an INET column, so a non-IP value raises a DataError
    which, sharing the request transaction, would roll back and fail the very
    action being audited. Recording no IP is far better than losing the action.
    """
    if request is None or request.client is None:
        return None
    host = request.client.host
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        logger.debug("audit: client host %r is not an IP address; storing NULL", host)
        return None


def record_audit(
    db: Session,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: str | None = None,
    payload: dict | None = None,
    request: Request | None = None,
) -> None:
    """Append to the audit log. Every approval and export goes through here."""
    client_ip = _client_ip(request)
    db.add(
        AuditLog(
            actor_id=actor.id if actor else None,
            action=action,
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            payload=payload or {},
            ip_address=client_ip,
        )
    )
