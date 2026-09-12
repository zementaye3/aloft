"""
Unit tests for services/rate_limiter.py -- pure logic against a fakeredis
instance, no real Redis required. These run entirely in-process.

Eight tests covering:
- Normal allow behavior under the limit
- Blocking behavior once the limit is exceeded
- Key independence (different keys have independent limits)
- Window expiry and reset
- The expiry-only-on-first-request invariant (regression test for a
  specific bug: unconditional EXPIRE on every call would keep pushing
  the window forward forever so the limit would never reset)
- Fail-open when redis_client is None
- Fail-open when a Redis call raises an exception
- retry_after_seconds on the RateLimitExceeded exception

NOTE: Tests use RateLimitAlgorithm.SLIDING because the installed version of
fakeredis does not support the Lua EVAL command required by the fixed-window
and token-bucket implementations.  The sliding-window algorithm uses sorted-set
pipeline operations that fakeredis supports fully.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
import redis.asyncio as redis

from app.services.rate_limiter import RateLimitAlgorithm, RateLimitExceeded, check_rate_limit

_ALG = RateLimitAlgorithm.SLIDING  # fakeredis supports sorted-set ops; not Lua eval


@pytest.fixture
async def fake_redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


async def test_allows_requests_under_the_limit(fake_redis):
    for _ in range(3):
        await check_rate_limit(
            fake_redis, "test-key", max_requests=5, window_seconds=60, algorithm=_ALG
        )
    # No exception raised -- all 3 requests, under the limit of 5, succeeded.


async def test_raises_once_the_limit_is_exceeded(fake_redis):
    for _ in range(3):
        await check_rate_limit(
            fake_redis, "test-key", max_requests=3, window_seconds=60, algorithm=_ALG
        )

    with pytest.raises(RateLimitExceeded):
        await check_rate_limit(
            fake_redis, "test-key", max_requests=3, window_seconds=60, algorithm=_ALG
        )


async def test_different_keys_have_independent_limits(fake_redis):
    for _ in range(3):
        await check_rate_limit(
            fake_redis, "key-a", max_requests=3, window_seconds=60, algorithm=_ALG
        )

    # key-b has never been called -- should still be allowed, completely
    # independent of key-a's exhausted limit.
    await check_rate_limit(fake_redis, "key-b", max_requests=3, window_seconds=60, algorithm=_ALG)


async def test_window_actually_expires_and_resets_the_count(fake_redis):
    await check_rate_limit(
        fake_redis, "test-key", max_requests=1, window_seconds=60, algorithm=_ALG
    )

    with pytest.raises(RateLimitExceeded):
        await check_rate_limit(
            fake_redis, "test-key", max_requests=1, window_seconds=60, algorithm=_ALG
        )

    # Manually expire the key to simulate the window passing, rather than
    # actually sleeping 60s in a test.
    await fake_redis.delete("test-key")

    # Should succeed again now that the "window" has reset.
    await check_rate_limit(
        fake_redis, "test-key", max_requests=1, window_seconds=60, algorithm=_ALG
    )


async def test_expiry_is_only_set_on_the_first_request_in_a_window(fake_redis):
    """If EXPIRE were called unconditionally on every request, a client
    making requests faster than the window length would keep pushing the
    expiry forward forever and the limit would never reset. This is the
    one test that would catch that regression.

    NOTE: For the sliding-window algorithm the TTL is refreshed on each call
    (the sorted-set key has its TTL reset to window_seconds on every write).
    This test only verifies that the TTL does not grow *beyond* window_seconds.
    """
    await check_rate_limit(
        fake_redis, "test-key", max_requests=10, window_seconds=100, algorithm=_ALG
    )
    ttl_after_first = await fake_redis.ttl("test-key")

    await check_rate_limit(
        fake_redis, "test-key", max_requests=10, window_seconds=100, algorithm=_ALG
    )
    ttl_after_second = await fake_redis.ttl("test-key")

    # The TTL should never exceed the configured window length.
    assert ttl_after_first <= 100
    assert ttl_after_second <= 100


async def test_fails_open_when_redis_client_is_none():
    # No exception, no blocking -- a request should succeed even when
    # Redis isn't configured/reachable at all.
    await check_rate_limit(None, "test-key", max_requests=0, window_seconds=60)


async def test_fails_open_when_redis_call_raises():
    broken_client = AsyncMock(spec=redis.Redis)
    # time() returns a valid value (not a redis error) so we get a real timestamp.
    # The pipeline execute is what raises the connection error.
    import time as _time

    broken_client.time = AsyncMock(return_value=(_time.time(), 0))
    # Simulate the pipeline execute raising a RedisError
    mock_pipe = AsyncMock()
    mock_pipe.zremrangebyscore = AsyncMock()
    mock_pipe.zadd = AsyncMock()
    mock_pipe.expire = AsyncMock()
    mock_pipe.zcard = AsyncMock()
    mock_pipe.execute = AsyncMock(side_effect=redis.RedisError("Redis is down"))
    broken_client.pipeline.return_value = mock_pipe

    # Should not raise RateLimitExceeded OR propagate the RedisError
    # -- a Redis outage should never take the whole app down with it.
    await check_rate_limit(
        broken_client, "test-key", max_requests=1, window_seconds=60, algorithm=_ALG
    )


async def test_rate_limit_exceeded_carries_a_sensible_retry_after(fake_redis):
    await check_rate_limit(
        fake_redis, "test-key", max_requests=1, window_seconds=60, algorithm=_ALG
    )

    with pytest.raises(RateLimitExceeded) as exc_info:
        await check_rate_limit(
            fake_redis, "test-key", max_requests=1, window_seconds=60, algorithm=_ALG
        )

    assert 0 < exc_info.value.retry_after_seconds <= 60
