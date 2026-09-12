"""
Compatibility shim — delegates to core/redis.py.

This module previously maintained a *second* Redis connection for rate
limiting and security monitoring.  All Redis state now flows through the
single shared connection in core/redis.py, halving the connection count
to the Redis server.

Callers that imported ``get_redis`` from here for *optional* (fail-open)
access should migrate to importing ``get_optional_redis`` from core/redis.
This shim preserves the old ``get_redis() -> Redis | None`` signature so
no call-sites need to change immediately.
"""

from __future__ import annotations

import logging

# Re-export lifecycle helpers so any code that imports from redis_client still
# works without modification.  Both point to the same underlying connection.
from app.core.redis import (
    close_redis_connection,
    connect_to_redis,
    get_optional_redis,
)

logger = logging.getLogger("aloft.redis_client")


def get_redis():
    """Return the shared Redis client, or None if unavailable.

    Preserves the original redis_client.get_redis() -> Redis | None contract
    so callers don't need to be updated immediately.
    """
    return get_optional_redis()


__all__ = [
    "connect_to_redis",
    "close_redis_connection",
    "get_redis",
    "get_optional_redis",
]
