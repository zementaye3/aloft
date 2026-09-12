from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.api_key_rotation import ApiKeyRotationManager
from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.groq")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# OpenRouter is OpenAI-compatible — same request/response shape as Groq.
# Used automatically when all Groq keys are exhausted.
# Default model mirrors the Groq default (Llama 3.3 70B) via OpenRouter's
# free tier. Override with OPENROUTER_MODEL in .env if needed.
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

# Module-level rotation manager cache
_rotation_manager: ApiKeyRotationManager | None = None


def _get_rotation_manager() -> ApiKeyRotationManager:
    """Get or create the API key rotation manager for Groq."""
    global _rotation_manager
    if _rotation_manager is None:
        settings = get_settings()
        # Handle both the property (real Settings) and direct attribute (mocked Settings in tests)
        api_keys = getattr(settings, "groq_api_keys", None)
        if api_keys is None:
            # Fallback for tests that mock Settings without the property
            api_key = settings.groq_api_key
            api_keys = [api_key.get_secret_value()] if api_key else []
        if not api_keys:
            logger.warning("No Groq API keys configured for rotation")
        _rotation_manager = ApiKeyRotationManager("groq", api_keys)
    return _rotation_manager


def reset_rotation_manager_cache() -> None:
    """Clear the cached rotation manager so it's rebuilt from current settings.

    Production never needs this (settings don't change at runtime). It
    exists because the module-level cache above otherwise survives across
    tests in the same pytest process: whichever test calls
    _get_rotation_manager() first "locks in" that test's monkeypatched API
    keys for every test that runs after it. See tests/conftest.py, which
    calls this alongside get_settings.cache_clear() before every test.
    """
    global _rotation_manager
    _rotation_manager = None


def _get_semaphore() -> asyncio.Semaphore:
    """Return a semaphore sized from config.

    Uses a module-level cache keyed on the configured concurrency so the
    same Semaphore is reused across calls (important — creating a new
    Semaphore every call would defeat the purpose entirely).

    Falls back to the default value of 3 if settings are unavailable
    (e.g. in tests that mock the settings object).
    """
    try:
        limit = get_settings().content_generation_max_concurrent
    except (AttributeError, Exception):
        # Settings not available (e.g. mock object in tests) — use safe default.
        limit = 3
    if _get_semaphore._cache_limit != limit:
        _get_semaphore._sem = asyncio.Semaphore(limit)
        _get_semaphore._cache_limit = limit
    return _get_semaphore._sem


_get_semaphore._cache_limit = -1  # type: ignore[attr-defined]
_get_semaphore._sem = asyncio.Semaphore(3)  # type: ignore[attr-defined]


class GroqClientError(Exception):
    pass


