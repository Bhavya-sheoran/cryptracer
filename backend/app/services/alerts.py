"""Real-time alerting: Redis Streams -> WebSocket fan-out.

Redis Streams rather than Kafka - see the README's assumptions. The publish path
is behind `publish_alert()` and the consume path behind `read_alerts()`, so
swapping in Kafka is a change to this module, not to its callers.

Why a stream and not a plain pub/sub channel: a stream is durable and
replayable. An alert raised while no investigator has the dashboard open must
still be there when someone opens it, and `XRANGE` gives the backlog for free.
Pub/sub would drop it.

The alert is also mirrored into Postgres, because the stream is capped and the
case file needs a permanent record.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.db.redis_client import ALERT_STREAM, get_client
from app.models import Alert

logger = logging.getLogger(__name__)

# Single source of truth for the stream key lives in app.db.redis_client.
STREAM_KEY = ALERT_STREAM
# Cap the stream so a long-running demo cannot grow it without bound. Postgres
# holds the permanent record.
STREAM_MAXLEN = 1000

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2}


def should_alert(risk_label: str, minimum: str = "medium") -> bool:
    """Only Medium and High resolutions raise an alert.

    Alerting on every Low would train investigators to ignore the feed, which is
    worse than not alerting at all.
    """
    return SEVERITY_ORDER.get(risk_label, -1) >= SEVERITY_ORDER.get(minimum, 1)


def build_message(entity_name: str | None, risk_label: str, score: float, case_number: str) -> str:
    where = entity_name or "an unattributed cluster"
    return (
        f"{risk_label.upper()} risk: funds from {case_number} trace to {where} "
        f"(fraud-linkage score {score:.1f}/100)."
    )


def publish_alert(
    db: Session | None,
    *,
    severity: str,
    message: str,
    case_id: str | None = None,
    case_number: str | None = None,
    entity_name: str | None = None,
    risk_score: float | None = None,
    address: str | None = None,
    persist: bool = True,
) -> dict:
    """Publish to the Redis stream and mirror into Postgres.

    A Redis outage must not lose the case record, so the database write happens
    even if the stream publish fails.
    """
    event = {
        "id": str(uuid.uuid4()),
        "severity": severity,
        "message": message,
        "case_id": case_id or "",
        "case_number": case_number or "",
        "entity_name": entity_name or "",
        "risk_score": "" if risk_score is None else f"{risk_score:.2f}",
        "address": address or "",
        "created_at": datetime.now(UTC).isoformat(),
        # Consumed by the dashboard banner; alerts are about synthetic data in
        # DEMO_MODE and must never imply otherwise.
        "is_synthetic": "true",
    }

    stream_id = None
    try:
        stream_id = get_client().xadd(STREAM_KEY, event, maxlen=STREAM_MAXLEN, approximate=True)
        event["stream_msg_id"] = stream_id
    except Exception as exc:  # noqa: BLE001 - alerting must not break intake
        logger.warning("alert publish to redis failed (continuing): %s", exc)

    if persist and db is not None:
        try:
            db.add(
                Alert(
                    case_id=uuid.UUID(case_id) if case_id else None,
                    severity=severity,
                    message=message,
                    stream_msg_id=stream_id,
                )
            )
            db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert persist failed: %s", exc)
            db.rollback()

    return event


def read_alerts(last_id: str = "0-0", count: int = 50) -> list[dict]:
    """The most recent `count` alerts, newest first.

    XRANGE walks the stream from the oldest entry, so `xrange(count=N)` returns
    the FIRST N alerts ever recorded - which, on a stream that has passed N
    entries, never changes and never includes anything new. XREVRANGE walks
    backwards from the newest, which is what "recent" has to mean for both the
    dashboard backlog and the polling fallback.
    """
    try:
        entries = get_client().xrevrange(STREAM_KEY, max="+", min="-", count=count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("alert read failed: %s", exc)
        return []
    return [{**fields, "stream_msg_id": msg_id} for msg_id, fields in entries]


async def stream_alerts(last_id: str = "$", block_ms: int = 2000):
    """Async generator yielding new alerts as they arrive.

    '$' means "only what arrives from now on"; the WebSocket sends the backlog
    separately on connect so a reconnecting client is not spammed with history
    it already displayed.

    The redis client here is synchronous, and `xread(block=...)` parks the
    calling thread until a message arrives or the timeout expires. Calling it
    directly from an async endpoint would block the event loop for the whole
    interval - stalling every other request on that worker and starving the very
    socket we are feeding. `asyncio.to_thread` moves the blocking wait off the
    loop, so the server stays responsive while a client sits idle on the stream.
    """
    redis = get_client()
    cursor = last_id
    while True:
        try:
            response = await asyncio.to_thread(
                redis.xread, {STREAM_KEY: cursor}, 10, block_ms
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("alert stream read failed: %s", exc)
            return
        if not response:
            yield None  # lets the caller send a keepalive
            continue
        for _stream, entries in response:
            for msg_id, fields in entries:
                cursor = msg_id
                yield {**fields, "stream_msg_id": msg_id}


def alert_json(event: dict) -> str:
    return json.dumps(event, default=str)
