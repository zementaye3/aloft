from unittest.mock import AsyncMock, patch

import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.db import ensure_indexes
from app.models.user import User
from app.services.route_bundle_repository import save_route_bundle

_FAKE_USER = User(
    user_id="000000000000000000000001",
    email="testuser@example.com",
    hashed_password="$2b$12$fakehashfortesting",
    is_active=True,
)

ADD = (8.9806, 38.7992)
DXB = (25.2532, 55.3657)


@pytest.fixture
async def mongomock_db():
    client = AsyncMongoMockClient()
    db = client["test_aloft"]
    await ensure_indexes(db)
    return db


@pytest.fixture
def mock_redis():
    """Create a mock Redis client for testing."""
    client = AsyncMock()

    # Make the methods return async-compatible values
    async def fake_set(*args, **kwargs):
        return True

    async def fake_get(*args, **kwargs):
        return None

    async def fake_lpush(*args, **kwargs):
        return True

    client.set = fake_set
    client.get = fake_get
    client.lpush = fake_lpush
    return client


@pytest.mark.asyncio
async def test_start_content_generation_creates_job(mongomock_db, mock_redis):
    """Test that start_content_generation creates a job successfully."""
    bundle = await save_route_bundle(mongomock_db, ADD, DXB, ["wikipedia:1001", "wikipedia:1002"])

    # Capture the job data
    stored_job_data = {}
    lpush_calls = []

    async def capture_set(key, value, *args, **kwargs):
        stored_job_data[key] = value
        return True

    async def capture_lpush(key, value, *args, **kwargs):
        lpush_calls.append((key, value))
        return True

    mock_redis.set = capture_set
    mock_redis.lpush = capture_lpush

    # Mock get_redis to return our mock
    with patch("app.routers.content.get_redis", return_value=mock_redis):
        from app.routers.content import start_content_generation

        result = await start_content_generation(
            bundle.route_key, "en", mongomock_db, mock_redis, _FAKE_USER
        )

    assert result.job_id is not None
    assert result.route_key == bundle.route_key
    assert result.total_pois == 2
    assert "job_id" in result.message
    assert len(lpush_calls) == 1
    assert lpush_calls[0] == ("queue:content", result.job_id)


@pytest.mark.asyncio
async def test_start_content_generation_404_for_unknown_route(mongomock_db, mock_redis):
    """Test that start_content_generation returns 404 for unknown route."""
    from fastapi import HTTPException

    from app.routers.content import start_content_generation

    with (
        patch("app.routers.content.get_redis", return_value=mock_redis),
        pytest.raises(HTTPException) as exc_info,
    ):
        await start_content_generation("no-such-route", "en", mongomock_db, mock_redis, _FAKE_USER)

    assert exc_info.value.status_code == 404
    assert "No route found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_start_content_generation_503_without_redis(mongomock_db):
    """Test that start_content_generation returns 503 when Redis is unavailable."""
    from fastapi import HTTPException

    from app.routers.content import start_content_generation

    bundle = await save_route_bundle(mongomock_db, ADD, DXB, ["wikipedia:1001"])

    with (
        patch("app.routers.content.get_redis", return_value=None),
        pytest.raises(HTTPException) as exc_info,
    ):
        await start_content_generation(bundle.route_key, "en", mongomock_db, None, _FAKE_USER)

    assert exc_info.value.status_code == 503
    assert "Redis not configured" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_content_generation_status_404_for_unknown_job(mock_redis):
    """Test that get_content_generation_status returns 404 for unknown job."""
    from fastapi import HTTPException

    from app.routers.content import get_content_generation_status

    async def fake_get_none(*args, **kwargs):
        return None

    mock_redis.get = fake_get_none

    with (
        patch("app.routers.content.get_redis", return_value=mock_redis),
        pytest.raises(HTTPException) as exc_info,
    ):
        await get_content_generation_status("test-route", "non-existent", _FAKE_USER)

    assert exc_info.value.status_code == 404
    assert "not found" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_content_generation_status_returns_job_status(mock_redis):
    """Test that get_content_generation_status returns job status correctly."""
    import json

    from app.routers.content import get_content_generation_status

    job_data = {
        "job_id": "test-job-123",
        "route_key": "test-route",
        "poi_source_ids": ["wikipedia:1001"],
        "language": "en",
        "status": "processing",
        "total": 1,
        "completed": 0,
        "failed": 0,
        "errors": [],
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }

    async def fake_get(*args, **kwargs):
        return json.dumps(job_data)

    mock_redis.get = fake_get

    with patch("app.routers.content.get_redis", return_value=mock_redis):
        result = await get_content_generation_status("test-route", "test-job-123", _FAKE_USER)

    assert result.job_id == "test-job-123"
    assert result.status == "processing"
    assert result.total == 1
    assert result.completed == 0
    assert result.progress_percent == 0.0
