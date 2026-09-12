import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.db import ensure_indexes
from app.services.audio_repository import get_audio, save_audio


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


@pytest.fixture(autouse=True)
def fake_storage_dir(tmp_path, monkeypatch):
    fake_settings = type("S", (), {"audio_storage_dir": str(tmp_path), "r2_configured": False})()
    monkeypatch.setattr("app.services.audio_repository.get_settings", lambda: fake_settings)
    return tmp_path


@pytest.mark.asyncio
async def test_save_audio_writes_file_to_disk(fake_storage_dir, db):
    asset = await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"fake mp3 bytes")

    from pathlib import Path

    assert Path(asset.file_path).exists()
    assert Path(asset.file_path).read_bytes() == b"fake mp3 bytes"


@pytest.mark.asyncio
async def test_save_audio_sanitizes_colon_in_filename(db):
    asset = await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"audio")

    from pathlib import Path

    filename = Path(asset.file_path).name
    assert ":" not in filename
    assert "wikipedia_1001" in filename


@pytest.mark.asyncio
async def test_save_audio_persists_metadata_in_mongo(db):
    await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"audio")

    doc = await db.audio_assets.find_one(
        {"poi_source_id": "wikipedia:1001", "language": "en", "voice_name": "en-US-Wavenet-D"}
    )
    assert doc is not None
    assert doc["format"] == "mp3"


@pytest.mark.asyncio
async def test_get_audio_returns_none_when_not_found(db):
    result = await get_audio(db, "wikipedia:9999", "en", "en-US-Wavenet-D")

    assert result is None


@pytest.mark.asyncio
async def test_get_audio_returns_saved_metadata(db):
    saved = await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"audio")

    fetched = await get_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D")

    assert fetched is not None
    assert fetched.file_path == saved.file_path


@pytest.mark.asyncio
async def test_save_audio_upserts_on_regeneration(db):
    asset1 = await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"first version")
    asset2 = await save_audio(db, "wikipedia:1001", "en", "en-US-Wavenet-D", b"second version")

    from pathlib import Path

    assert asset1.file_path == asset2.file_path
    assert Path(asset2.file_path).read_bytes() == b"second version"
    assert await db.audio_assets.count_documents({}) == 1


@pytest.mark.asyncio
async def test_different_voices_produce_different_files(db):
    asset_a = await save_audio(db, "wikipedia:1001", "en", "voice-a", b"audio a")
    asset_b = await save_audio(db, "wikipedia:1001", "en", "voice-b", b"audio b")

    assert asset_a.file_path != asset_b.file_path
    assert await db.audio_assets.count_documents({}) == 2
