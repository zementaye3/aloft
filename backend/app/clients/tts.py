"""
ElevenLabs text-to-speech client.

Uses ElevenLabs' /v1/text-to-speech/{voice_id} endpoint directly via
httpx -- the same thin-wrapper pattern as every other client in this app.
No credit card required on the free tier (10,000 chars/month).

Auth: a plain API key in the xi-api-key header. No OAuth, no service
account JSON, no Application Default Credentials -- just set
ELEVENLABS_API_KEY in your .env file.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.api_key_rotation import ApiKeyRotationManager, is_key_exhausted, mark_key_exhausted
from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.tts")

_BASE_URL = "https://api.elevenlabs.io"

# Transient server-side failures worth retrying.
# 401 (bad key) and 422 (bad voice ID / invalid input) are not retried --
# they will fail identically every time.
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

# Module-level rotation manager cache
_rotation_manager = None


def _get_rotation_manager():
    """Get or create the API key rotation manager for ElevenLabs."""
    global _rotation_manager
    if _rotation_manager is None:
        settings = get_settings()
        # Handle both the property (real Settings) and direct attribute (mocked Settings in tests)
        api_keys = getattr(settings, "elevenlabs_api_keys", None)
        if api_keys is None:
            # Fallback for tests that mock Settings without the property
            api_key = settings.elevenlabs_api_key
            api_keys = [api_key.get_secret_value()] if api_key else []
        if not api_keys:
            logger.warning("No ElevenLabs API keys configured for rotation")
        _rotation_manager = ApiKeyRotationManager("elevenlabs", api_keys)
    return _rotation_manager


def reset_rotation_manager_cache() -> None:
    """Clear the cached rotation manager so it's rebuilt from current settings.

    See app/clients/groq.py's reset_rotation_manager_cache() for why this
    exists — same module-level-cache-survives-across-tests problem.
    """
    global _rotation_manager
    _rotation_manager = None


class TtsClientError(Exception):
    """Raised when speech synthesis fails -- after retries are exhausted,
    or on a non-retryable error (bad API key, invalid voice ID).
    """


async def synthesize_speech(
    text: str,
    *,
    voice_id: str | None = None,
    http_client: httpx.AsyncClient,
) -> bytes:
    """Convert text to MP3 audio bytes using ElevenLabs.

    Args:
        text: the text to narrate.
        voice_id: ElevenLabs voice ID. Defaults to settings.elevenlabs_voice_id.
            Find voice IDs at elevenlabs.io/voice-lab or via GET /v1/voices.
        http_client: shared httpx client from ``app.state.http_client``.
            Callers **must** provide this — creating a new client per call
            would bypass connection pooling and leak sockets.

    Returns:
        Raw MP3 audio bytes.

    Raises:
        TtsClientError: bad API key, invalid voice ID, or retries exhausted.
    """
    settings = get_settings()
    rotation_manager = _get_rotation_manager()

    # Get API keys from rotation manager or fall back to single key
    api_keys = rotation_manager.api_keys if rotation_manager.api_keys else (
        [settings.elevenlabs_api_key.get_secret_value()] if settings.elevenlabs_api_key else []
    )

    if not api_keys:
        raise TtsClientError("ELEVENLABS_API_KEY is not set. Add it to your .env file.")

    resolved_voice_id = voice_id or settings.elevenlabs_voice_id
    url = f"{_BASE_URL}/v1/text-to-speech/{resolved_voice_id}"

    payload = {
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "output_format": settings.elevenlabs_output_format,
    }

    # last_error is set inside the per-key loop below. If every key is
    # skipped as pre-exhausted, the loop body never runs -- keep this
    # defined up front so the final `raise ... from last_error` can't hit
    # an UnboundLocalError in that case.
    last_error: Exception | None = None

    # Try each available API key
    for api_key in api_keys:
        # Skip if this key is marked as exhausted (only when using multiple keys)
        if len(api_keys) > 1 and await is_key_exhausted("elevenlabs", api_key):
            logger.debug("Skipping exhausted ElevenLabs API key")
            continue

        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }

        should_mark_exhausted = False

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await http_client.post(url, headers=headers, json=payload, timeout=30.0)
            except httpx.RequestError as exc:
                last_error = exc
                logger.warning("TTS request error, attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc)
            else:
                if response.status_code == 200:
                    return response.content

                if response.status_code in _RETRYABLE_STATUS_CODES:
                    last_error = TtsClientError(
                        f"ElevenLabs returned {response.status_code}: {response.text}"
                    )
                    # Mark as exhausted if we get 429 (rate limit/quota errors)
                    if response.status_code == 429:
                        should_mark_exhausted = True
                    logger.warning(
                        "TTS retryable error %d, attempt %d/%d",
                        response.status_code,
                        attempt,
                        _MAX_ATTEMPTS,
                    )
                else:
                    # 401, 422, etc. -- won't succeed on retry
                    raise TtsClientError(
                        f"TTS synthesis failed (non-retryable): "
                        f"HTTP {response.status_code}: {response.text}"
                    )

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

        # If we got here, this key failed all retries - mark it as exhausted if using rotation
        # and we encountered quota/rate limit errors
        if len(api_keys) > 1 and should_mark_exhausted:
            logger.warning("Marking ElevenLabs API key as exhausted after quota/rate limit errors")
            await mark_key_exhausted("elevenlabs", api_key)
        # Continue to next key if using rotation
        if len(api_keys) > 1:
            continue
        # If not using rotation and key failed, raise error
        break

    if last_error is None:
        raise TtsClientError(
            "TTS synthesis failed: all configured ElevenLabs API keys are currently "
            "marked exhausted (cooling down after a previous quota/rate-limit error)."
        )
    raise TtsClientError(
        f"TTS synthesis failed after {_MAX_ATTEMPTS} attempts across all API keys"
    ) from last_error
