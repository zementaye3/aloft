from unittest.mock import AsyncMock

import pytest

from app.services.content_job_service import (
    JobStatus,
    create_content_job,
    get_job_status,
    update_job_progress,
)


@pytest.fixture
async def mock_redis():
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
async def test_create_content_job(mock_redis):
    """Test that a content job is created with correct metadata."""
    route_key = "test-route-123"
    poi_source_ids = ["wikipedia:1001", "wikipedia:1002"]
    language = "en"

    # Capture the job data that would be stored
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

    job_id = await create_content_job(mock_redis, route_key, poi_source_ids, language)

    # Verify job_id is a string (UUID)
    assert isinstance(job_id, str)
    assert len(job_id) == 36  # UUID length

    # Verify job was stored
    assert f"job:{job_id}" in stored_job_data
    import json

    parsed_job = json.loads(stored_job_data[f"job:{job_id}"])
    assert parsed_job["job_id"] == job_id
    assert parsed_job["route_key"] == route_key
    assert parsed_job["poi_source_ids"] == poi_source_ids
    assert parsed_job["language"] == language
    assert parsed_job["status"] == JobStatus.PENDING
    assert parsed_job["total"] == 2
    assert parsed_job["completed"] == 0
    assert parsed_job["failed"] == 0
    assert "created_at" in parsed_job
    assert "updated_at" in parsed_job

    # Verify job was pushed to queue
    assert len(lpush_calls) == 1
    assert lpush_calls[0] == ("queue:content", job_id)


@pytest.mark.asyncio
async def test_get_job_status(mock_redis):
    """Test retrieving job status from Redis."""
    job_id = "test-job-123"
    job_data = {
        "job_id": job_id,
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
    import json

    async def fake_get(key, *args, **kwargs):
        return json.dumps(job_data)

    mock_redis.get = fake_get

    status = await get_job_status(mock_redis, job_id)

    assert status is not None
    assert status["job_id"] == job_id
    assert status["status"] == "processing"


@pytest.mark.asyncio
async def test_get_job_status_not_found(mock_redis):
    """Test that get_job_status returns None for non-existent job."""

    async def fake_get_none(key, *args, **kwargs):
        return None

    mock_redis.get = fake_get_none

    status = await get_job_status(mock_redis, "non-existent-job")

    assert status is None


@pytest.mark.asyncio
async def test_update_job_progress(mock_redis):
    """Test updating job progress."""
    job_id = "test-job-123"
    job_data = {
        "job_id": job_id,
        "route_key": "test-route",
        "poi_source_ids": ["wikipedia:1001", "wikipedia:1002"],
        "language": "en",
        "status": "processing",
        "total": 2,
        "completed": 0,
        "failed": 0,
        "errors": [],
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00",
    }
    import json

    stored_data = {}

    async def fake_get(key, *args, **kwargs):
        return json.dumps(job_data)

    async def fake_set(key, value, *args, **kwargs):
        stored_data[key] = value
        return True

    mock_redis.get = fake_get
    mock_redis.set = fake_set

    await update_job_progress(
        mock_redis, job_id, completed=1, failed=0, status=JobStatus.PROCESSING
    )

    # Verify job was updated
    assert f"job:{job_id}" in stored_data
    updated_job = json.loads(stored_data[f"job:{job_id}"])
    assert updated_job["completed"] == 1
    assert updated_job["failed"] == 0
    assert updated_job["status"] == JobStatus.PROCESSING
    assert "updated_at" in updated_job


@pytest.mark.asyncio
async def test_update_job_progress_with_error(mock_redis):
    """Test updating job progress with an error message."""
    job_id = "test-job-123"
    job_data = {
        "job_id": job_id,
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
    import json

    stored_data = {}

    async def fake_get(key, *args, **kwargs):
        return json.dumps(job_data)

    async def fake_set(key, value, *args, **kwargs):
        stored_data[key] = value
        return True

    mock_redis.get = fake_get
    mock_redis.set = fake_set

    error_msg = "Rate limit exceeded"
    await update_job_progress(
        mock_redis, job_id, completed=0, failed=1, status=JobStatus.PROCESSING, error=error_msg
    )

    # Verify error was added
    updated_job = json.loads(stored_data[f"job:{job_id}"])
    assert updated_job["failed"] == 1
    assert error_msg in updated_job["errors"]


@pytest.mark.asyncio
async def test_update_job_progress_job_not_found(mock_redis):
    """Test that update_job_progress handles missing job gracefully."""

    async def fake_get_none(key, *args, **kwargs):
        return None

    set_called = False

    async def fake_set(key, value, *args, **kwargs):
        nonlocal set_called
        set_called = True
        return True

    mock_redis.get = fake_get_none
    mock_redis.set = fake_set

    # Should not raise an exception
    await update_job_progress(
        mock_redis, "non-existent-job", completed=1, failed=0, status=JobStatus.PROCESSING
    )

    # Redis set should not be called
    assert not set_called
