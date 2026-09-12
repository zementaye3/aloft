"""
Test API key rotation functionality.

This test verifies that the API key rotation system works correctly
when services return quota/rate limit errors.

IMPORTANT: the Redis client used in production is redis.asyncio.Redis --
every method call (.exists, .setex, .delete, .scan_iter, .ttl, .get) is a
coroutine/async-iterator, not a plain return value. The mock_redis fixture
below uses AsyncMock (not Mock) for exactly that reason: a plain Mock lets
a test pass even if production code forgets to `await` the call (a
coroutine object is truthy, so a forgotten `await` on `.exists()` would
silently make `is_key_exhausted()` return True for everything -- which is
precisely the bug this suite is now written to catch).
"""

from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from app.clients.groq import GroqClientError, chat_completion
from app.clients.tts import TtsClientError, synthesize_speech
from app.core.api_key_rotation import (
    ApiKeyRotationManager,
    clear_exhausted_status,
    get_available_key,
    is_key_exhausted,
    mark_key_exhausted,
)


async def _empty_async_iter():
    return
    yield  # pragma: no cover - makes this an async generator


@pytest.fixture
def mock_redis():
    """Mock the *async* Redis client. See module docstring for why AsyncMock."""
    with patch("app.core.api_key_rotation.get_optional_redis") as mock:
        redis_mock = AsyncMock()
        redis_mock.exists = AsyncMock(return_value=False)
        redis_mock.setex = AsyncMock()
        redis_mock.ttl = AsyncMock(return_value=3600)
        redis_mock.get = AsyncMock(return_value=None)
        redis_mock.delete = AsyncMock()
        # scan_iter is an async generator on a real redis.asyncio client,
        # not a coroutine -- Mock(return_value=[...]) can't stand in for it.
        redis_mock.scan_iter = Mock(return_value=_empty_async_iter())
        mock.return_value = redis_mock
        yield redis_mock


@pytest.mark.asyncio
class TestApiKeyRotationManager:
    """Test the ApiKeyRotationManager class."""

    async def test_rotation_manager_initialization(self):
        """Test that rotation manager initializes correctly."""
        manager = ApiKeyRotationManager("test_service", ["key1", "key2", "key3"])
        assert manager.service == "test_service"
        assert manager.api_keys == ["key1", "key2", "key3"]
        assert manager._current_key is None

    async def test_get_key_returns_first_available(self, mock_redis):
        """Test that get_key returns the first available key."""
        manager = ApiKeyRotationManager("test_service", ["key1", "key2"])
        key = await manager.get_key()
        assert key == "key1"

    async def test_get_key_skips_exhausted(self, mock_redis):
        """Test that get_key skips exhausted keys."""
        manager = ApiKeyRotationManager("test_service", ["key1", "key2"])

        # Mock is_key_exhausted to return True for key1, False for key2
        with patch("app.core.api_key_rotation.is_key_exhausted") as mock_exhausted:
            mock_exhausted.side_effect = lambda service, key: key == "key1"
            key = await manager.get_key()
            assert key == "key2"

    async def test_get_key_returns_none_when_all_exhausted(self, mock_redis):
        """Test that get_key returns None when all keys are exhausted."""
        mock_redis.exists = AsyncMock(return_value=True)
        manager = ApiKeyRotationManager("test_service", ["key1", "key2"])
        key = await manager.get_key()
        assert key is None

    async def test_mark_current_exhausted(self, mock_redis):
        """Test marking the current key as exhausted."""
        manager = ApiKeyRotationManager("test_service", ["key1", "key2"])
        await manager.get_key()  # Set current key to key1
        await manager.mark_current_exhausted()
        mock_redis.setex.assert_called_once()
        assert manager._current_key is None

    async def test_has_available_keys(self, mock_redis):
        """Test checking if available keys exist."""
        manager = ApiKeyRotationManager("test_service", ["key1", "key2"])
        assert await manager.has_available_keys() is True

        mock_redis.exists = AsyncMock(return_value=True)
        assert await manager.has_available_keys() is False

    async def test_available_count(self, mock_redis):
        """Test counting available keys."""
        manager = ApiKeyRotationManager("test_service", ["key1", "key2", "key3"])
        assert await manager.available_count() == 3

        # Mock is_key_exhausted to return True for key1 and key2, False for key3
        with patch("app.core.api_key_rotation.is_key_exhausted") as mock_exhausted:
            mock_exhausted.side_effect = lambda service, key: key in ["key1", "key2"]
            assert await manager.available_count() == 1


