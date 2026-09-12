"""
Tests for the ElevenLabs TTS client.

Uses respx to intercept httpx calls -- the same pattern as the Wikipedia
and AviationStack client tests. No Google imports, no gRPC mocking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.tts import TtsClientError, synthesize_speech
from app.core.config import get_settings

_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
_FAKE_API_KEY = "sk_test_fake_key_for_tests"
_TTS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{_VOICE_ID}"


@pytest.fixture(autouse=True)
def no_real_sleep():
    with patch("app.clients.tts.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def fake_api_key(monkeypatch):
    """Inject a fake API key so tests never fail on missing key.
    Clears the lru_cache before and after so the patched value is picked up.
    """
    get_settings.cache_clear()
    monkeypatch.setenv("ELEVENLABS_API_KEY", _FAKE_API_KEY)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def http_client():
    """A real httpx.AsyncClient whose traffic respx will intercept."""
    return httpx.AsyncClient()


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_speech_returns_audio_bytes(http_client):
    respx.post(_TTS_URL).mock(return_value=httpx.Response(200, content=b"fake-mp3-bytes"))

    audio = await synthesize_speech(
        "A cathedral rises above the hills.",
        voice_id=_VOICE_ID,
        http_client=http_client,
    )

    assert audio == b"fake-mp3-bytes"


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_speech_sends_correct_headers(http_client):
    route = respx.post(_TTS_URL).mock(return_value=httpx.Response(200, content=b"audio"))

    await synthesize_speech("Some text", voice_id=_VOICE_ID, http_client=http_client)

    assert route.called
    sent = route.calls[0].request
    assert sent.headers["xi-api-key"] == _FAKE_API_KEY
    assert sent.headers["Accept"] == "audio/mpeg"


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_speech_allows_overriding_voice_id(http_client):
    custom_voice = "AZnzlk1XvdvUeBnXmlld"
    custom_url = f"https://api.elevenlabs.io/v1/text-to-speech/{custom_voice}"
    respx.post(custom_url).mock(return_value=httpx.Response(200, content=b"custom-audio"))

    audio = await synthesize_speech("Some text", voice_id=custom_voice, http_client=http_client)

    assert audio == b"custom-audio"


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_speech_retries_on_503_then_succeeds(http_client):
    respx.post(_TTS_URL).mock(
        side_effect=[
            httpx.Response(503, text="temporarily unavailable"),
            httpx.Response(200, content=b"recovered audio"),
        ]
    )

    audio = await synthesize_speech("Some text", voice_id=_VOICE_ID, http_client=http_client)

    assert audio == b"recovered audio"


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_speech_raises_after_exhausting_retries(http_client):
    respx.post(_TTS_URL).mock(return_value=httpx.Response(503, text="still down"))

    with pytest.raises(TtsClientError, match="3 attempts"):
        await synthesize_speech("Some text", voice_id=_VOICE_ID, http_client=http_client)


@pytest.mark.asyncio
@respx.mock
async def test_synthesize_speech_does_not_retry_invalid_argument(http_client):
    route = respx.post(_TTS_URL).mock(
        return_value=httpx.Response(422, json={"detail": "invalid voice_id"})
    )

    with pytest.raises(TtsClientError, match="non-retryable"):
        await synthesize_speech("Some text", voice_id=_VOICE_ID, http_client=http_client)

    # 422 is non-retryable -- called exactly once
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_synthesize_speech_raises_when_api_key_not_set(monkeypatch):
    """When ELEVENLABS_API_KEY is absent, the client raises immediately
    with a clear message before making any request.
    """
    from app.core.config import Settings

    # Patch get_settings to return a settings object with no API key,
    # bypassing .env file loading entirely -- delenv alone isn't enough
    # because pydantic-settings also reads the .env file on disk.
    fake_settings = Settings.model_construct(
        elevenlabs_api_key=None,
        elevenlabs_voice_id="21m00Tcm4TlvDq8ikWAM",
    )
    monkeypatch.setattr("app.clients.tts.get_settings", lambda: fake_settings)

    with pytest.raises(TtsClientError, match="ELEVENLABS_API_KEY"):
        async with httpx.AsyncClient() as client:
            await synthesize_speech("Some text", voice_id=_VOICE_ID, http_client=client)
