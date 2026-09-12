import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.db import ensure_indexes
from app.models.story import Story
from app.services.story_repository import save_story


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


def _make_story(**overrides) -> Story:
    defaults = {
        "poi_source_id": "wikipedia:1001",
        "language": "en",
        "text_content": "A cathedral rises above the hills.",
        "model_version": "test-model",
    }
    defaults.update(overrides)
    return Story(**defaults)


@pytest.mark.asyncio
async def test_save_story_inserts_new_story(db):
    inserted = await save_story(db, _make_story())

    assert inserted is True
    assert await db.stories.count_documents({}) == 1


@pytest.mark.asyncio
async def test_save_story_upserts_without_duplicating_same_poi_and_language(db):
    await save_story(db, _make_story(text_content="First version"))
    inserted_second_time = await save_story(db, _make_story(text_content="Regenerated version"))

    assert inserted_second_time is False
    assert await db.stories.count_documents({}) == 1
    doc = await db.stories.find_one({"poi_source_id": "wikipedia:1001", "language": "en"})
    assert doc["text_content"] == "Regenerated version"


@pytest.mark.asyncio
async def test_save_story_allows_same_poi_different_language(db):
    await save_story(db, _make_story(language="en"))
    await save_story(db, _make_story(language="am", text_content="Amharic version"))

    assert await db.stories.count_documents({}) == 2
