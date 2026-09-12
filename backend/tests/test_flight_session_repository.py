"""
Tests for the Redis-backed flight session repository.
Uses fakeredis-py -- an in-process Redis emulator, no real server needed.
"""

from __future__ import annotations

import fakeredis.aioredis as fakeredis
import pytest

from app.services.flight_session_repository import (
    create_session,
    disable_sharing,
    enable_sharing,
    get_session,
    get_session_by_share_token,
    record_position_and_narration,
)


@pytest.fixture
async def redis():
    server = fakeredis.FakeRedis()
    yield server
    await server.aclose()


@pytest.mark.asyncio
async def test_create_session_returns_unique_session_ids(redis):
    s1 = await create_session(redis, "route-key-1")
    s2 = await create_session(redis, "route-key-1")

    assert s1.session_id != s2.session_id


@pytest.mark.asyncio
async def test_create_session_starts_with_empty_narrated_list(redis):
    session = await create_session(redis, "route-key-1")

    assert session.narrated_poi_source_ids == []
    assert session.last_position is None


@pytest.mark.asyncio
async def test_get_session_returns_none_when_not_found(redis):
    assert await get_session(redis, "no-such-session") is None


@pytest.mark.asyncio
async def test_get_session_roundtrips_correctly(redis):
    created = await create_session(redis, "route-key-1")

    fetched = await get_session(redis, created.session_id)

    assert fetched is not None
    assert fetched.route_key == "route-key-1"


@pytest.mark.asyncio
async def test_record_position_updates_last_position(redis):
    session = await create_session(redis, "route-key-1")

    await record_position_and_narration(redis, session.session_id, 9.0, 38.0, None)

    updated = await get_session(redis, session.session_id)
    assert updated.last_position == (9.0, 38.0)


@pytest.mark.asyncio
async def test_record_position_adds_newly_narrated_poi(redis):
    session = await create_session(redis, "route-key-1")

    await record_position_and_narration(redis, session.session_id, 9.0, 38.0, "wikipedia:1001")

    updated = await get_session(redis, session.session_id)
    assert "wikipedia:1001" in updated.narrated_poi_source_ids


@pytest.mark.asyncio
async def test_record_position_does_not_duplicate_poi_narrated_twice(redis):
    session = await create_session(redis, "route-key-1")

    await record_position_and_narration(redis, session.session_id, 9.0, 38.0, "wikipedia:1001")
    await record_position_and_narration(redis, session.session_id, 9.01, 38.01, "wikipedia:1001")

    updated = await get_session(redis, session.session_id)
    assert updated.narrated_poi_source_ids.count("wikipedia:1001") == 1


@pytest.mark.asyncio
async def test_record_position_accumulates_multiple_distinct_pois(redis):
    session = await create_session(redis, "route-key-1")

    await record_position_and_narration(redis, session.session_id, 9.0, 38.0, "wikipedia:1001")
    await record_position_and_narration(redis, session.session_id, 12.0, 43.0, "wikipedia:1002")

    updated = await get_session(redis, session.session_id)
    assert set(updated.narrated_poi_source_ids) == {"wikipedia:1001", "wikipedia:1002"}


@pytest.mark.asyncio
async def test_record_position_on_expired_session_is_a_noop(redis):
    """If a session has expired (TTL hit), record_position silently does
    nothing rather than crashing -- the router will 404 on the next read.
    """
    await record_position_and_narration(redis, "expired-session-id", 9.0, 38.0, None)
    # No crash, and the key still doesn't exist
    assert await get_session(redis, "expired-session-id") is None


@pytest.mark.asyncio
async def test_enable_sharing_returns_none_for_unknown_session(redis):
    assert await enable_sharing(redis, "no-such-session") is None


@pytest.mark.asyncio
async def test_enable_sharing_sets_share_token_on_session(redis):
    session = await create_session(redis, "route-key-1")

    token = await enable_sharing(redis, session.session_id)

    updated = await get_session(redis, session.session_id)
    assert token is not None
    assert updated.share_token == token


@pytest.mark.asyncio
async def test_enable_sharing_is_idempotent(redis):
    session = await create_session(redis, "route-key-1")

    first_token = await enable_sharing(redis, session.session_id)
    second_token = await enable_sharing(redis, session.session_id)

    assert first_token == second_token


@pytest.mark.asyncio
async def test_get_session_by_share_token_resolves_shared_session(redis):
    session = await create_session(redis, "route-key-1")
    token = await enable_sharing(redis, session.session_id)

    fetched = await get_session_by_share_token(redis, token)

    assert fetched is not None
    assert fetched.session_id == session.session_id


@pytest.mark.asyncio
async def test_get_session_by_share_token_returns_none_for_unknown_token(redis):
    assert await get_session_by_share_token(redis, "no-such-token") is None


@pytest.mark.asyncio
async def test_get_session_by_share_token_returns_none_when_sharing_never_enabled(redis):
    await create_session(redis, "route-key-1")

    assert await get_session_by_share_token(redis, "") is None


@pytest.mark.asyncio
async def test_disable_sharing_returns_false_for_unknown_session(redis):
    assert await disable_sharing(redis, "no-such-session") is False


@pytest.mark.asyncio
async def test_disable_sharing_clears_share_token(redis):
    session = await create_session(redis, "route-key-1")
    await enable_sharing(redis, session.session_id)

    result = await disable_sharing(redis, session.session_id)

    updated = await get_session(redis, session.session_id)
    assert result is True
    assert updated.share_token is None


@pytest.mark.asyncio
async def test_disable_sharing_revokes_the_old_token(redis):
    session = await create_session(redis, "route-key-1")
    token = await enable_sharing(redis, session.session_id)

    await disable_sharing(redis, session.session_id)

    assert await get_session_by_share_token(redis, token) is None


@pytest.mark.asyncio
async def test_disable_sharing_when_sharing_was_never_on_is_a_noop(redis):
    session = await create_session(redis, "route-key-1")

    result = await disable_sharing(redis, session.session_id)

    updated = await get_session(redis, session.session_id)
    assert result is True
    assert updated.share_token is None


@pytest.mark.asyncio
async def test_re_enabling_sharing_after_disable_issues_a_new_token(redis):
    session = await create_session(redis, "route-key-1")
    first_token = await enable_sharing(redis, session.session_id)
    await disable_sharing(redis, session.session_id)

    second_token = await enable_sharing(redis, session.session_id)

    assert second_token != first_token
    assert await get_session_by_share_token(redis, first_token) is None
    assert (await get_session_by_share_token(redis, second_token)).session_id == session.session_id


@pytest.mark.asyncio
async def test_share_token_ttl_refreshes_on_position_update(redis):
    """The share-link key should never outlive (or expire before) the
    session it points to -- record_position_and_narration refreshes both.
    """
    session = await create_session(redis, "route-key-1")
    token = await enable_sharing(redis, session.session_id)

    await redis.expire(f"share:{token}", 5)
    await record_position_and_narration(redis, session.session_id, 9.0, 38.0, None)

    ttl = await redis.ttl(f"share:{token}")
    assert ttl > 5


@pytest.mark.asyncio
async def test_create_session_stores_language(redis):
    session = await create_session(redis, "route-key-1", language="fr")

    fetched = await get_session(redis, session.session_id)
    assert fetched.language == "fr"
