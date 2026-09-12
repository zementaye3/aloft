from unittest.mock import AsyncMock, patch

import httpx
import pytest
from mongomock_motor import AsyncMongoMockClient

from app.clients.tts import TtsClientError
from app.clients.wikipedia import RawImage, RawPoi
from app.core.db import ensure_indexes
from app.models.story import Story
from app.services.content_generation_service import generate_content_for_route
from app.services.poi_repository import save_pois
from app.services.story_repository import save_story as real_save_story
from app.services.story_service import InsufficientFactsError

POI_A = RawPoi(title="Cathedral", page_id=1001, lat=9.0177, lng=38.7669, distance_m=450.2)
POI_B = RawPoi(title="Museum", page_id=1002, lat=9.0339, lng=38.7611, distance_m=1820.7)

_FAKE_IMAGE = RawImage(url="https://example.com/a.jpg", width=800, height=600, is_lead_image=True)


def _story(source_id: str) -> Story:
    return Story(
        poi_source_id=source_id,
        language="en",
        text_content="A vivid story.",
        model_version="test-model",
    )


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.mark.asyncio
async def test_generates_content_for_every_poi(db, http_client):
    await save_pois(db, [POI_A, POI_B])

    with (
        patch(
            "app.services.content_generation_service.generate_story",
            new=AsyncMock(side_effect=lambda c, sid, name, language: _story(sid)),
        ),
        patch("app.services.content_generation_service.save_story", new=AsyncMock()),
        patch(
            "app.services.content_generation_service.synthesize_story_audio",
            new=AsyncMock(return_value=b"audio"),
        ),
        patch("app.services.content_generation_service.save_audio", new=AsyncMock()),
        patch(
            "app.services.content_generation_service.get_images",
            new=AsyncMock(return_value=[_FAKE_IMAGE]),
        ),
    ):
        results = await generate_content_for_route(
            http_client, db, ["wikipedia:1001", "wikipedia:1002"]
        )

    assert len(results) == 2
    assert all(r.story_ready and r.audio_ready for r in results)
    assert all(r.images_found == 1 for r in results)
    assert all(r.error is None for r in results)


@pytest.mark.asyncio
async def test_one_failure_does_not_stop_the_rest(db, http_client):
    await save_pois(db, [POI_A, POI_B])

    async def flaky(c, sid, name, language):
        if sid == "wikipedia:1001":
            raise InsufficientFactsError("no facts")
        return _story(sid)

    with (
        patch(
            "app.services.content_generation_service.generate_story",
            new=AsyncMock(side_effect=flaky),
        ),
        patch("app.services.content_generation_service.save_story", new=AsyncMock()),
        patch(
            "app.services.content_generation_service.synthesize_story_audio",
            new=AsyncMock(return_value=b"audio"),
        ),
        patch("app.services.content_generation_service.save_audio", new=AsyncMock()),
        patch("app.services.content_generation_service.get_images", new=AsyncMock(return_value=[])),
    ):
        results = await generate_content_for_route(
            http_client, db, ["wikipedia:1001", "wikipedia:1002"]
        )

    by_id = {r.poi_source_id: r for r in results}
    assert by_id["wikipedia:1001"].story_ready is False
    assert by_id["wikipedia:1001"].error is not None
    assert by_id["wikipedia:1002"].story_ready is True
    assert by_id["wikipedia:1002"].error is None


@pytest.mark.asyncio
async def test_skips_regenerating_cached_story(db, http_client):
    await save_pois(db, [POI_A])
    await real_save_story(db, _story("wikipedia:1001"))

    mock_generate = AsyncMock()
    with (
        patch("app.services.content_generation_service.generate_story", new=mock_generate),
        patch(
            "app.services.content_generation_service.synthesize_story_audio",
            new=AsyncMock(return_value=b"audio"),
        ),
        patch("app.services.content_generation_service.save_audio", new=AsyncMock()),
        patch("app.services.content_generation_service.get_images", new=AsyncMock(return_value=[])),
    ):
        results = await generate_content_for_route(http_client, db, ["wikipedia:1001"])

    mock_generate.assert_not_called()
    assert results[0].story_ready is True


@pytest.mark.asyncio
async def test_undiscovered_poi_recorded_without_crashing_batch(db, http_client):
    with patch(
        "app.services.content_generation_service.get_images", new=AsyncMock(return_value=[])
    ):
        results = await generate_content_for_route(http_client, db, ["wikipedia:9999"])

    assert results[0].story_ready is False
    assert "never discovered" in results[0].error


@pytest.mark.asyncio
async def test_audio_failure_after_story_success_reports_story_ready(db, http_client):
    await save_pois(db, [POI_A])

    with (
        patch(
            "app.services.content_generation_service.generate_story",
            new=AsyncMock(return_value=_story("wikipedia:1001")),
        ),
        patch("app.services.content_generation_service.save_story", new=AsyncMock()),
        patch(
            "app.services.content_generation_service.synthesize_story_audio",
            new=AsyncMock(side_effect=TtsClientError("quota exhausted")),
        ),
        patch("app.services.content_generation_service.get_images", new=AsyncMock(return_value=[])),
    ):
        results = await generate_content_for_route(http_client, db, ["wikipedia:1001"])

    assert results[0].story_ready is True
    assert results[0].audio_ready is False
    assert "Audio synthesis failed" in results[0].error
