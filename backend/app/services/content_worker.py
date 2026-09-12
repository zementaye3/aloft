"""
Processes content generation jobs from the Redis queue.

This runs as a separate async task started in main.py's lifespan --
not a separate process, just a background asyncio task that picks up
jobs while the main API handles requests normally.

Concurrency / rate limit strategy
──────────────────────────────────
POIs within a job are processed with bounded concurrency (up to
`content_generation_max_concurrent` in flight at once, same setting the
Groq client's own semaphore uses) instead of strictly one-at-a-time.
This used to be fully serial with a hardcoded 3s sleep between every
single POI -- safe, but slow: a 200-POI route took 10-15 minutes even
though nothing was actually rate-limited most of that time. The real
rate-limit protection lives one layer down, in each API client
(app/clients/groq.py, app/clients/tts.py, app/clients/wikipedia.py):
they already have their own semaphores and exponential backoff that
honours a 429's Retry-After header. Bounding concurrency here to the
same number those clients allow through gets a multi-POI speedup
without asking the worker to duplicate rate-limit logic the clients
already do correctly.

Within a single POI, image fetching doesn't depend on the generated
story (it only needs the POI's name), so it now runs concurrently with
story+audio generation instead of strictly after it.

- On 429:   back off (RATE_LIMIT_BACKOFF_SECONDS, exponential), retry
            up to MAX_POI_RETRIES times.
- On other error: log, mark POI failed, move to the next POI.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import redis.asyncio as redis
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.clients.groq import GroqClientError
from app.clients.tts import TtsClientError
from app.clients.wikipedia import WikipediaClientError, get_images
from app.core.config import get_settings
from app.services.audio_repository import get_audio, save_audio
from app.services.audio_service import get_voice_id_for_language, synthesize_story_audio
from app.services.content_job_service import (
    MAX_POI_RETRIES,
    RATE_LIMIT_BACKOFF_SECONDS,
    JobStatus,
    get_job_status,
    update_job_progress,
)
from app.services.poi_repository import get_poi, save_poi_images
from app.services.story_repository import get_story, save_story
from app.services.story_service import InsufficientFactsError, generate_story

logger = logging.getLogger("aloft.services.content_worker")


async def run_worker(
    redis_client: redis.Redis,
    db: AsyncIOMotorDatabase,
    http_client: httpx.AsyncClient,
) -> None:
    """Supervised worker loop. Restarts automatically on unexpected crashes.

    An inner _worker_loop() does the real work. This outer function catches
    any exception that leaks out of _worker_loop (which shouldn't happen —
    _worker_loop guards everything internally) and restarts after a short
    delay, so a freak bug never silently kills all content generation.
    CancelledError is NOT caught here so the graceful shutdown path
    (lifespan cancels the task) still works correctly.
    """
    logger.info("Content worker supervisor started")
    while True:
        try:
            await _worker_loop(redis_client, db, http_client)
        except asyncio.CancelledError:
            # Propagate cancellation so lifespan shutdown works correctly.
            logger.info("Content worker supervisor received cancellation — shutting down")
            raise
        except Exception:
            logger.exception("Content worker loop exited unexpectedly — restarting in 10 seconds")
            await asyncio.sleep(10)


async def _worker_loop(
    redis_client: redis.Redis,
    db: AsyncIOMotorDatabase,
    http_client: httpx.AsyncClient,
) -> None:
    """Main worker loop. Runs forever, picks up jobs from the queue.
    Called by run_worker's supervisor loop.
    """
    logger.info("Content worker started")
    while True:
        try:
            # BRPOP blocks up to 5 seconds waiting for a job --
            # returns None if nothing arrives, loops back and waits again.
            result = await redis_client.brpop("queue:content", timeout=5)
            if result is None:
                continue

            _, job_id = result
            job_id = job_id.decode() if isinstance(job_id, bytes) else job_id
            await _process_job(redis_client, db, http_client, job_id)

        except asyncio.CancelledError:
            logger.info("Content worker shutting down")
            raise
        except Exception:
            logger.exception("Unexpected error in content worker, continuing")
            await asyncio.sleep(5)


async def _process_job(
    redis_client: redis.Redis,
    db: AsyncIOMotorDatabase,
    http_client: httpx.AsyncClient,
    job_id: str,
) -> None:
    job = await get_job_status(redis_client, job_id)
    if job is None:
        logger.warning("Job %s not found in Redis", job_id)
        return

    poi_source_ids = job["poi_source_ids"]
    language = job["language"]

    await update_job_progress(redis_client, job_id, 0, 0, JobStatus.PROCESSING)
    logger.info("Processing job %s: %d POIs, language=%s", job_id, len(poi_source_ids), language)

    settings = get_settings()
    concurrency = max(1, settings.content_generation_max_concurrent)
    semaphore = asyncio.Semaphore(concurrency)

    # Mutable counters shared across concurrent tasks -- guarded by a lock
    # since multiple POIs can finish at (almost) the same instant.
    counts = {"completed": 0, "failed": 0}
    counts_lock = asyncio.Lock()

    async def _run_one(source_id: str) -> None:
        async with semaphore:
            success = await _process_one_poi_with_retry(
                http_client, db, source_id, language
            )
        async with counts_lock:
            if success:
                counts["completed"] += 1
            else:
                counts["failed"] += 1
            await update_job_progress(
                redis_client, job_id, counts["completed"], counts["failed"], JobStatus.PROCESSING
            )

    await asyncio.gather(*(_run_one(source_id) for source_id in poi_source_ids))

    # Job is COMPLETED even if some POIs failed -- we processed everything we could
    final_status = JobStatus.COMPLETED
    await update_job_progress(
        redis_client, job_id, counts["completed"], counts["failed"], final_status
    )
    logger.info(
        "Job %s done: %d completed, %d failed", job_id, counts["completed"], counts["failed"]
    )


async def _process_one_poi_with_retry(
    http_client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    source_id: str,
    language: str,
) -> bool:
    """Process one POI with exponential backoff on rate limits and errors."""
    backoff_base = RATE_LIMIT_BACKOFF_SECONDS

    for attempt in range(1, MAX_POI_RETRIES + 1):
        try:
            await _process_one_poi(http_client, db, source_id, language)
            return True
        except GroqClientError as exc:
            error_str = str(exc)
            if "429" in error_str or "rate" in error_str.lower():
                backoff_time = backoff_base * (2 ** (attempt - 1))  # Exponential backoff
                logger.warning(
                    "Groq rate limit on %s attempt %d/%d -- backing off %.1fs",
                    source_id,
                    attempt,
                    MAX_POI_RETRIES,
                    backoff_time,
                )
                if attempt < MAX_POI_RETRIES:
                    await asyncio.sleep(backoff_time)
                    continue
            logger.warning("Groq error on %s (non-retryable): %s", source_id, exc)
            return False
        except WikipediaClientError as exc:
            error_str = str(exc)
            if "429" in error_str:
                backoff_time = backoff_base * (2 ** (attempt - 1))
                logger.warning(
                    "Wikipedia rate limit on %s attempt %d/%d -- backing off %.1fs",
                    source_id,
                    attempt,
                    MAX_POI_RETRIES,
                    backoff_time,
                )
                if attempt < MAX_POI_RETRIES:
                    await asyncio.sleep(backoff_time)
                    continue
            logger.warning("Wikipedia error on %s: %s", source_id, exc)
            return False
        except TtsClientError as exc:
            logger.warning("TTS error on %s: %s -- story saved, no audio", source_id, exc)
            return False
        except InsufficientFactsError:
            # Not worth retrying -- no Wikipedia article means no article.
            return False
        except Exception as exc:
            logger.exception(
                "Unexpected error on %s attempt %d/%d: %s", source_id, attempt, MAX_POI_RETRIES, exc
            )
            if attempt < MAX_POI_RETRIES:
                backoff_time = backoff_base * (2 ** (attempt - 1))
                await asyncio.sleep(backoff_time)
                continue
            return False
    return False


async def _process_one_poi(
    http_client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    source_id: str,
    language: str,
) -> None:
    """Generate story + audio + images for one POI if not already cached.

    Image fetching only needs the POI's name (known up front), not the
    generated story, so it runs concurrently with story+audio generation
    via asyncio.gather rather than strictly after it.
    """
    poi = await get_poi(db, source_id)
    if poi is None:
        return

    async def _story_and_audio() -> None:
        # Story -- skip if already exists
        story = await get_story(db, source_id, language)
        if story is None:
            story = await generate_story(http_client, source_id, poi.name, language=language)
            await save_story(db, story)

        # Audio -- skip if already exists
        voice_id = get_voice_id_for_language(language)
        existing_audio = await get_audio(db, source_id, language, voice_id)
        if existing_audio is None:
            try:
                audio_bytes = await synthesize_story_audio(
                    story.text_content, language=language, http_client=http_client
                )
                await save_audio(db, source_id, language, voice_id, audio_bytes)
            except TtsClientError as exc:
                logger.warning("TTS failed for %s: %s -- story saved, no audio", source_id, exc)

    async def _images() -> None:
        # Re-fetch the POI in case images were added concurrently (e.g. by
        # a discover_pois?auto_images=true call) since we started.
        current = await get_poi(db, source_id)
        if current is not None and not current.image_refs:
            images = await get_images(http_client, current.name)
            await save_poi_images(db, source_id, [img.url for img in images])

    results = await asyncio.gather(_story_and_audio(), _images(), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
