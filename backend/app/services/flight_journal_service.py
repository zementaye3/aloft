from __future__ import annotations

import secrets
import uuid

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.flight_journal import FlightJournalEntry, UserStats

BADGES = {
    "first_flight": lambda s: s.total_flights >= 1,
    "five_flights": lambda s: s.total_flights >= 5,
    "ten_flights": lambda s: s.total_flights >= 10,
    "world_traveler": lambda s: s.total_countries >= 10,
    "globe_trotter": lambda s: s.total_countries >= 25,
    "century_places": lambda s: s.total_places_narrated >= 100,
    "long_haul": lambda s: s.total_distance_km >= 10000,
    "around_the_world": lambda s: s.total_distance_km >= 40075,
}


async def save_flight_journal_entry(
    db: AsyncIOMotorDatabase,
    user_id: str,
    session_data: dict,
) -> FlightJournalEntry:
    """Call this when a flight session ends."""
    entry = FlightJournalEntry(
        entry_id=str(uuid.uuid4()),
        user_id=user_id,
        **session_data,
    )
    await db.flight_journal.insert_one(entry.to_mongo_dict())
    await _update_user_stats(db, user_id, entry)
    return entry


async def get_user_stats(db: AsyncIOMotorDatabase, user_id: str) -> UserStats:
    doc = await db.user_stats.find_one({"user_id": user_id})
    if doc is None:
        return UserStats(user_id=user_id)
    doc.pop("_id", None)
    return UserStats(**doc)


async def get_flight_history(
    db: AsyncIOMotorDatabase, user_id: str, limit: int = 20
) -> list[FlightJournalEntry]:
    cursor = db.flight_journal.find(
        {"user_id": user_id},
        sort=[("flight_date", -1)],
        limit=limit,
    )
    entries = []
    async for doc in cursor:
        doc.pop("_id", None)
        entries.append(FlightJournalEntry(**doc))
    return entries


async def get_journal_entry_by_id(
    db: AsyncIOMotorDatabase, entry_id: str, user_id: str
) -> FlightJournalEntry | None:
    """Owner-scoped lookup of a single journal entry.

    Scoped by user_id as well as entry_id so one user can never fetch
    another's entry by guessing/enumerating ids.
    """
    doc = await db.flight_journal.find_one({"entry_id": entry_id, "user_id": user_id})
    if doc is None:
        return None
    doc.pop("_id", None)
    return FlightJournalEntry(**doc)


async def enable_journal_sharing(
    db: AsyncIOMotorDatabase, entry_id: str, user_id: str
) -> str | None:
    """Turn on a public share link for this journal entry.

    Idempotent: calling this twice returns the same token instead of
    rotating it, so a link already handed to someone doesn't silently
    break. Returns None if the entry doesn't exist or isn't owned by this
    user (caller should 404).
    """
    entry = await get_journal_entry_by_id(db, entry_id, user_id)
    if entry is None:
        return None
    if entry.share_token is None:
        entry.share_token = secrets.token_urlsafe(24)
        await db.flight_journal.update_one(
            {"entry_id": entry_id, "user_id": user_id},
            {"$set": {"share_token": entry.share_token}},
        )
    return entry.share_token


async def disable_journal_sharing(
    db: AsyncIOMotorDatabase, entry_id: str, user_id: str
) -> bool:
    """Revoke a journal entry's share link, if one exists.

    Returns False if the entry doesn't exist / isn't owned by this user
    (caller should 404); True otherwise, whether or not sharing was
    actually on.
    """
    entry = await get_journal_entry_by_id(db, entry_id, user_id)
    if entry is None:
        return False
    if entry.share_token is not None:
        await db.flight_journal.update_one(
            {"entry_id": entry_id, "user_id": user_id},
            {"$set": {"share_token": None}},
        )
    return True


async def get_journal_entry_by_share_token(
    db: AsyncIOMotorDatabase, token: str
) -> FlightJournalEntry | None:
    """Resolve a public share token to its journal entry.

    Fully public lookup by design -- no ownership check, since the whole
    point of a share link is letting someone without an account view it.
    Mirrors get_session_by_share_token's pattern for live sessions.
    """
    doc = await db.flight_journal.find_one({"share_token": token})
    if doc is None:
        return None
    doc.pop("_id", None)
    return FlightJournalEntry(**doc)


async def _update_user_stats(
    db: AsyncIOMotorDatabase, user_id: str, entry: FlightJournalEntry
) -> None:
    stats = await get_user_stats(db, user_id)

    stats.total_flights += 1
    stats.total_distance_km += entry.distance_km
    stats.total_places_narrated += len(entry.narrated_poi_names)

    for country in entry.countries_flown_over:
        if country not in stats.all_countries:
            stats.all_countries.append(country)
    stats.total_countries = len(stats.all_countries)

    for poi_name in entry.narrated_poi_names:
        if poi_name not in stats.all_narrated_poi_names:
            stats.all_narrated_poi_names.append(poi_name)

    # Check badges
    for badge_id, condition in BADGES.items():
        if badge_id not in stats.badges_earned and condition(stats):
            stats.badges_earned.append(badge_id)

    await db.user_stats.update_one(
        {"user_id": user_id},
        {"$set": stats.model_dump()},
        upsert=True,
    )
