from __future__ import annotations

import asyncio
import logging
from typing import Literal

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.clients.groq import GroqClientError
from app.clients.openverse import OpenverseClientError
from app.clients.openverse import search_images as openverse_search_images
from app.clients.tts import TtsClientError
from app.clients.wikipedia import WikipediaClientError, get_images
from app.services.audio_repository import get_audio, save_audio
from app.services.audio_service import get_voice_id_for_language, synthesize_story_audio
from app.services.poi_repository import get_poi, save_poi_images
from app.services.story_repository import get_story, save_story
from app.services.story_service import InsufficientFactsError, generate_story

logger = logging.getLogger("aloft.services.content_generation")


class PoiContentResult(BaseModel):
    poi_source_id: str
    story_ready: bool
    audio_ready: bool
    images_found: int
    # None  → image fetching was not attempted or POI itself was missing
    # ""    → attempted, found nothing from either source
    # "wikipedia" | "openverse" | "cached" → source that provided images
    image_source: Literal["wikipedia", "openverse", "cached", ""] | None = None
    error: str | None = None


async def generate_content_for_route(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    poi_source_ids: list[str],
    language: str = "en",
    max_concurrent: int | None = None,
    skip_audio: bool = False,
) -> list[PoiContentResult]:
    """Generate stories and audio for every POI concurrently.

    max_concurrent limits simultaneous Groq + ElevenLabs calls so we don't
    blast the free-tier rate limits. Defaults to settings.content_generation_max_concurrent
    (3 for free-tier accounts; raise it in settings for paid plans).

    skip_audio skips audio synthesis (useful when ElevenLabs quota is exhausted).
    """
    from app.core.config import get_settings

    concurrency = (
        max_concurrent
        if max_concurrent is not None
        else get_settings().content_generation_max_concurrent
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(source_id: str) -> PoiContentResult:
        async with semaphore:
            return await _generate_content_for_one_poi(client, db, source_id, language, skip_audio)

    return list(await asyncio.gather(*[_bounded(sid) for sid in poi_source_ids]))


async def _generate_content_for_one_poi(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    source_id: str,
    language: str,
    skip_audio: bool = False,
) -> PoiContentResult:
    poi = await get_poi(db, source_id)
    if poi is None:
        return PoiContentResult(
            poi_source_id=source_id,
            story_ready=False,
            audio_ready=False,
            images_found=0,
            image_source=None,
            error="POI was never discovered",
        )

    story_ready, audio_ready, error = await _ensure_story_and_audio(
        client, db, source_id, poi.name, language, skip_audio
    )

    images_found = 0
    image_source: Literal["wikipedia", "openverse", ""] | None = ""

    # --- Check cache before calling Wikipedia ---
    if poi.image_refs:
        # Images already exist, skip Wikipedia call entirely
        images_found = len(poi.image_refs)
        image_source = "cached"
    else:
        # --- Primary: Wikipedia images ---
        try:
            images = await get_images(client, poi.name)
            if images:
                await save_poi_images(db, source_id, [image.url for image in images])
                images_found = len(images)
                image_source = "wikipedia"
        except WikipediaClientError as exc:
            logger.warning("Wikipedia image fetch failed for %s: %s", source_id, exc)
            error = error or f"Wikipedia image fetch failed: {exc}"

    # --- Fallback: Openverse ---
    if images_found == 0:
        try:
            ov_images = await openverse_search_images(client, poi.name)
            if ov_images:
                await save_poi_images(db, source_id, [img.url for img in ov_images])
                images_found = len(ov_images)
                image_source = "openverse"
        except OpenverseClientError as exc:
            logger.debug("Openverse fallback failed for %s: %s", source_id, exc)

    return PoiContentResult(
        poi_source_id=source_id,
        story_ready=story_ready,
        audio_ready=audio_ready,
        images_found=images_found,
        image_source=image_source,
        error=error,
    )


async def _ensure_story_and_audio(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    source_id: str,
    poi_name: str,
    language: str,
    skip_audio: bool = False,
) -> tuple[bool, bool, str | None]:
    try:
        story = await get_story(db, source_id, language)
        if story is None:
            story = await generate_story(client, source_id, poi_name, language=language)
            await save_story(db, story)
    except InsufficientFactsError as exc:
        return False, False, f"Skipped: {exc}"
    except (WikipediaClientError, GroqClientError) as exc:
        logger.warning("Story generation failed for %s: %s", source_id, exc)
        return False, False, f"Story generation failed: {exc}"

    if skip_audio:
        logger.info("Skipping audio synthesis for %s (skip_audio=true)", source_id)
        return True, False, "Audio synthesis skipped (skip_audio=true)"

    voice_id = get_voice_id_for_language(language)
    try:
        existing_audio = await get_audio(db, source_id, language, voice_id)
        if existing_audio is None:
            audio_bytes = await synthesize_story_audio(
                story.text_content, language=language, http_client=client
            )
            await save_audio(db, source_id, language, voice_id, audio_bytes)
    except TtsClientError as exc:
        logger.warning("Audio synthesis failed for %s: %s", source_id, exc)
        return True, False, f"Audio synthesis failed: {exc}"

    return True, True, None
