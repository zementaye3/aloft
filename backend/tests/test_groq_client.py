import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.groq import GROQ_API_URL, OPENROUTER_API_URL, GroqClientError, chat_completion
from app.core.config import Settings

MESSAGES = [{"role": "user", "content": "Tell me about this place."}]


def _fake_settings(**overrides) -> Settings:
    # openrouter_api_key is None by default so tests that only mock GROQ_API_URL
    # don't unexpectedly trigger the OpenRouter fallback.
    defaults = {
        "groq_api_key": "test-key",
        "groq_model": "test-model",
        "openrouter_api_key": None,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _mock_settings(monkeypatch, **overrides):
    monkeypatch.setattr("app.clients.groq.get_settings", lambda: _fake_settings(**overrides))


@pytest.fixture(autouse=True)
def no_real_sleep():
    with patch("app.clients.groq.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.mark.asyncio
async def test_chat_completion_returns_generated_text(monkeypatch):
    _mock_settings(monkeypatch)
    mock_response = {"choices": [{"message": {"content": "A cathedral rises above the hills."}}]}

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(GROQ_API_URL).mock(return_value=httpx.Response(200, json=mock_response))
            result = await chat_completion(client, MESSAGES)

    assert result == "A cathedral rises above the hills."


@pytest.mark.asyncio
async def test_chat_completion_raises_when_api_key_missing(monkeypatch):
    _mock_settings(monkeypatch, groq_api_key=None, openrouter_api_key=None)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post(GROQ_API_URL).mock(return_value=httpx.Response(200, json={}))
            with pytest.raises(GroqClientError, match="No LLM backend configured"):
                await chat_completion(client, MESSAGES)

    assert route.call_count == 0


@pytest.mark.asyncio
async def test_chat_completion_sends_correct_payload_and_auth_header(monkeypatch):
    _mock_settings(monkeypatch, groq_model="llama-test-model")
    mock_response = {"choices": [{"message": {"content": "ok"}}]}

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post(GROQ_API_URL).mock(
                return_value=httpx.Response(200, json=mock_response)
            )
            await chat_completion(client, MESSAGES, temperature=0.5, max_tokens=100)

    sent = route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer test-key"
    sent_body = json.loads(sent.content)
    assert sent_body["model"] == "llama-test-model"
    assert sent_body["temperature"] == 0.5
    assert sent_body["max_completion_tokens"] == 100
    assert sent_body["messages"] == MESSAGES


@pytest.mark.asyncio
async def test_chat_completion_retries_on_503_then_succeeds(monkeypatch):
    _mock_settings(monkeypatch)
    mock_response = {"choices": [{"message": {"content": "Recovered after retry."}}]}

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post(GROQ_API_URL).mock(
                side_effect=[
                    httpx.Response(503),
                    httpx.Response(200, json=mock_response),
                ]
            )
            result = await chat_completion(client, MESSAGES)

    assert route.call_count == 2
    assert result == "Recovered after retry."


@pytest.mark.asyncio
async def test_chat_completion_respects_retry_after_header(monkeypatch):
    _mock_settings(monkeypatch)
    mock_response = {"choices": [{"message": {"content": "ok"}}]}

    with patch("app.clients.groq.asyncio.sleep", new=AsyncMock()) as mock_sleep:
        async with httpx.AsyncClient() as client:
            with respx.mock:
                respx.post(GROQ_API_URL).mock(
                    side_effect=[
                        httpx.Response(429, headers={"retry-after": "7"}),
                        httpx.Response(200, json=mock_response),
                    ]
                )
                await chat_completion(client, MESSAGES)

    mock_sleep.assert_awaited_once_with(7.0)


@pytest.mark.asyncio
async def test_chat_completion_does_not_retry_non_retryable_errors(monkeypatch):
    _mock_settings(monkeypatch)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post(GROQ_API_URL).mock(
                return_value=httpx.Response(401, text="invalid api key")
            )
            with pytest.raises(GroqClientError, match="401"):
                await chat_completion(client, MESSAGES)

    assert route.call_count == 1


@pytest.mark.asyncio
async def test_chat_completion_raises_after_exhausting_retries(monkeypatch):
    _mock_settings(monkeypatch)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.post(GROQ_API_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(GroqClientError):
                await chat_completion(client, MESSAGES)

    assert route.call_count == 3


@pytest.mark.asyncio
async def test_chat_completion_raises_on_malformed_response(monkeypatch):
    _mock_settings(monkeypatch)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(GROQ_API_URL).mock(
                return_value=httpx.Response(200, json={"unexpected": "shape"})
            )
            with pytest.raises(GroqClientError, match="Unexpected Groq response shape"):
                await chat_completion(client, MESSAGES)


@pytest.mark.asyncio
async def test_openrouter_fallback_used_when_all_groq_keys_exhausted(monkeypatch):
    """When all Groq keys fail, OpenRouter is tried automatically."""
    _mock_settings(monkeypatch, openrouter_api_key="or-test-key")
    mock_response = {"choices": [{"message": {"content": "Story from OpenRouter."}}]}

    async with httpx.AsyncClient() as client:
        with respx.mock:
            # Groq always fails
            respx.post(GROQ_API_URL).mock(return_value=httpx.Response(503))
            # OpenRouter succeeds
            or_route = respx.post(OPENROUTER_API_URL).mock(
                return_value=httpx.Response(200, json=mock_response)
            )
            result = await chat_completion(client, MESSAGES)

    assert result == "Story from OpenRouter."
    assert or_route.call_count == 1


@pytest.mark.asyncio
async def test_openrouter_fallback_sends_correct_headers(monkeypatch):
    """OpenRouter requests include the required HTTP-Referer and X-Title headers."""
    _mock_settings(monkeypatch, openrouter_api_key="or-test-key")
    mock_response = {"choices": [{"message": {"content": "ok"}}]}

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(GROQ_API_URL).mock(return_value=httpx.Response(503))
            or_route = respx.post(OPENROUTER_API_URL).mock(
                return_value=httpx.Response(200, json=mock_response)
            )
            await chat_completion(client, MESSAGES)

    sent = or_route.calls[0].request
    assert sent.headers["Authorization"] == "Bearer or-test-key"
    assert "HTTP-Referer" in sent.headers
    assert "X-Title" in sent.headers


@pytest.mark.asyncio
async def test_raises_when_both_groq_and_openrouter_fail(monkeypatch):
    """GroqClientError is raised when both backends fail."""
    _mock_settings(monkeypatch, openrouter_api_key="or-test-key")

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.post(GROQ_API_URL).mock(return_value=httpx.Response(503))
            respx.post(OPENROUTER_API_URL).mock(return_value=httpx.Response(503))
            with pytest.raises(GroqClientError):
                await chat_completion(client, MESSAGES)


@pytest.mark.asyncio
async def test_groq_not_tried_when_only_openrouter_configured(monkeypatch):
    """If only OPENROUTER_API_KEY is set (no Groq key), OpenRouter is used directly."""
    _mock_settings(monkeypatch, groq_api_key=None, openrouter_api_key="or-test-key")
    mock_response = {"choices": [{"message": {"content": "OpenRouter only."}}]}

    async with httpx.AsyncClient() as client:
        with respx.mock:
            groq_route = respx.post(GROQ_API_URL).mock(
                return_value=httpx.Response(200, json=mock_response)
            )
            or_route = respx.post(OPENROUTER_API_URL).mock(
                return_value=httpx.Response(200, json=mock_response)
            )
            result = await chat_completion(client, MESSAGES)

    assert result == "OpenRouter only."
    assert groq_route.call_count == 0
    assert or_route.call_count == 1