@pytest.mark.asyncio
class TestApiKeyRotationFunctions:
    """Test the standalone API key rotation functions."""

    async def test_mark_key_exhausted(self, mock_redis):
        """Test marking a key as exhausted."""
        await mark_key_exhausted("test_service", "test_key")
        mock_redis.setex.assert_called_once()

    async def test_is_key_exhausted(self, mock_redis):
        """Test checking if a key is exhausted."""
        mock_redis.exists = AsyncMock(return_value=True)
        assert await is_key_exhausted("test_service", "test_key") is True

        mock_redis.exists = AsyncMock(return_value=False)
        assert await is_key_exhausted("test_service", "test_key") is False

    async def test_is_key_exhausted_without_redis_fails_open(self):
        """Regression test: when Redis is unavailable, keys must be treated
        as available (fail open), never as exhausted (fail closed). Getting
        this backwards is what caused every rotation-enabled client to
        believe all of its keys were exhausted in production."""
        with patch("app.core.api_key_rotation.get_optional_redis", return_value=None):
            assert await is_key_exhausted("test_service", "test_key") is False

    async def test_get_available_key(self, mock_redis):
        """Test getting an available key from a list."""
        keys = ["key1", "key2", "key3"]
        key = await get_available_key("test_service", keys)
        assert key == "key1"

        # Mock is_key_exhausted to return True for key1, False for others
        with patch("app.core.api_key_rotation.is_key_exhausted") as mock_exhausted:
            mock_exhausted.side_effect = lambda service, key: key == "key1"
            key = await get_available_key("test_service", keys)
            assert key == "key2"

    async def test_clear_exhausted_status(self, mock_redis):
        """Test clearing exhausted status."""
        await clear_exhausted_status("test_service", "test_key")
        mock_redis.delete.assert_called_once()

    async def test_get_exhausted_keys_info_with_real_async_client_shape(self, mock_redis):
        """Regression test: get_exhausted_keys_info must iterate scan_iter
        with `async for`, not a plain `for` loop -- a plain for loop over a
        real async generator raises TypeError immediately."""
        from app.core.api_key_rotation import get_exhausted_keys_info

        info = await get_exhausted_keys_info("test_service")
        assert info["exhausted_count"] == 0


@pytest.mark.asyncio
class TestGroqClientRotation:
    """Test Groq client with API key rotation."""

    async def test_groq_rotation_on_429_error(self):
        """Test that Groq client rotates to next key on 429 error."""
        with patch("app.clients.groq._get_rotation_manager") as mock_manager:
            mock_manager.return_value = ApiKeyRotationManager("groq", ["key1", "key2"])

            mock_settings = Mock()
            mock_settings.groq_api_keys = ["key1", "key2"]
            mock_settings.groq_model = "test-model"
            mock_settings.content_generation_max_concurrent = 3

            with patch("app.clients.groq.get_settings", return_value=mock_settings):
                mock_client = AsyncMock()
                # First key fails with 429, second succeeds
                mock_client.post.side_effect = [
                    httpx.HTTPStatusError(
                        "Rate limit exceeded",
                        request=Mock(),
                        response=Mock(status_code=429, headers=Mock(get=Mock(return_value=None)))
                    ),
                    Mock(
                        raise_for_status=Mock(),
                        json=Mock(return_value={"choices": [{"message": {"content": "test response"}}]})
                    )
                ]

                with patch("app.core.api_key_rotation.is_key_exhausted", return_value=False), \
                     patch("app.core.api_key_rotation.mark_key_exhausted"):
                    result = await chat_completion(mock_client, [{"role": "user", "content": "test"}])
                    assert result == "test response"

    async def test_groq_fails_when_all_keys_exhausted(self):
        """Test that Groq client fails when all keys are exhausted."""
        with patch("app.clients.groq._get_rotation_manager") as mock_manager:
            mock_manager.return_value = ApiKeyRotationManager("groq", ["key1", "key2"])

            mock_settings = Mock()
            mock_settings.groq_api_keys = ["key1", "key2"]
            mock_settings.groq_model = "test-model"
            mock_settings.content_generation_max_concurrent = 3

            with patch("app.clients.groq.get_settings", return_value=mock_settings):
                mock_client = AsyncMock()
                # Both keys fail with 429
                mock_client.post.side_effect = httpx.HTTPStatusError(
                    "Rate limit exceeded",
                    request=Mock(),
                    response=Mock(status_code=429, headers=Mock(get=Mock(return_value=None)))
                )

                with patch("app.core.api_key_rotation.is_key_exhausted", return_value=False), \
                     patch("app.core.api_key_rotation.mark_key_exhausted"), \
                     pytest.raises(GroqClientError):
                    await chat_completion(mock_client, [{"role": "user", "content": "test"}])

    async def test_groq_fails_cleanly_when_all_keys_pre_exhausted(self):
        """Regression test: if every key is already marked exhausted before
        any request is attempted, chat_completion must raise a clean
        GroqClientError -- not an UnboundLocalError from a `last_error`
        that was never assigned because the per-key loop body never ran."""
        with patch("app.clients.groq._get_rotation_manager") as mock_manager:
            mock_manager.return_value = ApiKeyRotationManager("groq", ["key1", "key2"])

            mock_settings = Mock()
            mock_settings.groq_api_keys = ["key1", "key2"]
            mock_settings.groq_model = "test-model"
            mock_settings.content_generation_max_concurrent = 3
            mock_settings.openrouter_api_key = None  # disable OpenRouter fallback for this test

            with patch("app.clients.groq.get_settings", return_value=mock_settings):
                mock_client = AsyncMock()

                with patch("app.core.api_key_rotation.is_key_exhausted", return_value=True):
                    with pytest.raises(GroqClientError):
                        await chat_completion(mock_client, [{"role": "user", "content": "test"}])
                    mock_client.post.assert_not_called()


