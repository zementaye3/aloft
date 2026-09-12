"""
GDPR Article 15 (Right of Access) and Article 17 (Right to Erasure) — real implementation.

Data scoping
============
Only three collections are genuinely user-scoped personal data:
  - favorites        (app/models/favorite.py    - has user_id)
  - flight_journal    (app/models/flight_journal.py - has user_id)
  - user_stats         (app/models/flight_journal.py - has user_id)
  - upcoming_flights  (app/models/upcoming_flight.py - has user_id)

stories, audio_assets, and route_bundles are NOT user-scoped — they're a
shared content cache keyed by POI/route, generated once and reused across
every user who flies a similar route. They contain no personal data, so
they're correctly excluded from export/deletion (the previous version of
this file queried a "user_id" field that doesn't exist on those
collections, silently returning nothing while looking like it worked).

Flight sessions live in Redis with a 12-hour TTL (see
flight_session_repository.py) and expire on their own — nothing to export
or delete there beyond what already self-destructs.

Deletion flow
=============
Deletion is scheduled with a grace period (default 48h, see
settings.gdpr_deletion_grace_period_hours), not immediate — a user who
deletes by mistake, or whose account is compromised, has a window to
cancel via POST /v1/user/data/cancel-deletion. During the grace period the
account is deactivated (blocks login) but not yet purged.

This mirrors the existing Redis-backed worker pattern used by
content_job_service.py / content_worker.py, just for a different job type:
  gdpr:pending_deletions        -> sorted set, score = due_at unix timestamp
  gdpr:deletion:{user_id}       -> hash of {reason, requested_at, due_at}

gdpr_worker.py polls this sorted set and actually performs the cascade
delete once a deletion is due.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import get_settings
from app.models.user import User
from app.services.user_repository import delete_user, get_user_by_id, update_user

logger = logging.getLogger("aloft.services.gdpr")

_PENDING_SET_KEY = "gdpr:pending_deletions"
_DETAIL_KEY_PREFIX = "gdpr:deletion:"


class GdprError(Exception):
    pass


# ---------------------------------------------------------------------------
# Export (Article 15)
# ---------------------------------------------------------------------------


async def collect_user_export(db: AsyncIOMotorDatabase, user: User) -> dict[str, Any]:
    """Collect all real personal data for a user into a single export document."""
    favorites = await db.favorites.find({"user_id": user.user_id}).to_list(length=None)
    for doc in favorites:
        doc.pop("_id", None)

    journal_entries = await db.flight_journal.find({"user_id": user.user_id}).to_list(length=None)
    for doc in journal_entries:
        doc.pop("_id", None)

    stats_doc = await db.user_stats.find_one({"user_id": user.user_id})
    if stats_doc:
        stats_doc.pop("_id", None)

    upcoming_flights = await db.upcoming_flights.find({"user_id": user.user_id}).to_list(
        length=None
    )
    for doc in upcoming_flights:
        doc.pop("_id", None)

    return {
        "user_id": user.user_id,
        "email": user.email,
        "role": user.role.value,
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "mfa_enabled": user.mfa_enabled,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "last_login_at": user.last_login_at,
        "last_login_ip": user.last_login_ip,
        "password_changed_at": user.password_changed_at,
        "favorites": favorites,
        "flight_journal_entries": journal_entries,
        "flight_stats": stats_doc,
        "upcoming_flights": upcoming_flights,
        "export_timestamp": datetime.now(UTC),
        "note": (
            "This export excludes app-wide content caches (points of interest, "
            "narration text, audio, and route data) because that content is not "
            "tied to any individual user -- it's shared, POI-keyed content "
            "generated once and served to anyone flying a similar route."
        ),
    }


# ---------------------------------------------------------------------------
# Deletion (Article 17)
# ---------------------------------------------------------------------------


async def schedule_deletion(
    db: AsyncIOMotorDatabase,
    redis: Redis,
    user: User,
    reason: str,
) -> dict[str, Any]:
    """Deactivate the account immediately and schedule a real cascade delete
    after the grace period. Returns the deletion record.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    due_at = now + timedelta(hours=settings.gdpr_deletion_grace_period_hours)

    # Block login immediately, even though the actual purge waits for the
    # grace period. This matters if the deletion request followed a
    # suspected account compromise.
    user.is_active = False
    await update_user(db, user)

    await redis.zadd(_PENDING_SET_KEY, {user.user_id: due_at.timestamp()})
    await redis.hset(
        f"{_DETAIL_KEY_PREFIX}{user.user_id}",
        mapping={
            "email": user.email,
            "reason": reason,
            "requested_at": now.isoformat(),
            "due_at": due_at.isoformat(),
        },
    )

    logger.warning(
        "GDPR deletion scheduled for user %s, due %s, reason: %s",
        user.user_id,
        due_at.isoformat(),
        reason,
    )

    return {
        "user_id": user.user_id,
        "email": user.email,
        "deletion_requested": True,
        "deletion_scheduled": due_at,
        "data_categories_to_delete": [
            "user_profile",
            "favorites",
            "flight_journal_entries",
            "flight_stats",
            "upcoming_flights",
        ],
    }


