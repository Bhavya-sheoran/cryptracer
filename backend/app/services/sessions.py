"""Token revocation.

A JWT is valid until it expires. That is the point of them - they are verified
by signature, with no lookup - and it is also the problem: an officer's session
lasts eight hours, and until now nothing could end one early. A stolen token, a
laptop left open, an officer dismissed at 10am: all of it stayed valid until
6pm, and the only remedy was rotating the signing key and logging everybody out.

Two mechanisms, because there are two different questions.

  * **Revoke one token** - "this session ends now". Keyed on the token's `jti`.
    Used by sign-out.
  * **Revoke every token a user holds** - "this person's access ends now".
    Keyed on the user id with a cutoff timestamp: any token issued before it is
    refused. Used when an account is disabled or a password is reset, where
    hunting down individual jtis is both impossible and the wrong shape.

Both live in Redis with a TTL matched to the token lifetime, so the store never
grows without bound - a revocation stops being interesting the moment the token
it refers to would have expired anyway.

Redis being unreachable fails CLOSED for revocation checks: if we cannot
confirm a token is still valid, it is refused. That is the opposite of the rate
limiter's choice, and deliberately so. A limiter that fails open lets some
extra traffic through; a revocation check that fails open silently re-admits
every session someone has explicitly terminated.
"""

from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

REVOKED_TOKEN_PREFIX = "sih183:revoked:"
USER_CUTOFF_PREFIX = "sih183:session_cutoff:"

#: Ceiling on how long a revocation record is kept. Matched to the longest
#: token lifetime; beyond that the token is expired on its own terms.
MAX_TTL_SECONDS = 24 * 3600


class RevocationUnavailable(RuntimeError):
    """Redis could not be reached, so revocation state is unknown."""


def _client():
    from app.db import redis_client

    return redis_client.get_client()


def revoke_token(jti: str, expires_at: int) -> None:
    """End one session.

    TTL is the token's own remaining life: once it would have expired anyway,
    remembering that it was revoked serves no purpose.
    """
    ttl = max(1, min(int(expires_at - time.time()), MAX_TTL_SECONDS))
    _client().setex(f"{REVOKED_TOKEN_PREFIX}{jti}", ttl, "1")
    logger.info("token revoked", extra={"jti": jti, "ttl_seconds": ttl})


def revoke_all_for_user(user_id: str) -> None:
    """End every session this user currently holds.

    Recorded as a cutoff timestamp rather than by enumerating tokens: we do not
    know which tokens exist, and could not enumerate them if we did. Any token
    whose `iat` predates the cutoff is refused.
    """
    cutoff = int(time.time())
    _client().setex(f"{USER_CUTOFF_PREFIX}{user_id}", MAX_TTL_SECONDS, str(cutoff))
    logger.info("all sessions revoked for user", extra={"user_id": user_id})


def is_revoked(jti: str | None, user_id: str | None, issued_at: int | None) -> bool:
    """Whether this token has been revoked, individually or with its user.

    Raises RevocationUnavailable when Redis cannot be reached. The caller must
    treat that as "refuse the request": a revocation system that cannot be
    consulted has to be assumed to have something to say, or terminating a
    session becomes advisory.
    """
    try:
        client = _client()

        if jti and client.exists(f"{REVOKED_TOKEN_PREFIX}{jti}"):
            return True

        if user_id and issued_at is not None:
            cutoff = client.get(f"{USER_CUTOFF_PREFIX}{user_id}")
            # `<=`, not `<`. JWT `iat` is integer seconds (RFC 7519), so a
            # token minted in the same second as the revocation compares equal
            # and a strict `<` let it survive - which is exactly the token an
            # attacker who has just signed in would be holding.
            #
            # The cost is that a legitimate sign-in within the same second is
            # also refused. That is the right direction to err: the officer
            # retries a second later, whereas the other way round the
            # revocation silently does not apply.
            if cutoff is not None and issued_at <= int(cutoff):
                return True

        return False
    except RevocationUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - any Redis fault means "unknown"
        logger.error("revocation check failed, refusing token: %s", exc)
        raise RevocationUnavailable(str(exc)) from exc


def clear_user_cutoff(user_id: str) -> None:
    """Lift a blanket revocation. Used when re-enabling an account."""
    _client().delete(f"{USER_CUTOFF_PREFIX}{user_id}")
