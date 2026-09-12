"""
Audio asset persistence — Cloudflare R2 (production) or local disk (dev).

Storage strategy
────────────────
When all four R2 credentials are set (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME), audio bytes are written to R2 and
the MongoDB document stores an object key prefixed with "r2:" so reads can
route to R2 rather than disk.

When R2 is not configured, files are written to `audio_storage_dir` on the
local filesystem and the MongoDB document stores the full file path as
before. This means the existing dev workflow and all tests continue to work
without any R2 credentials.

Read routing
────────────
  file_path starts with "r2:" → fetch from R2
  anything else               → read from local disk

This scheme means old records written to disk before R2 was configured
continue to be readable as long as the disk files exist. New records
written after R2 is configured always go to R2.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from functools import lru_cache, partial
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.models.audio import AudioAsset

logger = logging.getLogger("aloft.services.audio_repository")

# R2 object keys are stored in MongoDB prefixed with this string so read
# paths know to fetch from R2 rather than the local filesystem.
_R2_PREFIX = "r2:"


def _compress_audio_sync(input_bytes: bytes) -> bytes:
    """Compress MP3 to 64kbps — half size, same quality for speech.

    Blocking (subprocess.run) by design -- must only ever be called via
    _compress_audio()'s run_in_executor, never awaited directly from the
    event loop. See _compress_audio() for why.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", "pipe:0", "-b:a", "64k", "-f", "mp3", "pipe:1", "-loglevel", "quiet"],
            input=input_bytes,
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            return result.stdout
    except FileNotFoundError:
        # ffmpeg isn't installed -- expected unless it's been added to the
        # runtime image (see Dockerfile). Falls back to uncompressed audio,
        # which still works fine, just larger.
        logger.debug("ffmpeg not found -- skipping audio compression")
    except Exception:
        logger.warning("Audio compression failed -- falling back to uncompressed", exc_info=True)
    return input_bytes  # fall back to uncompressed


async def _compress_audio(input_bytes: bytes) -> bytes:
    """Async wrapper around _compress_audio_sync().

    subprocess.run() blocks the calling thread for the life of the ffmpeg
    process (up to the 30s timeout above). Calling it directly from an
    async function would freeze the *entire* event loop for that whole
    duration -- every other in-flight request stalls, not just this one.
    run_in_executor hands the blocking call to a worker thread so the loop
    stays free to serve other requests while ffmpeg runs.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _compress_audio_sync, input_bytes)


def _r2_object_key(poi_source_id: str, language: str, voice_name: str) -> str:
    """Return the R2 object key for an audio asset."""
    safe_id = poi_source_id.replace(":", "_")
    return f"audio/{safe_id}__{language}__{voice_name}.mp3"


def _local_file_path(poi_source_id: str, language: str, voice_name: str) -> Path:
    """Return the local filesystem path for an audio asset."""
    settings = get_settings()
    safe_id = poi_source_id.replace(":", "_")
    filename = f"{safe_id}__{language}__{voice_name}.mp3"
    return Path(settings.audio_storage_dir) / filename


@lru_cache(maxsize=1)
def _get_r2_client():
    """Return a cached boto3 S3 client pointed at the Cloudflare R2 endpoint.

    boto3 client construction is not free — it parses config, initialises
    request signers, and sets up connection pool state. Caching with
    lru_cache(maxsize=1) means a single client is reused across all save/read
    calls in the process lifetime. This is safe because settings are themselves
    lru_cache'd and don't change at runtime.
    """
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),  # type: ignore[union-attr]
        region_name="auto",
    )


async def save_audio(
    db: AsyncIOMotorDatabase,
    poi_source_id: str,
    language: str,
    voice_name: str,
    audio_bytes: bytes,
) -> AudioAsset:
    """Persist audio bytes to R2 (if configured) or local disk, record in MongoDB.

    Returns the saved AudioAsset. The `file_path` field contains either an
    "r2:<key>" reference or a local filesystem path depending on which
    backend was used.
    """
    settings = get_settings()

    # Compress audio before saving (runs ffmpeg in a worker thread so it
    # doesn't block the event loop -- see _compress_audio's docstring).
    audio_bytes = await _compress_audio(audio_bytes)

    if settings.r2_configured:
        key = _r2_object_key(poi_source_id, language, voice_name)
        try:
            r2 = _get_r2_client()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                partial(
                    r2.put_object,
                    Bucket=settings.r2_bucket_name,
                    Key=key,
                    Body=audio_bytes,
                    ContentType="audio/mpeg",
                ),
            )
            stored_path = f"{_R2_PREFIX}{key}"
            logger.debug("Stored audio in R2: %s", key)
        except (BotoCoreError, ClientError) as exc:
            # R2 failure is loud — a silent fallback to disk in production
            # would defeat the whole purpose of using R2.
            raise RuntimeError(f"Failed to upload audio to R2: {exc}") from exc
    else:
        file_path = _local_file_path(poi_source_id, language, voice_name)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(audio_bytes)
        stored_path = str(file_path)
        logger.debug("Stored audio on disk: %s", file_path)

    asset = AudioAsset(
        poi_source_id=poi_source_id,
        language=language,
        voice_name=voice_name,
        file_path=stored_path,
    )
    await db.audio_assets.update_one(
        {"poi_source_id": poi_source_id, "language": language, "voice_name": voice_name},
        {"$set": asset.to_mongo_dict()},
        upsert=True,
    )
    return asset


async def read_audio_bytes(asset: AudioAsset) -> bytes:
    """Fetch the raw MP3 bytes for an AudioAsset from wherever they are stored.

    Reads from R2 if `file_path` starts with "r2:", otherwise from disk.
    Raises FileNotFoundError / RuntimeError on failure.
    """
    if asset.file_path.startswith(_R2_PREFIX):
        key = asset.file_path[len(_R2_PREFIX) :]
        settings = get_settings()
        try:
            r2 = _get_r2_client()
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                partial(r2.get_object, Bucket=settings.r2_bucket_name, Key=key),
            )
            return response["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise RuntimeError(f"Failed to fetch audio from R2 (key={key}): {exc}") from exc
    else:
        path = Path(asset.file_path)
        if not path.exists():
            raise FileNotFoundError(f"Audio file not found on disk: {asset.file_path}")
        return path.read_bytes()


async def get_audio(
    db: AsyncIOMotorDatabase, poi_source_id: str, language: str, voice_name: str
) -> AudioAsset | None:
    """Look up an AudioAsset record in MongoDB. Returns None if not found."""
    doc = await db.audio_assets.find_one(
        {"poi_source_id": poi_source_id, "language": language, "voice_name": voice_name}
    )
    if doc is None:
        return None
    doc.pop("_id", None)
    return AudioAsset(**doc)
