from __future__ import annotations

import httpx

from app.clients.tts import synthesize_speech
from app.core.config import get_settings

# Per-language voice overrides.
#
# eleven_multilingual_v2 handles all 29 supported languages from a single
# voice ID -- it detects language from the text automatically, no locale
# parameter needed. The default (Bella, EXAVITQu4vr4xnSDxMaL) therefore
# works for every language the app supports.
#
# Language-specific voices require paid ElevenLabs plans.
# For free tier accounts, use the default Bella voice which supports
# multilingual text-to-speech.
#
# To verify or update these IDs:
#   GET https://api.elevenlabs.io/v1/voices
#   (no auth needed for premade voices; xi-api-key header for your cloned voices)
#
# To use a different voice for a language, set ELEVENLABS_VOICE_ID_{LANG}
# in your .env (e.g. ELEVENLABS_VOICE_ID_AR=...). If unset, falls back to
# the per-language default below, then the global default from settings.
_LANGUAGE_VOICE_DEFAULTS: dict[str, str] = {
    # All languages use the default free tier voice (Bella)
    # Language-specific voices require paid plans
}


def get_voice_id_for_language(language: str) -> str:
    """Return the best voice ID for the given BCP-47 language code.

    Priority order:
    1. ELEVENLABS_VOICE_ID env var override (global default from settings)
       -- only used if it's been changed from the default Bella ID, which
       signals the operator intentionally picked a specific voice.
    2. Per-language voice from _LANGUAGE_VOICE_DEFAULTS (empty for free tier).
    3. Bella (EXAVITQu4vr4xnSDxMaL) -- the global fallback (free tier).

    This means a fresh install with no .env changes gets the free tier voice
    which supports multilingual text-to-speech via eleven_multilingual_v2.
    """
    settings = get_settings()
    _BELLA_DEFAULT = "EXAVITQu4vr4xnSDxMaL"

    # If operator has explicitly set a custom global voice, respect it.
    if settings.elevenlabs_voice_id != _BELLA_DEFAULT:
        return settings.elevenlabs_voice_id

    # Use per-language default if available, else fall back to Bella.
    return _LANGUAGE_VOICE_DEFAULTS.get(language, _BELLA_DEFAULT)


async def synthesize_story_audio(
    text: str,
    language: str = "en",
    voice_id: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> bytes:
    """Generate MP3 audio for a story using ElevenLabs.

    eleven_multilingual_v2 detects language from the text -- no locale
    parameter is sent to the API. The voice_id determines accent and
    character; language-appropriate defaults are selected via
    get_voice_id_for_language() unless voice_id is explicitly provided.

    Args:
        text: Story text to narrate.
        language: BCP-47 code used to pick a native-accented voice
            (see _LANGUAGE_VOICE_DEFAULTS). Has no effect if voice_id
            is supplied directly.
        voice_id: Explicit voice ID override -- skips language lookup.
        http_client: Shared httpx client from app.state.http_client.
            If not provided a temporary client is created for this call
            (acceptable in tests; production callers should always inject
            the shared client to benefit from connection pooling).
    """
    resolved_voice_id = voice_id or get_voice_id_for_language(language)
    if http_client is not None:
        return await synthesize_speech(text, voice_id=resolved_voice_id, http_client=http_client)
    # Fallback: create a one-shot client (tests / ad-hoc callers only).
    async with httpx.AsyncClient() as tmp_client:
        return await synthesize_speech(text, voice_id=resolved_voice_id, http_client=tmp_client)