@pytest.mark.asyncio
class TestTtsClientRotation:
    """Test TTS client with API key rotation."""

    async def test_tts_rotation_on_429_error(self):
        """Test that TTS client rotates to next key on 429 error."""
        with patch("app.clients.tts._get_rotation_manager") as mock_manager:
            mock_manager.return_value = ApiKeyRotationManager("elevenlabs", ["key1", "key2"])

            with patch("app.clients.tts.get_settings") as mock_settings:
                mock_settings.return_value.elevenlabs_api_keys = ["key1", "key2"]
                mock_settings.return_value.elevenlabs_voice_id = "test-voice"

                mock_client = AsyncMock()
                # First key fails with 429, second succeeds
                mock_client.post.side_effect = [
                    Mock(status_code=429, text="Rate limit exceeded"),
                    Mock(status_code=200, content=b"audio data")
                ]

                with patch("app.clients.tts.is_key_exhausted", return_value=False), \
                     patch("app.clients.tts.mark_key_exhausted"):
                    result = await synthesize_speech("test text", http_client=mock_client)
                    assert result == b"audio data"

    async def test_tts_fails_when_all_keys_exhausted(self):
        """Test that TTS client fails when all keys are exhausted."""
        with patch("app.clients.tts._get_rotation_manager") as mock_manager:
            mock_manager.return_value = ApiKeyRotationManager("elevenlabs", ["key1", "key2"])

            with patch("app.clients.tts.get_settings") as mock_settings:
                mock_settings.return_value.elevenlabs_api_keys = ["key1", "key2"]
                mock_settings.return_value.elevenlabs_voice_id = "test-voice"

                mock_client = AsyncMock()
                # Both keys fail with 429
                mock_client.post.return_value = Mock(status_code=429, text="Rate limit exceeded")

                with patch("app.clients.tts.is_key_exhausted", return_value=False), \
                     patch("app.clients.tts.mark_key_exhausted"), \
                     pytest.raises(TtsClientError):
                    await synthesize_speech("test text", http_client=mock_client)

    async def test_tts_fails_cleanly_when_all_keys_pre_exhausted(self):
        """Regression test: same as the Groq equivalent above, but for TTS."""
        with patch("app.clients.tts._get_rotation_manager") as mock_manager:
            mock_manager.return_value = ApiKeyRotationManager("elevenlabs", ["key1", "key2"])

            with patch("app.clients.tts.get_settings") as mock_settings:
                mock_settings.return_value.elevenlabs_api_keys = ["key1", "key2"]
                mock_settings.return_value.elevenlabs_voice_id = "test-voice"

                mock_client = AsyncMock()

                with patch("app.clients.tts.is_key_exhausted", return_value=True):
                    with pytest.raises(TtsClientError):
                        await synthesize_speech("test text", http_client=mock_client)
                    mock_client.post.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
