from __future__ import annotations

import io
import json
import logging
import zipfile

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.services.audio_repository import get_audio, read_audio_bytes
from app.services.audio_service import get_voice_id_for_language
from app.services.poi_repository import get_poi
from app.services.route_bundle_repository import get_route_bundle
from app.services.story_repository import get_story

logger = logging.getLogger("aloft.services.download")


class RouteNotFoundError(Exception):
    pass


class ZipTooLargeError(Exception):
    """Raised when the assembled ZIP would exceed MAX_ZIP_BYTES."""

    pass


# Hard ceiling on the in-memory ZIP buffer.
# A typical route with 20 POIs × ~200KB audio + 4 images ~100KB each is
# roughly 12MB.  50MB gives comfortable headroom while preventing a runaway
# request (hundreds of POIs with multi-MB audio files) from exhausting the
# process's RAM on free-tier infrastructure (Render, Railway, etc.).
MAX_ZIP_BYTES = 50 * 1024 * 1024  # 50 MB


async def build_download_zip(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    route_key: str,
    language: str = "en",
    voice_name: str | None = None,
    include_images: bool = True,
) -> bytes:
    """Package already-generated content into a ZIP.

    Raises RouteNotFoundError if route_key doesn't match a known route.
    Run POST /routes/{route_key}/content first to maximise what's ready.
    """
    bundle = await get_route_bundle(db, route_key)
    if bundle is None:
        raise RouteNotFoundError(f"No route found for route_key '{route_key}'")

    resolved_voice = voice_name or get_voice_id_for_language(language)
    manifest_entries = []
    buf = io.BytesIO()

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for source_id in bundle.poi_source_ids:
            entry = await _package_poi(
                client, db, zf, source_id, language, resolved_voice, include_images
            )
            if entry is not None:
                manifest_entries.append(entry)

            # Check size after each POI to catch runaway bundles early.
            # buf.seek(0, 2) moves to the true end of the BytesIO buffer;
            # buf.tell() then returns the real byte count. This is more reliable
            # than bare buf.tell() because ZipFile internally buffers writes and
            # the position may lag behind actual bytes written until flushed.
            buf.seek(0, 2)
            if buf.tell() > MAX_ZIP_BYTES:
                raise ZipTooLargeError(
                    f"ZIP bundle exceeded {MAX_ZIP_BYTES // (1024 * 1024)}MB limit "
                    f"after {len(manifest_entries)} POIs. Run content generation "
                    "for fewer POIs or reduce audio/image count."
                )

        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "route_key": route_key,
                    "departure": list(bundle.departure),
                    "arrival": list(bundle.arrival),
                    "language": language,
                    "voice_name": resolved_voice,
                    "poi_count": len(manifest_entries),
                    "pois": manifest_entries,
                },
                indent=2,
            ),
        )

    return buf.getvalue()


async def _package_poi(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    zf: zipfile.ZipFile,
    source_id: str,
    language: str,
    voice_name: str,
    include_images: bool,
) -> dict | None:
    poi = await get_poi(db, source_id)
    if poi is None:
        logger.warning("Skipping %s — POI not found", source_id)
        return None

    story = await get_story(db, source_id, language)
    if story is None:
        logger.warning("Skipping %s — no story yet", source_id)
        return None

    safe_id = source_id.replace(":", "_")
    entry: dict = {
        "source_id": source_id,
        "name": poi.name,
        "location": poi.location,
        "text_content": story.text_content,
        "audio_file": None,
        "image_files": [],
    }

    audio = await get_audio(db, source_id, language, voice_name)
    if audio is not None:
        try:
            audio_data = await read_audio_bytes(audio)
            filename = f"audio/{safe_id}.mp3"
            zf.writestr(filename, audio_data)
            entry["audio_file"] = filename
        except (FileNotFoundError, RuntimeError) as exc:
            logger.warning("%s has no audio — bundling text only (%s)", source_id, exc)
    else:
        logger.warning("%s has no audio — bundling text only", source_id)

    if include_images and poi.image_refs:
        entry["image_files"] = await _fetch_images(client, zf, poi.image_refs, safe_id)

    return entry


async def _fetch_images(
    client: httpx.AsyncClient, zf: zipfile.ZipFile, urls: list[str], safe_id: str
) -> list[str]:
    filenames = []
    for i, url in enumerate(urls):
        try:
            resp = await client.get(url, timeout=10.0)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Skipping image %s: %s", url, exc)
            continue
        ext = "jpg" if url.lower().endswith((".jpg", ".jpeg")) else "png"
        filename = f"images/{safe_id}_{i}.{ext}"
        zf.writestr(filename, resp.content)
        filenames.append(filename)
    return filenames