async def chat_completion(
    client: httpx.AsyncClient,
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int = 400,
) -> str:
    """Generate a chat completion, trying Groq first then OpenRouter as fallback.

    Priority order:
      1. Groq keys (rotated automatically when one is exhausted)
      2. OpenRouter (used only when every Groq key has failed or is exhausted)

    OpenRouter uses the same OpenAI-compatible API format so the request
    payload and response parsing are identical — only the URL, auth header,
    and model name differ.
    """
    from app.core.api_key_rotation import is_key_exhausted, mark_key_exhausted

    settings = get_settings()
    rotation_manager = _get_rotation_manager()

    # Get API keys from rotation manager or fall back to single key
    groq_api_keys = rotation_manager.api_keys if rotation_manager.api_keys else (
        [settings.groq_api_key.get_secret_value()] if settings.groq_api_key else []
    )

    payload = {
        "model": model or settings.groq_model,
        "messages": messages,
        "temperature": temperature,
        "max_completion_tokens": max_tokens,
    }
    timeout = httpx.Timeout(connect=5.0, read=30.0, write=5.0, pool=5.0)

    last_error: Exception | None = None

    # -------------------------------------------------------------------------
    # Phase 1: Try every Groq key
    # -------------------------------------------------------------------------
    for api_key in groq_api_keys:
        # Skip if this key is marked as exhausted (only when using multiple keys)
        if len(groq_api_keys) > 1 and await is_key_exhausted("groq", api_key):
            logger.debug("Skipping exhausted Groq API key")
            continue

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        retry_after_override: float | None = None
        should_mark_exhausted = False

        async with _get_semaphore():
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(
                        GROQ_API_URL, json=payload, headers=headers, timeout=timeout
                    )
                    response.raise_for_status()
                except httpx.TransportError as exc:
                    last_error = exc
                    logger.warning("Groq network error, attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc)
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                        raise GroqClientError(
                            f"Groq returned non-retryable status {exc.response.status_code}: "
                            f"{exc.response.text[:200]}"
                        ) from exc
                    last_error = exc
                    retry_after_override = _retry_after_seconds(exc.response)
                    if exc.response.status_code == 429:
                        should_mark_exhausted = True
                    logger.warning(
                        "Groq got retryable status %d, attempt %d/%d (retry-after=%s)",
                        exc.response.status_code,
                        attempt,
                        _MAX_ATTEMPTS,
                        retry_after_override,
                    )
                else:
                    return _extract_text(response)

                if attempt < _MAX_ATTEMPTS:
                    wait = (
                        retry_after_override
                        if retry_after_override is not None
                        else _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    )
                    await asyncio.sleep(wait)
                    retry_after_override = None

        if len(groq_api_keys) > 1 and should_mark_exhausted:
            logger.warning("Marking Groq API key as exhausted after quota/rate limit errors")
            await mark_key_exhausted("groq", api_key)
        if len(groq_api_keys) > 1:
            continue
        break

    # -------------------------------------------------------------------------
    # Phase 2: All Groq keys failed — try OpenRouter as fallback
    # -------------------------------------------------------------------------
    openrouter_key = (
        settings.openrouter_api_key.get_secret_value()
        if settings.openrouter_api_key
        else None
    )

    if openrouter_key:
        logger.warning(
            "All Groq keys exhausted or failed — falling back to OpenRouter"
        )
        result = await _try_openrouter(
            client, openrouter_key, messages, model, temperature, max_tokens, timeout
        )
        if result is not None:
            return result
        # OpenRouter also failed — fall through to raise the original Groq error
        logger.warning("OpenRouter fallback also failed — no LLM backends available")

    if not groq_api_keys and not openrouter_key:
        raise GroqClientError(
            "No LLM backend configured. Set GROQ_API_KEY and/or OPENROUTER_API_KEY in .env."
        )

    if last_error is None:
        raise GroqClientError(
            "chat_completion failed: all configured Groq API keys are currently "
            "marked exhausted (cooling down after a previous quota/rate-limit error)."
        )
    raise GroqClientError("chat_completion failed after trying all API keys") from last_error


async def _try_openrouter(
    client: httpx.AsyncClient,
    api_key: str,
    messages: list[dict[str, str]],
    model: str | None,
    temperature: float,
    max_tokens: int,
    timeout: httpx.Timeout,
) -> str | None:
    """Attempt a completion via OpenRouter. Returns the text on success, None on failure.

    OpenRouter is OpenAI-compatible — same payload and response shape as Groq.
    The only differences are:
      - URL: openrouter.ai/api/v1/chat/completions
      - Model: uses OpenRouter model IDs (e.g. meta-llama/llama-3.3-70b-instruct:free)
      - HTTP-Referer header: recommended by OpenRouter for usage attribution

    Uses the same semaphore as Groq calls so total concurrent LLM calls
    never exceeds content_generation_max_concurrent regardless of which
    backend is active.
    """
    # Use the OpenRouter model equivalent of the configured Groq model.
    # If a specific model was requested by the caller, pass it through directly
    # (the caller may already be using an OpenRouter-compatible model ID).
    openrouter_model = model or OPENROUTER_DEFAULT_MODEL

    payload = {
        "model": openrouter_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/JoshuaMinase/Aloft",
        "X-Title": "Aloft",
    }

    async with _get_semaphore():
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.post(
                    OPENROUTER_API_URL, json=payload, headers=headers, timeout=timeout
                )
                response.raise_for_status()
            except httpx.TransportError as exc:
                logger.warning(
                    "OpenRouter network error, attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                    logger.warning(
                        "OpenRouter non-retryable status %d: %s",
                        exc.response.status_code,
                        exc.response.text[:200],
                    )
                    return None
                logger.warning(
                    "OpenRouter retryable status %d, attempt %d/%d",
                    exc.response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )
            else:
                try:
                    return _extract_text(response)
                except GroqClientError as exc:
                    logger.warning("OpenRouter unexpected response shape: %s", exc)
                    return None

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    return None


def _retry_after_seconds(response: httpx.Response) -> float | None:
    val = response.headers.get("retry-after")
    if val is None:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def _extract_text(response: httpx.Response) -> str:
    try:
        return response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        raise GroqClientError(f"Unexpected Groq response shape: {response.text[:200]}") from exc
