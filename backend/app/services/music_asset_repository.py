"""
Background music file resolution — local disk (dev) or R2 (fetch-and-cache).

Why this exists
================
music_assets/*.wav|*.flac|*.mp3 used to be committed directly to git
(~109MB), which is what bloated every clone of this repo. They're now
gitignored and no longer copied into the Docker image. Instead:

  1. Run scripts/upload_music_assets_to_r2.py once, locally, to push the
     real files to R2 under the "static/music/" prefix.
  2. At runtime, resolve_music_file() checks local disk first (this is what
     lets local dev keep working if you still have the files in
     music_assets/ without touching R2 at all), then falls back to
     downloading from R2 and caching to local disk so subsequent requests
     for the same track are instant.

This mirrors the same R2-vs-disk pattern already used for narration audio
in app/services/audio_repository.py, just applied to the small, fixed set
of static music tracks instead of per-POI generated audio.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache, partial
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import get_settings
from app.services.music_catalog import MusicTrack

logger = logging.getLogger("aloft.services.music_asset_repository")

_R2_MUSIC_PREFIX = "static/music"


class MusicAssetError(Exception):
    """Raised when a music track can't be resolved from either disk or R2."""


@lru_cache(maxsize=1)
def _get_r2_client():
    """Reuses the same lazy-cached-client pattern as audio_repository.py."""
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),  # type: ignore[union-attr]
        region_name="auto",
    )


def _local_path(track: MusicTrack) -> Path:
    return Path("music_assets") / track.local_filename


async def resolve_music_file(track: MusicTrack) -> Path:
    """Return a local filesystem path to the track's audio file, downloading
    and caching from R2 first if it isn't already on disk.

    Raises MusicAssetError if the file isn't available locally and R2 either
    isn't configured or doesn't have the track (run
    scripts/upload_music_assets_to_r2.py to fix the latter).
    """
    local_path = _local_path(track)
    if local_path.exists():
        return local_path

    settings = get_settings()
    if not settings.r2_configured:
        raise MusicAssetError(
            f"Music file '{track.local_filename}' not found locally at {local_path}, "
            "and R2 is not configured to fetch it. Either place the file at that path "
            "manually, or configure R2 and run scripts/upload_music_assets_to_r2.py."
        )

    key = f"{_R2_MUSIC_PREFIX}/{track.local_filename}"
    try:
        r2 = _get_r2_client()
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            partial(r2.get_object, Bucket=settings.r2_bucket_name, Key=key),
        )
        data = response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        raise MusicAssetError(
            f"Music file '{track.local_filename}' not found locally and the R2 fetch "
            f"failed (key={key}): {exc}. Run scripts/upload_music_assets_to_r2.py to "
            "upload the track catalog to R2 first."
        ) from exc

    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_bytes(data)
    logger.info("Cached music track '%s' from R2 to %s", track.track_id, local_path)
    return local_path
