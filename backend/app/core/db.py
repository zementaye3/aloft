from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from app.core.config import get_settings

logger = logging.getLogger("aloft.db")

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not connected. Call connect_to_mongo() at app startup first.")
    return _db


async def connect_to_mongo() -> None:
    global _client, _db
    settings = get_settings()
    logger.info("Connecting to MongoDB at %s", settings.mongodb_uri)
    try:
        # Check if using local MongoDB (localhost) to disable TLS
        is_local = "localhost" in settings.mongodb_uri or "127.0.0.1" in settings.mongodb_uri
        
        kwargs = {
            "serverSelectionTimeoutMS": 30000,
            "connectTimeoutMS": 30000,
            "retryWrites": True,
            "w": "majority",
            "retryReads": True,
        }
        
        # Only enable TLS for non-local connections
        if not is_local:
            kwargs["tls"] = True
            kwargs["tlsAllowInvalidCertificates"] = False
        
        _client = AsyncIOMotorClient(settings.mongodb_uri, **kwargs)
        _db = _client[settings.mongodb_db_name]
        await _db.command("ping")
        logger.info("MongoDB ping OK")
    except Exception as exc:
        logger.error("MongoDB connection failed: %s", exc)
        raise

    try:
        await ensure_indexes(_db)
    except Exception as exc:
        logger.error("MongoDB index creation failed: %s", exc)
        raise


async def close_mongo_connection() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
    _client = None
    _db = None


async def ensure_indexes(db: AsyncIOMotorDatabase) -> None:
    index_defs: list[tuple] = [
        (db.pois, [("location", "2dsphere")], "pois_location_2dsphere"),
        (db.pois, [("source_id", 1)], "pois_source_id_unique", {"unique": True, "sparse": True}),
        (
            db.stories,
            [("poi_source_id", 1), ("language", 1)],
            "stories_poi_lang_unique",
            {"unique": True},
        ),
        (db.airports, [("iata_code", 1)], "airports_iata_unique", {"unique": True}),
        (db.route_bundles, [("route_key", 1)], "route_bundles_route_key_unique", {"unique": True}),
        (db.users, [("email", 1)], "users_email_unique", {"unique": True}),
        (
            db.audio_assets,
            [("poi_source_id", 1), ("language", 1), ("voice_name", 1)],
            "audio_assets_poi_lang_voice_unique",
            {"unique": True},
        ),
        # Flight journal and user stats indexes
        (db.flight_journal, [("user_id", 1), ("flight_date", -1)], "flight_journal_user_date"),
        (db.flight_journal, [("entry_id", 1)], "flight_journal_entry_id_unique", {"unique": True}),
        (
            db.flight_journal,
            [("share_token", 1)],
            "flight_journal_share_token_unique",
            {"unique": True, "sparse": True},
        ),
        (db.user_stats, [("user_id", 1)], "user_stats_user_id_unique", {"unique": True}),
        # Favorites indexes
        (db.favorites, [("user_id", 1), ("saved_at", -1)], "favorites_user_saved_at"),
        (
            db.favorites,
            [("user_id", 1), ("poi_source_id", 1)],
            "favorites_user_poi_unique",
            {"unique": True},
        ),
        # Upcoming flights indexes
        (
            db.upcoming_flights,
            [("user_id", 1), ("departure_time", 1)],
            "upcoming_flights_user_departure",
        ),
        (
            db.upcoming_flights,
            [("departure_time", 1), ("notification_sent", 1)],
            "upcoming_flights_departure_notification",
        ),
    ]
    failed_indexes: list[str] = []
    for collection, keys, name, *rest in index_defs:
        try:
            kwargs = rest[0] if rest else {}
            await collection.create_index(keys, name=name, **kwargs)
            logger.debug("Created index %s on %s", name, collection.name)
        except Exception as exc:
            logger.error(
                "Index %s creation failed on collection %s: %s", name, collection.name, exc
            )
            failed_indexes.append(name)

    if failed_indexes:
        # Abort startup — missing indexes mean silent correctness bugs (duplicate
        # users if users_email_unique is missing) or severe performance degradation.
        raise RuntimeError(
            f"FATAL: Failed to create required indexes: {', '.join(failed_indexes)}. "
            "Check MongoDB permissions and existing data for conflicts."
        )

    logger.info("Connected to MongoDB database '%s'", db.name)
