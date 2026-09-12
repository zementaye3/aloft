from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.clients.wikipedia import RawPoi
from app.core.db import ensure_indexes
from app.core.dependencies import get_database, get_http_client
from app.main import app
from app.models.story import Story
from app.services.audio_repository import save_audio
from app.services.download_service import RouteNotFoundError, build_download_zip
from app.services.poi_repository import save_poi_images, save_pois
from app.services.route_bundle_repository import save_route_bundle
from app.services.story_repository import save_story

ADD = (8.9806, 38.7992)
DXB = (25.2532, 55.3657)

CATHEDRAL = RawPoi(
    title="Holy Trinity Cathedral", page_id=1001, lat=9.0177, lng=38.7669, distance_m=450.2
)
MUSEUM = RawPoi(title="National Museum", page_id=1002, lat=9.0339, lng=38.7611, distance_m=1820.7)


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


@pytest.fixture(autouse=True)
def fake_settings(tmp_path, monkeypatch):
    s = type(
        "S",
        (),
        {
            "audio_storage_dir": str(tmp_path),
            "elevenlabs_voice_id": "en-US-Wavenet-D",
            "r2_configured": False,
        },
    )()
    monkeypatch.setattr("app.services.audio_repository.get_settings", lambda: s)
    monkeypatch.setattr("app.services.audio_service.get_settings", lambda: s)


def _story(source_id: str, text: str = "A story.") -> Story:
    return Story(
        poi_source_id=source_id,
        language="en",
        text_content=text,
        model_version="test-model",
    )


# --- Service tests ---


@pytest.mark.asyncio
async def test_raises_for_unknown_route(db):
    async with httpx.AsyncClient() as client:
        with pytest.raises(RouteNotFoundError):
            await build_download_zip(client, db, "no-such-route")


@pytest.mark.asyncio
async def test_includes_story_and_audio(db):
    await save_pois(db, [CATHEDRAL])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_story(db, _story("wikipedia:1001"))
    await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"fake mp3")

    async with httpx.AsyncClient() as client:
        zip_bytes = await build_download_zip(client, db, bundle.route_key, include_images=False)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert "wikipedia:1001" in zf.read("manifest.json").decode()
        assert zf.read("audio/wikipedia_1001.mp3") == b"fake mp3"


@pytest.mark.asyncio
async def test_text_only_when_audio_missing(db):
    await save_pois(db, [CATHEDRAL])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_story(db, _story("wikipedia:1001", text="Text but no audio yet."))

    async with httpx.AsyncClient() as client:
        zip_bytes = await build_download_zip(client, db, bundle.route_key, include_images=False)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert "Text but no audio yet." in zf.read("manifest.json").decode()
        assert not any(n.startswith("audio/") for n in zf.namelist())


@pytest.mark.asyncio
async def test_excludes_poi_with_no_story(db):
    await save_pois(db, [CATHEDRAL, MUSEUM])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001", "wikipedia:1002"])
    await save_story(db, _story("wikipedia:1001"))
    # wikipedia:1002 gets no story

    async with httpx.AsyncClient() as client:
        zip_bytes = await build_download_zip(client, db, bundle.route_key, include_images=False)

    manifest = zf_manifest(zip_bytes)
    assert "wikipedia:1001" in manifest
    assert "wikipedia:1002" not in manifest


@pytest.mark.asyncio
async def test_downloads_image_bytes_into_zip(db):
    await save_pois(db, [CATHEDRAL])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_story(db, _story("wikipedia:1001"))
    await save_poi_images(db, "wikipedia:1001", ["https://upload.wikimedia.org/photo.jpg"])

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://upload.wikimedia.org/photo.jpg").mock(
                return_value=httpx.Response(200, content=b"jpeg bytes")
            )
            zip_bytes = await build_download_zip(client, db, bundle.route_key, include_images=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert zf.read("images/wikipedia_1001_0.jpg") == b"jpeg bytes"


@pytest.mark.asyncio
async def test_failed_image_download_does_not_fail_bundle(db):
    await save_pois(db, [CATHEDRAL])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_story(db, _story("wikipedia:1001"))
    await save_poi_images(db, "wikipedia:1001", ["https://upload.wikimedia.org/broken.jpg"])

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get("https://upload.wikimedia.org/broken.jpg").mock(
                return_value=httpx.Response(404)
            )
            zip_bytes = await build_download_zip(client, db, bundle.route_key, include_images=True)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert "images/wikipedia_1001_0.jpg" not in zf.namelist()
        assert zf.read("manifest.json")  # rest of bundle still valid


def zf_manifest(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.read("manifest.json").decode()


# --- Router tests ---


@pytest.fixture
def test_client(db, auth_override) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_download_404_for_unknown_route(test_client):
    assert test_client.get("/routes/no-such-route/download").status_code == 404


@pytest.mark.asyncio
async def test_download_returns_zip(test_client, db):
    await save_pois(db, [CATHEDRAL])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_story(db, _story("wikipedia:1001"))
    await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"fake mp3")

    response = test_client.get(f"/v1/routes/{bundle.route_key}/download?include_images=false")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert "manifest.json" in zf.namelist()
        assert "audio/wikipedia_1001.mp3" in zf.namelist()
