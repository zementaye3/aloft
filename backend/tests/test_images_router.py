from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.clients.wikipedia import WIKIPEDIA_API_URL, RawPoi
from app.core.db import ensure_indexes
from app.core.dependencies import get_database, get_http_client
from app.main import app
from app.services.poi_repository import get_poi, save_pois

CATHEDRAL_RAW = RawPoi(
    title="Holy Trinity Cathedral, Addis Ababa",
    page_id=1001,
    lat=9.0177,
    lng=38.7669,
    distance_m=450.2,
)

LEAD_IMAGE_RESPONSE = {
    "query": {
        "pages": {
            "1": {
                "original": {
                    "source": "https://upload.wikimedia.org/commons/cathedral_lead.jpg",
                    "width": 1200,
                    "height": 800,
                }
            }
        }
    }
}

GALLERY_RESPONSE = {
    "query": {
        "pages": {
            "2": {
                "imageinfo": [
                    {
                        "url": "https://upload.wikimedia.org/commons/Commons-logo.svg",
                        "width": 1024,
                        "height": 1024,
                        "mime": "image/svg+xml",
                    }
                ]
            }
        }
    }
}

EMPTY_GALLERY_RESPONSE = {"query": {"pages": {}}}
NO_LEAD_IMAGE_RESPONSE = {"query": {"pages": {"1": {}}}}


def _images_handler(lead_response: dict, gallery_response: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        if "piprop" in request.url.params:
            return httpx.Response(200, json=lead_response)
        if "generator" in request.url.params:
            return httpx.Response(200, json=gallery_response)
        return httpx.Response(404)

    return handler


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


@pytest.mark.asyncio
async def test_fetch_images_for_a_discovered_poi(test_client, mongomock_db):
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(
            side_effect=_images_handler(LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
        )
        response = test_client.post("/v1/pois/wikipedia:1001/images")

    assert response.status_code == 200
    body = response.json()
    assert body["poi_source_id"] == "wikipedia:1001"
    assert len(body["images"]) == 1  # logo filtered out, only lead image survives
    assert body["images"][0]["is_lead_image"] is True


def test_fetch_images_404_when_poi_was_never_discovered(test_client):
    response = test_client.post("/v1/pois/wikipedia:9999/images")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_fetch_images_persists_image_refs_on_the_poi(test_client, mongomock_db):
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(
            side_effect=_images_handler(LEAD_IMAGE_RESPONSE, GALLERY_RESPONSE)
        )
        test_client.post("/v1/pois/wikipedia:1001/images")

    poi = await get_poi(mongomock_db, "wikipedia:1001")
    assert poi.image_refs == ["https://upload.wikimedia.org/commons/cathedral_lead.jpg"]


@pytest.mark.asyncio
async def test_fetch_images_returns_empty_list_honestly_when_nothing_real_exists(
    test_client, mongomock_db
):
    """When Wikipedia has no images AND Openverse has no results, return an empty list."""
    await save_pois(mongomock_db, [CATHEDRAL_RAW])

    _OPENVERSE_IMAGES_URL = "https://api.openverse.org/v1/images/"

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(
            side_effect=_images_handler(NO_LEAD_IMAGE_RESPONSE, EMPTY_GALLERY_RESPONSE)
        )
        # Openverse fallback is tried when Wikipedia returns nothing.
        # Return an empty result set so the test verifies honest empty response.
        respx.get(_OPENVERSE_IMAGES_URL).mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        response = test_client.post("/v1/pois/wikipedia:1001/images")

    assert response.status_code == 200
    assert response.json()["images"] == []
