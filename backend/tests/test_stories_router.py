from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from pydantic import SecretStr

from app.clients.groq import GROQ_API_URL
from app.clients.wikipedia import WIKIPEDIA_API_URL, RawPoi
from app.core.db import ensure_indexes
from app.core.dependencies import get_database, get_http_client
from app.main import app
from app.services.poi_repository import save_pois

CATHEDRAL_RAW = RawPoi(
    title="Holy Trinity Cathedral, Addis Ababa",
    page_id=1001,
    lat=9.0177,
    lng=38.7669,
    distance_m=450.2,
)

CATHEDRAL_SUMMARY = {
    "query": {
        "pages": {
            "1": {
                "title": "Holy Trinity Cathedral, Addis Ababa",
                "extract": "Holy Trinity Cathedral is the second largest church in "
                "Ethiopia, built to commemorate liberation from Italian occupation.",
            }
        }
    }
}

EMPTY_SUMMARY = {"query": {"pages": {"-1": {"title": "Nowhere", "missing": ""}}}}

GENERATED_TEXT = {
    "choices": [
        {"message": {"content": "A cathedral rises, built not for kings, but for freedom."}}
    ]
}


@pytest.fixture(autouse=True)
def no_real_sleep():
    with (
        patch("app.clients.wikipedia.asyncio.sleep", new=AsyncMock()),
        patch("app.clients.groq.asyncio.sleep", new=AsyncMock()),
    ):
        yield


@pytest.fixture(autouse=True)
def fake_groq_key(monkeypatch):
    fake_settings = type(
        "S", (), {"groq_api_key": SecretStr("test-key"), "groq_model": "test-model"}
    )()
    monkeypatch.setattr("app.clients.groq.get_settings", lambda: fake_settings)
    monkeypatch.setattr("app.services.story_service.get_settings", lambda: fake_settings)


@pytest.fixture
async def mongomock_db():
    client = AsyncMongoMockClient()
    db = client["test_aloft"]
    await ensure_indexes(db)
    return db


@pytest.fixture
async def shared_http_client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def test_client(mongomock_db, shared_http_client, auth_override) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: mongomock_db
    app.dependency_overrides[get_http_client] = lambda: shared_http_client
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_create_story_for_a_discovered_poi(test_client, mongomock_db):
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=CATHEDRAL_SUMMARY))
        respx.post(GROQ_API_URL).mock(return_value=httpx.Response(200, json=GENERATED_TEXT))

        response = test_client.post("/v1/pois/wikipedia:1001/story")

    assert response.status_code == 200
    body = response.json()
    assert body["poi_source_id"] == "wikipedia:1001"
    assert body["language"] == "en"
    assert "cathedral" in body["text_content"].lower()


def test_create_story_404_when_poi_was_never_discovered(test_client):
    response = test_client.post("/v1/pois/wikipedia:9999/story")

    assert response.status_code == 404
    assert "wikipedia:9999" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_story_400_when_insufficient_facts(test_client, mongomock_db):
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=EMPTY_SUMMARY))
        response = test_client.post("/v1/pois/wikipedia:1001/story")

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_create_story_accepts_language_query_param(test_client, mongomock_db):
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=CATHEDRAL_SUMMARY))
        groq_route = respx.post(GROQ_API_URL).mock(
            return_value=httpx.Response(200, json=GENERATED_TEXT)
        )

        response = test_client.post("/v1/pois/wikipedia:1001/story?language=am")

    assert response.json()["language"] == "am"
    sent_body = groq_route.calls[0].request.content.decode()
    assert "Amharic" in sent_body


@pytest.mark.asyncio
async def test_create_story_returns_cached_story_without_calling_groq(test_client, mongomock_db):
    """Second call must return the cached story and not hit Groq at all."""
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=CATHEDRAL_SUMMARY))
        groq_route = respx.post(GROQ_API_URL).mock(
            return_value=httpx.Response(200, json=GENERATED_TEXT)
        )
        test_client.post("/v1/pois/wikipedia:1001/story")
        response = test_client.post("/v1/pois/wikipedia:1001/story")

    assert response.status_code == 200
    assert groq_route.call_count == 1  # Groq called exactly once, not twice


def test_create_story_400_for_unsupported_language(test_client):
    response = test_client.post("/v1/pois/wikipedia:9999/story?language=xx")

    assert response.status_code == 400
    assert "Unsupported language" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_story_force_regenerates_even_when_cached(test_client, mongomock_db):
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=CATHEDRAL_SUMMARY))
        respx.post(GROQ_API_URL).mock(return_value=httpx.Response(200, json=GENERATED_TEXT))
        test_client.post("/v1/pois/wikipedia:1001/story")

    new_text = {"choices": [{"message": {"content": "A freshly regenerated story."}}]}
    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=CATHEDRAL_SUMMARY))
        groq_route = respx.post(GROQ_API_URL).mock(return_value=httpx.Response(200, json=new_text))
        response = test_client.post("/v1/pois/wikipedia:1001/story?force=true")

    assert groq_route.call_count == 1
    assert response.json()["text_content"] == "A freshly regenerated story."
