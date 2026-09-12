"""
Openverse image client -- CC-licensed photo fallback when Wikimedia has nothing.

Openverse (openverse.org) is a search engine for openly-licensed creative works
maintained by WordPress/Automattic. It indexes ~700M images from Wikimedia
Commons, Flickr, the Metropolitan Museum, and many other sources -- all under
Creative Commons or public domain licences that allow use in an app.

We use it ONLY as a fallback when Wikipedia's get_images() returns an empty
list. Wikimedia Commons images are higher quality and don't require attribution
in most display contexts (Wikipedia content is CC BY-SA); Openverse results
may require displaying a per-image credit, so we keep that fallback layer
separate and track attribution metadata.

API:
  - Production: https://api.openverse.org/v1/images/
  - No API key required for read access (rate limited to 100 req/min unauthenticated).
  - Register a free client ID at https://api.openverse.org/v1/auth_tokens/register/
    to get a higher rate limit (500 req/min).
  - Set OPENVERSE_CLIENT_ID + OPENVERSE_CLIENT_SECRET in .env to use OAuth.
    If not set, falls back to anonymous access.

Docs: https://api.openverse.org/v1/
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from pydantic import BaseModel

from app.core.api_key_rotation import is_key_exhausted, mark_key_exhausted
from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.openverse")

_BASE_URL = "https://api.openverse.org/v1"
_TOKEN_URL = f"{_BASE_URL}/auth_tokens/token/"
_IMAGES_URL = f"{_BASE_URL}/images/"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

# Minimum dimensions to accept -- small thumbnails aren't useful for display
_MIN_DIMENSION_PX = 400

# Licences that allow use in an app without legal risk.
# CC0 and PDM need no attribution. CC BY / CC BY-SA require credit.
_ALLOWED_LICENCE_SLUGS = {"cc0", "pdm", "by", "by-sa", "by-nc", "by-nc-sa"}

# Module-level OAuth token cache. Openverse tokens are valid for ~24 hours.
# Caching avoids a separate token request per search_images() call -- a batch
# of 20 POIs would otherwise fire 20 token requests for no reason.
# Structure: {"token": str, "expires_at": float (Unix timestamp), "credentials": tuple}.
# Protected by _token_lock to prevent race conditions when multiple coroutines
# call search_images concurrently before the cache is populated.
_token_cache: dict | None = None
_token_lock = asyncio.Lock()

# Module-level rotation manager cache
_rotation_manager = None


def _get_rotation_manager():
    """Get or create the API key rotation manager for Openverse."""
    global _rotation_manager
    if _rotation_manager is None:
        settings = get_settings()
        # Handle both the property (real Settings) and direct attribute (mocked Settings in tests)
        client_ids = getattr(settings, "openverse_client_ids", None)
        client_secrets = getattr(settings, "openverse_client_secrets", None)
        if client_ids is None or client_secrets is None:
            # Fallback for tests that mock Settings without the property
            client_id = settings.openverse_client_id
            client_secret = settings.openverse_client_secret
            client_ids = [client_id] if client_id else []
            client_secrets = [client_secret.get_secret_value()] if client_secret else []
        if not client_ids or not client_secrets:
            logger.warning("No Openverse credentials configured for rotation")
        # Pair up client IDs with their corresponding secrets
        credentials = list(zip(client_ids, client_secrets, strict=False)) if client_ids and client_secrets else []
        _rotation_manager = credentials
    return _rotation_manager


def reset_rotation_manager_cache() -> None:
    """Clear the cached credentials list so it's rebuilt from current settings.

    See app/clients/groq.py's reset_rotation_manager_cache() for why this
    exists — same module-level-cache-survives-across-tests problem.
    """
    global _rotation_manager
    _rotation_manager = None


class OpenverseImage(BaseModel):
    """A CC-licensed image from Openverse."""

    url: str
    width: int
    height: int
    licence: str  # e.g. "cc0", "by", "by-sa"
    licence_url: str  # full URL to licence text
    creator: str  # attribution name
    creator_url: str  # attribution link (may be empty)
    title: str
    foreign_landing_url: str  # page on the source site


class OpenverseClientError(Exception):
    """Raised when the Openverse API request fails."""


async def search_images(
    client: httpx.AsyncClient,
    query: str,
    max_images: int = 4,
) -> list[OpenverseImage]:
    """Search Openverse for CC-licensed photos matching query.

    Returns up to max_images results, filtered to minimum dimensions and
    allowed licences. Returns an empty list if no suitable images are found.
    Raises OpenverseClientError on network failure after retries.

    Args:
        client: shared httpx AsyncClient.
        query: search term (typically a POI name).
        max_images: maximum number of images to return.
    """
    settings = get_settings()
    headers = await _get_auth_headers(client, settings)

    params = {
        "q": query,
        "page_size": min(max_images * 3, 20),  # fetch extra to filter by size/licence
        "license_type": "commercial,modification",  # safe for app use
        "mature": "false",
        "extension": "jpg,png",
    }

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.get(
                _IMAGES_URL,
                params=params,
                headers=headers,
                timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            )
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning("Openverse network error attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc)
        else:
            if response.status_code == 200:
                return _parse_results(response, max_images)
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = OpenverseClientError(f"Openverse returned HTTP {response.status_code}")
                logger.warning(
                    "Openverse retryable error %d, attempt %d/%d",
                    response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )
            else:
                raise OpenverseClientError(
                    f"Openverse non-retryable HTTP {response.status_code}: {response.text[:200]}"
                )

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise OpenverseClientError(
        f"Openverse search failed after {_MAX_ATTEMPTS} attempts"
    ) from last_error


async def _get_auth_headers(
    client: httpx.AsyncClient,
    settings,  # Settings instance
) -> dict[str, str]:
    """Return Authorization header if OAuth credentials are set; otherwise empty."""
    global _token_cache

    credentials = _get_rotation_manager()
    
    if not credentials or len(credentials) <= 1:
        # Fall back to single credential behavior
        client_id = getattr(settings, "openverse_client_id", None)
        client_secret = getattr(settings, "openverse_client_secret", None)

        if not client_id or not client_secret:
            # Anonymous access -- 100 req/min limit, fine for low-traffic use
            return {"User-Agent": f"AloftFlightNarrationApp/0.1 ({settings.app_contact_email})"}

        try:
            async with _token_lock:
                now = time.time()
                # Reuse cached token if it has at least 60 seconds of remaining validity.
                if _token_cache and _token_cache["expires_at"] > now + 60:
                    token = _token_cache["token"]
                else:
                    token = await _fetch_oauth_token(
                        client, client_id, client_secret.get_secret_value()
                    )
                    # Openverse tokens are valid for 24 hours; cache for 23h55m to be safe.
                    _token_cache = {"token": token, "expires_at": now + 86100, "credentials": (client_id, client_secret.get_secret_value())}
            return {
                "Authorization": f"Bearer {token}",
                "User-Agent": f"AloftFlightNarrationApp/0.1 ({settings.app_contact_email})",
            }
        except Exception as exc:
            logger.warning("Openverse OAuth failed, falling back to anonymous: %s", exc)
            _token_cache = None  # reset so next call retries rather than replaying a bad token
            return {"User-Agent": f"AloftFlightNarrationApp/0.1 ({settings.app_contact_email})"}
    
    # Try each credential pair with rotation
    for client_id, client_secret in credentials:
        # Skip if this credential pair is marked as exhausted
        if len(credentials) > 1 and await is_key_exhausted(
            "openverse", f"{client_id}:{client_secret}"
        ):
            logger.debug("Skipping exhausted Openverse credentials")
            continue
        
        try:
            async with _token_lock:
                now = time.time()
                # Reuse cached token if it has at least 60 seconds of remaining validity and matches current credentials
                if (_token_cache and 
                    _token_cache["expires_at"] > now + 60 and
                    _token_cache.get("credentials") == (client_id, client_secret)):
                    token = _token_cache["token"]
                else:
                    token = await _fetch_oauth_token(client, client_id, client_secret)
                    # Openverse tokens are valid for 24 hours; cache for 23h55m to be safe.
                    _token_cache = {"token": token, "expires_at": now + 86100, "credentials": (client_id, client_secret)}
            return {
                "Authorization": f"Bearer {token}",
                "User-Agent": f"AloftFlightNarrationApp/0.1 ({settings.app_contact_email})",
            }
        except Exception as exc:
            logger.warning("Openverse OAuth failed with credentials %s, trying next: %s", client_id, exc)
            # Mark these credentials as exhausted if using rotation
            if len(credentials) > 1:
                await mark_key_exhausted("openverse", f"{client_id}:{client_secret}")
            continue
    
    # All credentials failed, fall back to anonymous
    logger.warning("All Openverse credentials exhausted, falling back to anonymous access")
    return {"User-Agent": f"AloftFlightNarrationApp/0.1 ({settings.app_contact_email})"}


async def _fetch_oauth_token(client: httpx.AsyncClient, client_id: str, client_secret: str) -> str:
    """Fetch a short-lived Bearer token from Openverse's OAuth endpoint."""
    response = await client.post(
        _TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def _parse_results(response: httpx.Response, max_images: int) -> list[OpenverseImage]:
    """Parse Openverse JSON search results into OpenverseImage objects."""
    try:
        data = response.json()
    except Exception as exc:
        raise OpenverseClientError(f"Openverse response is not valid JSON: {exc}") from exc

    results = data.get("results", [])
    images: list[OpenverseImage] = []

    for item in results:
        if len(images) >= max_images:
            break
        try:
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)

            # Skip images without dimensions or too small
            if width < _MIN_DIMENSION_PX or height < _MIN_DIMENSION_PX:
                continue

            licence = (item.get("license") or "").lower()
            if licence not in _ALLOWED_LICENCE_SLUGS:
                continue

            url = item.get("url", "")
            if not url:
                continue

            images.append(
                OpenverseImage(
                    url=url,
                    width=width,
                    height=height,
                    licence=licence,
                    licence_url=item.get("license_url", ""),
                    creator=item.get("creator", ""),
                    creator_url=item.get("creator_url", ""),
                    title=item.get("title", ""),
                    foreign_landing_url=item.get("foreign_landing_url", ""),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("Openverse: skipping malformed result: %s", exc)

    logger.debug("Openverse search returned %d usable images", len(images))
    return images
