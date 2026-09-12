from unittest.mock import ANY, AsyncMock

import pytest

from app.services.audio_service import (
    get_voice_id_for_language,
    synthesize_story_audio,
)

_BELLA = "EXAVITQu4vr4xnSDxMaL"  # default free tier voice (multilingual)


@pytest.mark.asyncio
async def test_forwards_text_to_synthesize_speech(monkeypatch):
    mock_synth = AsyncMock(return_value=b"audio bytes")
    monkeypatch.setattr("app.services.audio_service.synthesize_speech", mock_synth)

    await synthesize_story_audio("Some text")

    # Default English: resolves to Bella (the global default for free tier)
    mock_synth.assert_awaited_once_with("Some text", voice_id=_BELLA, http_client=ANY)


@pytest.mark.asyncio
async def test_passes_through_explicit_voice_id(monkeypatch):
    mock_synth = AsyncMock(return_value=b"audio bytes")
    monkeypatch.setattr("app.services.audio_service.synthesize_speech", mock_synth)

    await synthesize_story_audio("Text", voice_id="AZnzlk1XvdvUeBnXmlld")

    mock_synth.assert_awaited_once_with("Text", voice_id="AZnzlk1XvdvUeBnXmlld", http_client=ANY)


@pytest.mark.asyncio
async def test_language_falls_back_to_bella_for_unsupported(monkeypatch):
    """Languages not in _LANGUAGE_VOICE_DEFAULTS use the Bella fallback."""
    mock_synth = AsyncMock(return_value=b"audio bytes")
    monkeypatch.setattr("app.services.audio_service.synthesize_speech", mock_synth)

    await synthesize_story_audio("مرحباً", language="ar")  # Arabic -- not in defaults

    # Falls back to Bella since "ar" is not in _LANGUAGE_VOICE_DEFAULTS
    mock_synth.assert_awaited_once_with("مرحباً", voice_id=_BELLA, http_client=ANY)


@pytest.mark.asyncio
async def test_unsupported_language_falls_back_to_bella(monkeypatch):
    """A language not in _LANGUAGE_VOICE_DEFAULTS uses the Bella fallback."""
    mock_synth = AsyncMock(return_value=b"audio bytes")
    monkeypatch.setattr("app.services.audio_service.synthesize_speech", mock_synth)

    await synthesize_story_audio("Hei verden", language="no")  # Norwegian -- not in defaults

    # Falls back to Bella since "no" is not in _LANGUAGE_VOICE_DEFAULTS
    mock_synth.assert_awaited_once_with("Hei verden", voice_id=_BELLA, http_client=ANY)


@pytest.mark.asyncio
async def test_explicit_voice_id_overrides_language_default(monkeypatch):
    """Explicit voice_id always wins over language-based lookup."""
    mock_synth = AsyncMock(return_value=b"audio bytes")
    monkeypatch.setattr("app.services.audio_service.synthesize_speech", mock_synth)

    custom_voice = "AZnzlk1XvdvUeBnXmlld"
    await synthesize_story_audio("مرحباً", language="ar", voice_id=custom_voice)

    # custom_voice wins despite language="ar"
    mock_synth.assert_awaited_once_with("مرحباً", voice_id=custom_voice, http_client=ANY)


def test_get_voice_id_for_language_arabic_falls_back_to_bella():
    # Arabic not in defaults, falls back to Bella
    assert get_voice_id_for_language("ar") == _BELLA


def test_get_voice_id_for_language_french_falls_back_to_bella():
    # French not in defaults, falls back to Bella
    assert get_voice_id_for_language("fr") == _BELLA


def test_get_voice_id_for_language_english_returns_bella():
    assert get_voice_id_for_language("en") == _BELLA
