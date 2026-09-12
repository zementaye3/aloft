from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.core.db import ensure_indexes
from app.core.dependencies import get_database, get_http_client
from app.main import app
from app.models.story import Story
from app.services.story_repository import save_story

_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"


@pytest.fixture(autouse=True)
def fake_storage_dir(tmp_path, monkeypatch):
    fake_settings = type(
        "S",
        (),
        {
            "audio_storage_dir": str(tmp_path),
            "elevenlabs_voice_id": _DEFAULT_VOICE_ID,
            "r2_configured": False,
        },
    )()
    monkeypatch.setattr("app.services.audio_repository.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.services.audio_service.get_settings", lambda: fake_settings)
    return tmp_path


@pytest.fixture
async def mongomock_db():
    client = AsyncMongoMockClient()
    db = client["test_aloft"]
    await ensure_indexes(db)
    return db


@pytest.fixture
def test_client(mongomock_db, auth_override) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: mongomock_db
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


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
async def test_create_audio_for_existing_story(test_client, mongomock_db):
    await save_story(mongomock_db, _make_story())

    with patch("app.routers.audio.synthesize_story_audio", new=AsyncMock(return_value=b"fake-mp3")):
        response = test_client.post("/v1/pois/wikipedia:1001/audio")

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/mpeg"
    assert response.content == b"fake-mp3"


def test_create_audio_404_when_story_does_not_exist(test_client):
    response = test_client.post("/v1/pois/wikipedia:9999/audio")

    assert response.status_code == 404
    assert "wikipedia:9999" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_audio_caches_on_second_call(test_client, mongomock_db):
    await save_story(mongomock_db, _make_story())

    with patch(
        "app.routers.audio.synthesize_story_audio", new=AsyncMock(return_value=b"first-call-audio")
    ):
        first_response = test_client.post("/v1/pois/wikipedia:1001/audio")

    with patch(
        "app.routers.audio.synthesize_story_audio",
        new=AsyncMock(return_value=b"should-never-be-returned"),
    ) as mock_second:
        second_response = test_client.post("/v1/pois/wikipedia:1001/audio")

    assert mock_second.call_count == 0
    assert first_response.content == second_response.content == b"first-call-audio"


@pytest.mark.asyncio
async def test_different_languages_are_separate_files(test_client, mongomock_db):
    await save_story(mongomock_db, _make_story(language="en"))
    await save_story(mongomock_db, _make_story(language="am", text_content="Amharic version"))

    with patch(
        "app.routers.audio.synthesize_story_audio", new=AsyncMock(return_value=b"english-audio")
    ):
        test_client.post("/v1/pois/wikipedia:1001/audio?language=en")

    with patch(
        "app.routers.audio.synthesize_story_audio", new=AsyncMock(return_value=b"amharic-audio")
    ):
        response = test_client.post("/v1/pois/wikipedia:1001/audio?language=am")

    assert response.content == b"amharic-audio"
