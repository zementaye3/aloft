from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.story import Story


async def get_story(db: AsyncIOMotorDatabase, poi_source_id: str, language: str) -> Story | None:
    doc = await db.stories.find_one({"poi_source_id": poi_source_id, "language": language})
    if doc is None:
        return None
    doc.pop("_id", None)
    return Story(**doc)


async def get_stories_batch(
    db: AsyncIOMotorDatabase, poi_source_ids: list[str], language: str
) -> list[Story]:
    """Fetch multiple stories for the same language in one query."""
    if not poi_source_ids:
        return []
    cursor = db.stories.find(
        {
            "poi_source_id": {"$in": poi_source_ids},
            "language": language,
        }
    )
    stories = []
    async for doc in cursor:
        doc.pop("_id", None)
        stories.append(Story(**doc))
    return stories


async def save_story(db: AsyncIOMotorDatabase, story: Story) -> bool:
    """Upsert a story by (poi_source_id, language). Returns True if newly inserted."""
    result = await db.stories.update_one(
        {"poi_source_id": story.poi_source_id, "language": story.language},
        {"$set": story.to_mongo_dict()},
        upsert=True,
    )
    return result.upserted_id is not None
