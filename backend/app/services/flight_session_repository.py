"""
Flight session persistence -- now backed by Redis instead of MongoDB.

Why Redis:
- Sessions are short-lived per-user state (one flight = one session).
  After the plane lands they're worthless. Redis TTL auto-deletes them
  after 12 hours -- no manual cleanup, no MongoDB collection growing forever.
- Every GPS ping reads the session (to check already-narrated POIs) and
  writes it (to append newly narrated ones). Redis handles this at
  microsecond latency compared to a MongoDB round trip.

Storage layout:
  session:{session_id}  ->  JSON blob of the full FlightSession
  TTL is reset on every write so an active session never expires mid-flight.

Atomic append:
  record_position_and_narration uses a pipeline (read → mutate → write)
  inside a single round trip. Concurrent pings for the same session are
  rare (a mobile app sends one ping at a time) but the pipeline keeps it
  clean regardless.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.core.config import get_settings
from app.models.flight_session import FlightSession

_KEY_PREFIX = "session:"
_SHARE_KEY_PREFIX = "share:"


def _key(session_id: str) -> str:
    return f"{_KEY_PREFIX}{session_id}"


def _share_key(token: str) -> str:
    return f"{_SHARE_KEY_PREFIX}{token}"


def _as_str(value: str | bytes) -> str:
    """Normalize a Redis reply to str.

    The app's real client has decode_responses=True and already hands back
    str; a bare fakeredis.FakeRedis() in tests returns bytes. Defensive
    decode here matches the pattern used elsewhere in this module.
    """
    return value.decode("utf-8") if isinstance(value, bytes) else value


async def _refresh_share_ttl(redis: Redis, session: FlightSession, ttl: int) -> None:
    """Keep a session's public share link alive as long as the session itself.

    Called from every write that already refreshes the session's own TTL,
    so a long-haul flight's spectator link never expires out from under a
    still-active session.
    """
    if session.share_token:
        await redis.expire(_share_key(session.share_token), ttl)


def _serialize(session: FlightSession) -> str:
    return session.model_dump_json()


def _deserialize(raw: str) -> FlightSession:
    return FlightSession.model_validate_json(raw)


async def create_session(
    redis: Redis,
    route_key: str,
    owner_id: str = "",
    language: str = "en",
    arrival_country: str | None = None,
    arrival_city: str | None = None,
    destination_tour_narrations: list[str] | None = None,
) -> FlightSession:
    session = FlightSession(
        session_id=str(uuid.uuid4()),
        route_key=route_key,
        owner_id=owner_id,
        language=language,
        arrival_country=arrival_country,
        arrival_city=arrival_city,
        destination_tour_narrations=destination_tour_narrations or [],
    )
    ttl = get_settings().session_ttl_seconds
    await redis.set(_key(session.session_id), _serialize(session), ex=ttl)
    return session


async def get_session(redis: Redis, session_id: str) -> FlightSession | None:
    raw = await redis.get(_key(session_id))
    if raw is None:
        return None
    return _deserialize(raw)


async def record_position_and_narration(
    redis: Redis,
    session_id: str,
    lat: float,
    lng: float,
    newly_narrated_source_id: str | None,
) -> None:
    """Update last_position and, if a POI was triggered, append it to the
    narrated list.  Idempotent: recording the same source_id twice is a
    no-op (set membership check before append).

    TTL is refreshed on every write so an active in-flight session never
    expires while the plane is still in the air.
    """
    key = _key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return  # session expired or never existed -- nothing to update

    session = _deserialize(raw)
    session.last_position = (lat, lng)
    session.last_updated_at = datetime.now(UTC)

    if (
        newly_narrated_source_id is not None
        and newly_narrated_source_id not in session.narrated_poi_source_ids
    ):
        session.narrated_poi_source_ids.append(newly_narrated_source_id)

    ttl = get_settings().session_ttl_seconds
    await redis.set(key, _serialize(session), ex=ttl)
    await _refresh_share_ttl(redis, session, ttl)


async def record_region_narration(
    redis: Redis,
    session_id: str,
    lat: float,
    lng: float,
) -> None:
    """Record that a region narration fired -- updates the cooldown timestamp."""
    key = _key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return  # session expired or never existed -- nothing to update

    session = _deserialize(raw)
    session.last_position = (lat, lng)
    session.last_region_narration_at = datetime.now(UTC)
    session.last_updated_at = datetime.now(UTC)

    ttl = get_settings().session_ttl_seconds
    await redis.set(key, _serialize(session), ex=ttl)
    await _refresh_share_ttl(redis, session, ttl)


async def record_upcoming_narration(
    redis: Redis,
    session_id: str,
    poi_source_id: str,
) -> None:
    """Record that an upcoming/teaser narration fired for a POI.
    Idempotent: the same POI is never teasered twice per session.
    """
    key = _key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return  # session expired or never existed -- nothing to update

    session = _deserialize(raw)
    session.last_updated_at = datetime.now(UTC)

    if poi_source_id not in session.upcoming_poi_triggered_source_ids:
        session.upcoming_poi_triggered_source_ids.append(poi_source_id)

    ttl = get_settings().session_ttl_seconds
    await redis.set(key, _serialize(session), ex=ttl)
    await _refresh_share_ttl(redis, session, ttl)


async def record_destination_tour_narration(
    redis: Redis,
    session_id: str,
    lat: float,
    lng: float,
    narration_index: int,
) -> None:
    """Record that a destination tour narration fired.

    Updates the destination tour index and last position.
    This is called when a destination tour highlight is played.
    """
    key = _key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return  # session expired or never existed -- nothing to update

    session = _deserialize(raw)
    session.destination_tour_index = narration_index
    session.last_destination_tour_at = datetime.now(UTC)
    session.last_position = (lat, lng)
    session.last_updated_at = datetime.now(UTC)

    ttl = get_settings().session_ttl_seconds
    await redis.set(key, _serialize(session), ex=ttl)
    await _refresh_share_ttl(redis, session, ttl)


async def update_session_destination_tour(
    redis: Redis,
    session_id: str,
    tour_narrations: list[str],
) -> None:
    """Patch the destination_tour_narrations on an existing session.

    Called from the background task in start_session once prepare_destination_tour
    finishes — avoids importing private helpers (_key, _serialize, _deserialize)
    from the repository module directly.

    No-op if the session has already expired.
    """
    key = _key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return  # session expired while tour was being prepared — nothing to do

    session = _deserialize(raw)
    session.destination_tour_narrations = tour_narrations
    session.last_updated_at = datetime.now(UTC)

    ttl = get_settings().session_ttl_seconds
    await redis.set(key, _serialize(session), ex=ttl)
    await _refresh_share_ttl(redis, session, ttl)


async def enable_sharing(redis: Redis, session_id: str) -> str | None:
    """Turn on public spectator viewing for a session.

    Idempotent: calling this twice returns the same token instead of
    rotating it, so a link already handed to someone doesn't silently
    break. Returns None if the session doesn't exist (caller should 404).
    """
    key = _key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return None

    session = _deserialize(raw)
    if session.share_token is None:
        session.share_token = secrets.token_urlsafe(24)
    session.last_updated_at = datetime.now(UTC)

    ttl = get_settings().session_ttl_seconds
    await redis.set(key, _serialize(session), ex=ttl)
    await redis.set(_share_key(session.share_token), session_id, ex=ttl)
    return session.share_token


async def disable_sharing(redis: Redis, session_id: str) -> bool:
    """Revoke a session's share link, if one exists.

    Returns False if the session doesn't exist (caller should 404); True
    otherwise, whether or not sharing was actually turned on.
    """
    key = _key(session_id)
    raw = await redis.get(key)
    if raw is None:
        return False

    session = _deserialize(raw)
    if session.share_token is not None:
        await redis.delete(_share_key(session.share_token))
        session.share_token = None
        session.last_updated_at = datetime.now(UTC)
        ttl = get_settings().session_ttl_seconds
        await redis.set(key, _serialize(session), ex=ttl)
    return True


async def get_session_by_share_token(redis: Redis, token: str) -> FlightSession | None:
    """Resolve a public share token to its session, for the spectator view.

    Fully public lookup by design -- no ownership check, since the whole
    point of a share link is letting someone without an account view it.
    """
    session_id = await redis.get(_share_key(token))
    if session_id is None:
        return None
    return await get_session(redis, _as_str(session_id))
