"""
Shared Redis connection — single client instance for the whole application.

WHY ONE MODULE vs TWO?
  The codebase previously had two Redis modules (core/redis.py and
  core/redis_client.py), both connecting to the same REDIS_URL.  That doubled
  the connection count for no benefit.  They are now unified here:

  get_redis_required()  → raises RuntimeError if not connected.
    Used by sessions.py / flight_session_repository.py where Redis is
    mandatory (a session without Redis is simply broken).

  get_redis_optional()  → returns None if not connected.
    Used by rate limiting, security monitoring, password reset, etc.
    All callers must already handle None gracefully (fail-open). Returning
    None here instead of raising keeps the failure-mode separation that the
    two old modules provided, without the double-connection overhead.

Key prefixes:
  session:{uuid}       — flight session blobs (TTL = session_ttl_seconds)
  ratelimit:*          — per-key counters (TTL = rate-limit window)
  security:*           — security event metadata
  reset:{token}        — password-reset tokens (TTL 15 min)
  verify:{token}       — email-verification tokens (TTL 24 h)
"""

from __future__ import annotations

import logging

from redis.asyncio import Redis

from app.core.config import get_settings

logger = logging.getLogger("aloft.redis")

_redis: Redis | None = None
_connection_attempted: bool = False


# ---------------------------------------------------------------------------
# Lifecycle helpers (called from app lifespan in main.py)
# ---------------------------------------------------------------------------


async def connect_to_redis() -> None:
    """Open the Redis connection if REDIS_URL is configured.

    Safe to call even when redis_url is None — becomes a no-op instead of
    raising, because Redis is optional for the core API to function.
    """
    global _redis, _connection_attempted
    _connection_attempted = True

    settings = get_settings()
    if not settings.redis_url:
        logger.info("REDIS_URL not configured -- Redis features will be unavailable")
        return

    _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await _redis.ping()
        logger.info("Connected to Redis (shared client)")
    except Exception:
        logger.exception("Redis configured but unreachable -- Redis features will fail open")
        _redis = None


async def close_redis_connection() -> None:
    global _redis, _connection_attempted
    if _redis is not None:
        await _redis.aclose()
    _redis = None
    _connection_attempted = False


# ---------------------------------------------------------------------------
# Accessor functions
# ---------------------------------------------------------------------------


def get_redis() -> Redis:
    """Return the Redis client.

    Raises RuntimeError if Redis is not connected — use this where Redis is
    *required* (e.g. session storage: a session without Redis is broken).
    """
    if _redis is None:
        raise RuntimeError(
            "Redis not connected. Call connect_to_redis() at app startup first, "
            "or set REDIS_URL in your environment."
        )
    return _redis


def get_optional_redis() -> Redis | None:
    """Return the Redis client, or None if Redis is not available.

    Use this where Redis is *optional* (rate limiting, security monitoring,
    password reset tokens, etc.).  All callers must handle None gracefully
    by failing open — a Redis outage must never take the whole API down.
    """
    if not _connection_attempted:
        # connect_to_redis() was never called (e.g. in unit tests that don't
        # go through the full lifespan).  Treat as unavailable.
        return None
    return _redis
