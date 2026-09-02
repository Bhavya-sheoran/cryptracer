"""WebSocket alert channel and the alert history endpoint.

The dashboard subscribes here and receives alerts as they are raised, rather
than discovering them on the next page refresh. Redis Streams is the transport;
this endpoint is the bridge from stream to socket.

On connect the client gets the recent backlog, then live events. That matters
because an alert raised while nobody had the dashboard open still needs to be
seen - a socket carrying only future events would silently lose it.

**Both routes require an authenticated officer.** Alert messages name case
numbers and the exchange a case resolved to, which is investigation detail, not
public information.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.postgres import SessionLocal, get_db
from app.deps import get_current_user
from app.models import User
from app.services import alerts as alerts_svc
from app.services.auth import AuthError, decode_token, get_user_by_id

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(tags=["alerts"])


def _authenticate_socket(token: str | None) -> User | None:
    """Resolve the officer behind a socket connection.

    A browser WebSocket cannot set an Authorization header, so the token arrives
    as a query parameter. It is verified exactly as the HTTP dependency does,
    and the user is re-read from the database so a revoked account cannot keep a
    socket alive on an old token.
    """
    if not token:
        return None
    try:
        payload = decode_token(token)
    except AuthError:
        return None
    db: Session = SessionLocal()
    try:
        user = get_user_by_id(db, payload.get("sub"))
        return user if user and user.is_active else None
    finally:
        db.close()


@router.websocket("/ws/alerts")
async def alert_socket(
    websocket: WebSocket,
    backlog: int = Query(20, ge=0, le=200),
    token: str | None = Query(None, description="Bearer token; browsers cannot set headers here"),
):
    user = _authenticate_socket(token)
    if user is None:
        # Close before accepting: an unauthenticated client never sees a frame.
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION, reason="authentication required"
        )
        return

    await websocket.accept()
    try:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "hello",
                    "officer": user.username,
                    "data_source": "synthetic" if settings.demo_mode else "live_indexer_apis",
                    "notice": (
                        "Alerts describe synthetic demonstration data."
                        if settings.demo_mode
                        else "Alerts describe live indexer data."
                    ),
                }
            )
        )

        # Backlog first, so a freshly opened dashboard is not blank.
        if backlog:
            # read_alerts returns newest-first; replay oldest-first so the
            # client sees them in the order they actually happened.
            for event in reversed(alerts_svc.read_alerts("0-0", count=backlog)):
                await websocket.send_text(json.dumps({"type": "alert", "replay": True, **event}))

        # `$` = only what arrives from now on; the backlog above covered history.
        async for event in alerts_svc.stream_alerts(last_id="$", block_ms=2000):
            if event is None:
                # Idle tick. A ping keeps intermediaries from reaping the socket
                # and lets the client notice a dead connection.
                await websocket.send_text(json.dumps({"type": "ping"}))
                continue
            await websocket.send_text(json.dumps({"type": "alert", "replay": False, **event}))

    except WebSocketDisconnect:
        logger.debug("alert socket disconnected")
    except (asyncio.CancelledError, RuntimeError):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert socket error: %s", exc)
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/alerts/recent", tags=["alerts"])
def recent_alerts(
    limit: int = Query(50, ge=1, le=200),
    _db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Polling fallback for clients that cannot hold a WebSocket open.

    Authenticated: alert messages name case numbers and destination exchanges.
    """
    return {
        "alerts": alerts_svc.read_alerts("0-0", count=limit),
        "transport": "redis-streams",
        "stream": alerts_svc.STREAM_KEY,
    }
