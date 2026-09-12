"""
Background content generation using Redis as a simple job queue.
No Celery, no RQ, no extra dependencies -- Redis already exists in
the stack for rate limiting and verification tokens.

Job lifecycle:
  pending → processing → completed | failed

Key format:
  job:{job_id}           → job metadata (status, progress, errors)
  queue:content          → Redis list of pending job_ids (LPUSH/BRPOP)
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from enum import Enum

import redis.asyncio as redis

from app.core.config import get_settings

logger = logging.getLogger("aloft.services.content_job")

# Load settings for configurable delays
settings = get_settings()
INTER_POI_DELAY_SECONDS = settings.content_inter_poi_delay_seconds
INTER_IMAGE_DELAY_SECONDS = settings.content_inter_image_delay_seconds
RATE_LIMIT_BACKOFF_SECONDS = settings.content_rate_limit_backoff_seconds
MAX_POI_RETRIES = settings.content_max_poi_retries


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


async def create_content_job(
    redis_client: redis.Redis,
    route_key: str,
    poi_source_ids: list[str],
    language: str = "en",
) -> str:
    """Create a new content generation job. Returns the job_id immediately."""
    job_id = str(uuid.uuid4())
    job_data = {
        "job_id": job_id,
        "route_key": route_key,
        "poi_source_ids": poi_source_ids,
        "language": language,
        "status": JobStatus.PENDING,
        "total": len(poi_source_ids),
        "completed": 0,
        "failed": 0,
        "errors": [],
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
    }

    # Store job metadata
    await redis_client.set(
        f"job:{job_id}",
        json.dumps(job_data),
        ex=86400,  # 24 hour TTL -- jobs don't need to live forever
    )

    # Push to the queue
    await redis_client.lpush("queue:content", job_id)

    logger.info(
        "Created content job %s for route %s (%d POIs)", job_id, route_key, len(poi_source_ids)
    )
    return job_id


async def get_job_status(redis_client: redis.Redis, job_id: str) -> dict | None:
    """Get current job status and progress."""
    data = await redis_client.get(f"job:{job_id}")
    if data is None:
        return None
    return json.loads(data)


async def update_job_progress(
    redis_client: redis.Redis,
    job_id: str,
    completed: int,
    failed: int,
    status: JobStatus,
    error: str | None = None,
) -> None:
    data = await redis_client.get(f"job:{job_id}")
    if data is None:
        return
    job = json.loads(data)
    job["completed"] = completed
    job["failed"] = failed
    job["status"] = status
    job["updated_at"] = datetime.now(UTC).isoformat()
    if error:
        job["errors"].append(error)
    await redis_client.set(f"job:{job_id}", json.dumps(job), ex=86400)