async def cancel_deletion(db: AsyncIOMotorDatabase, redis: Redis, user_id: str) -> bool:
    """Cancel a pending deletion and reactivate the account.

    Returns False if there was no pending deletion to cancel.
    """
    removed = await redis.zrem(_PENDING_SET_KEY, user_id)
    await redis.delete(f"{_DETAIL_KEY_PREFIX}{user_id}")

    if not removed:
        return False

    user = await get_user_by_id(db, user_id)
    if user is not None:
        user.is_active = True
        await update_user(db, user)

    logger.info("GDPR deletion cancelled for user %s", user_id)
    return True


async def get_deletion_status(redis: Redis, user_id: str) -> dict[str, Any] | None:
    """Return the pending deletion record for a user, or None if none is pending."""
    detail = await redis.hgetall(f"{_DETAIL_KEY_PREFIX}{user_id}")
    if not detail:
        return None
    # redis.asyncio with decode_responses=True returns str keys/values already;
    # guard for bytes in case the client isn't configured that way.
    return {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in detail.items()
    }


async def process_due_deletions(db: AsyncIOMotorDatabase, redis: Redis) -> int:
    """Find and actually purge every deletion whose grace period has elapsed.

    Returns the number of accounts deleted. Called periodically by
    gdpr_worker.run_gdpr_deletion_worker — this is where the real DELETE
    happens, not in the router.
    """
    now_ts = datetime.now(UTC).timestamp()
    due_user_ids = await redis.zrangebyscore(_PENDING_SET_KEY, min=0, max=now_ts)

    deleted_count = 0
    for user_id in due_user_ids:
        uid = user_id.decode() if isinstance(user_id, bytes) else user_id
        try:
            await _purge_user_data(db, uid)
            deleted_count += 1
        except Exception:
            logger.exception("Failed to purge user %s -- will retry next cycle", uid)
            continue  # leave it in the sorted set to retry next poll

        await redis.zrem(_PENDING_SET_KEY, uid)
        await redis.delete(f"{_DETAIL_KEY_PREFIX}{uid}")

    return deleted_count


async def _purge_user_data(db: AsyncIOMotorDatabase, user_id: str) -> None:
    """The actual cascade delete. Called only once a deletion is due."""
    await db.favorites.delete_many({"user_id": user_id})
    await db.flight_journal.delete_many({"user_id": user_id})
    await db.user_stats.delete_many({"user_id": user_id})
    await db.upcoming_flights.delete_many({"user_id": user_id})

    deleted = await delete_user(db, user_id)
    if not deleted:
        logger.warning("GDPR purge: user %s had no user document to delete", user_id)

    logger.warning("GDPR purge complete for user %s", user_id)
