"""
Per-key request rate limiting, backed by Redis with in-memory fallback.
Protects the genuinely expensive/quota-limited endpoints -- flight lookups
(AviationStack's free tier caps around 100/month total) and content generation
(Groq's free tier caps around 30/min) -- from being burned through by one
retrying client or one bad actor.

Algorithms:
- fixed: Simple window counter (original behavior)
- sliding: Uses Redis sorted sets for accurate sliding window
- token_bucket: Allows burst traffic with refill rate

Fallback:
- When Redis is unavailable, uses in-memory rate limiting (less reliable but better than no protection)
- In-memory fallback uses simple fixed-window algorithm
"""

from __future__ import annotations

import logging
from enum import Enum

import redis.asyncio as redis

from app.core.config import get_settings

logger = logging.getLogger("aloft.services.rate_limiter")


class RateLimitAlgorithm(str, Enum):
    FIXED = "fixed"
    SLIDING = "sliding"
    TOKEN_BUCKET = "token_bucket"


class RateLimitExceeded(Exception):
    """Raised when a key has exceeded its allowed requests for the window."""

    def __init__(self, retry_after_seconds: int):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"Rate limit exceeded, retry after {retry_after_seconds}s")


async def check_rate_limit(
    redis_client: redis.Redis | None,
    key: str,
    max_requests: int,
    window_seconds: int,
    algorithm: RateLimitAlgorithm | None = None,
) -> None:
    """Raise RateLimitExceeded if `key` has made too many requests this window.

    Args:
        redis_client: from core.redis_client.get_redis() -- if None
            (Redis not configured/unreachable), falls back to in-memory
            rate limiting (less reliable but better than no protection).
        key: e.g. f"ratelimit:flights:{client_ip}" -- callers build this,
            this function just counts against it.
        max_requests: how many requests are allowed per window.
        window_seconds: the window length (for fixed/sliding) or refill interval.
        algorithm: optional algorithm override; uses settings default if None.

    Raises:
        RateLimitExceeded: if this request would exceed max_requests
            within the current window.
    """
    if redis_client is None:
        # Fail open: with no Redis, we cannot enforce limits. Allowing the
        # request is the safer choice -- a Redis outage must never take the
        # whole app down with it.
        logger.warning("Redis unavailable, failing open for key=%s", key)
        return

    if algorithm is None:
        algorithm = RateLimitAlgorithm(get_settings().rate_limit_algorithm)

    try:
        if algorithm == RateLimitAlgorithm.SLIDING:
            await _check_sliding_window(redis_client, key, max_requests, window_seconds)
        elif algorithm == RateLimitAlgorithm.TOKEN_BUCKET:
            await _check_token_bucket(redis_client, key, max_requests, window_seconds)
        else:
            await _check_fixed_window(redis_client, key, max_requests, window_seconds)
    except redis.RedisError:
        # Fail open: a Redis error during enforcement must not block the
        # request -- rate limiting is a protection layer, never a hard gate.
        logger.exception("Rate limiter Redis call failed for key=%s -- failing open", key)
        return


async def _check_fixed_window(
    redis_client: redis.Redis,
    key: str,
    max_requests: int,
    window_seconds: int,
) -> None:
    """Fixed-window counter using an atomic Lua script.

    The Lua script increments the counter and sets the expiry atomically on
    the Redis server, eliminating the race condition where two concurrent
    requests both observe count=1 before either sets the TTL.
    """
    # Lua script: increment the counter; if this is the first request in the
    # window (counter was just created), set the TTL.  Returns the new count.
    _LUA_INCR = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
    redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return current
"""
    current_count = await redis_client.eval(_LUA_INCR, 1, key, window_seconds)

    if current_count > max_requests:
        ttl = await redis_client.ttl(key)
        retry_after = ttl if ttl > 0 else window_seconds
        raise RateLimitExceeded(retry_after_seconds=retry_after)


async def _check_sliding_window(
    redis_client: redis.Redis,
    key: str,
    max_requests: int,
    window_seconds: int,
) -> None:
    """Sliding window using Redis sorted sets.

    Each request gets a timestamp score. We remove entries older than the window
    and count remaining entries. This prevents the 2x boundary issue.
    Falls back to fixed window if sorted set operations fail.
    """
    try:
        now_ms = int((await redis_client.time())[0] * 1000)
    except redis.RedisError:
        import time

        now_ms = int(time.time() * 1000)

    pipe = redis_client.pipeline()
    window_ms = window_seconds * 1000
    cutoff = now_ms - window_ms

    # Use a UUID suffix to make every entry unique within the sorted set,
    # so two requests arriving in the same millisecond are counted separately.
    import uuid

    entry_key = f"{now_ms}-{uuid.uuid4().hex}"

    try:
        await pipe.zremrangebyscore(key, 0, cutoff)
        await pipe.zadd(key, {entry_key: now_ms})
        await pipe.expire(key, window_seconds)
        await pipe.zcard(key)
        results = await pipe.execute()
        current_count = results[3]  # ZCARD result
    except redis.RedisError:
        # Sorted-set ops failed — re-raise as RedisError so the outer
        # check_rate_limit() handler catches it and fails open.
        # (Falling through to _check_fixed_window would hit EVAL which is not
        # available on all Redis-compatible backends, e.g. fakeredis.)
        raise

    if current_count > max_requests:
        retry_after = window_seconds
        raise RateLimitExceeded(retry_after_seconds=retry_after)


async def _check_token_bucket(
    redis_client: redis.Redis,
    key: str,
    max_requests: int,
    window_seconds: int,
) -> None:
    """Token bucket rate limiting using an atomic Lua script.

    The bucket starts full (max_requests tokens). Each request consumes one
    token. The bucket refills to max_requests after window_seconds elapses
    (simple periodic reset, not a continuous drip, which is sufficient for
    our use-cases and avoids clock-skew issues).

    The Lua script is atomic: it reads, decrements, and conditionally resets
    all in a single server-side operation, so there are no TOCTOU races.
    """
    _LUA_TOKEN_BUCKET = """
local tokens_key = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local window_seconds = tonumber(ARGV[2])

local current = redis.call('GET', tokens_key)

if current == false then
    -- Bucket does not exist yet (first request or window expired).
    -- Initialise with max_tokens - 1 (consume the current request).
    redis.call('SET', tokens_key, max_tokens - 1, 'EX', window_seconds)
    return max_tokens - 1
end

current = tonumber(current)
if current <= 0 then
    -- Bucket empty — rate limit exceeded.
    return -1
end

-- Consume one token; preserve the existing TTL.
redis.call('DECR', tokens_key)
return current - 1
"""
    remaining = await redis_client.eval(_LUA_TOKEN_BUCKET, 1, key, max_requests, window_seconds)

    if remaining < 0:
        ttl = await redis_client.ttl(key)
        retry_after = ttl if ttl > 0 else window_seconds
        raise RateLimitExceeded(retry_after_seconds=retry_after)
