"""
OpenSky Network client — live ADS-B aircraft position lookup.

Authentication
──────────────
OpenSky now requires OAuth2 client-credentials flow. Create an API client
at https://opensky-network.org → Account → API Client to get a
client_id / client_secret pair. Leave both unset for anonymous access
(more limited, but still functional).

Token flow
──────────
1. POST client_id + client_secret to the OpenSky auth server.
2. Receive a Bearer token (expires ~30 minutes).
3. Cache the token and reuse until near expiry, then refresh.

API docs: https://openskynetwork.github.io/opensky-api/rest.html
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx
from pydantic import BaseModel

from app.core.api_key_rotation import is_key_exhausted, mark_key_exhausted
from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.opensky")

_BASE_URL = "https://opensky-network.org/api"
_STATES_ENDPOINT = f"{_BASE_URL}/states/all"
_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0
_TOKEN_REFRESH_MARGIN_SECONDS = 60

# Module-level rotation manager cache
_rotation_manager = None


def _get_rotation_manager():
    """Get or create the API key rotation manager for OpenSky."""
    global _rotation_manager
    if _rotation_manager is None:
        settings = get_settings()
        # Handle both the property (real Settings) and direct attribute (mocked Settings in tests)
        client_ids = getattr(settings, "opensky_client_ids", None)
        client_secrets = getattr(settings, "opensky_client_secrets", None)
        if client_ids is None or client_secrets is None:
            # Fallback for tests that mock Settings without the property
            client_id = settings.opensky_client_id
            client_secret = settings.opensky_client_secret
            client_ids = [client_id] if client_id else []
            client_secrets = [client_secret.get_secret_value()] if client_secret else []
        if not client_ids or not client_secrets:
            logger.warning("No OpenSky credentials configured for rotation")
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


class AircraftPosition(BaseModel):
    """Current position of an aircraft from the OpenSky live feed."""

    icao24: str
    callsign: str | None
    latitude: float
    longitude: float
    baro_altitude_m: float | None = None
    velocity_ms: float | None = None
    true_track_deg: float | None = None
    vertical_rate_ms: float | None = None
    on_ground: bool = False


class OpenSkyClientError(Exception):
    """Raised when the OpenSky API request fails after retries."""


class AircraftNotFoundError(OpenSkyClientError):
    """Raised when the aircraft is not currently visible in the OpenSky feed.

    This is normal — the aircraft may be on the ground, out of ADS-B
    coverage, or the transponder may be off. Callers should treat this
    as a soft failure and fall back to client-provided GPS.
    """


class _TokenCache:
    """Thread-safe (async-safe) OAuth2 token cache for OpenSky.

    Stores the current access token and its expiry time. A new token
    is fetched automatically when the current one is missing or about
    to expire.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._current_credentials: tuple[str, str] | None = None

    async def get_token(self, client: httpx.AsyncClient) -> str | None:
        """Return a valid Bearer token, or None if client credentials are not set."""
        async with self._lock:
            now = time.time()
            if self._token and now < self._expires_at - _TOKEN_REFRESH_MARGIN_SECONDS:
                return self._token

            settings = get_settings()
            client_id = settings.opensky_client_id
            client_secret = (
                settings.opensky_client_secret.get_secret_value()
                if settings.opensky_client_secret
                else None
            )

            if not client_id or not client_secret:
                return None

            try:
                token = await self._fetch_token(client, client_id, client_secret)
                self._token = token
                self._expires_at = time.time() + 1800  # OpenSky tokens last ~30 min
                self._current_credentials = (client_id, client_secret)
                return self._token
            except Exception as exc:
                logger.warning("Failed to fetch OpenSky OAuth token: %s", exc)
                return None

    async def get_token_with_rotation(self, client: httpx.AsyncClient) -> str | None:
        """Return a valid Bearer token using credential rotation if available."""
        credentials = _get_rotation_manager()
        
        if not credentials or len(credentials) <= 1:
            # Fall back to single credential behavior
            return await self.get_token(client)
        
        # Try each credential pair
        for client_id, client_secret in credentials:
            # Skip if this credential pair is marked as exhausted
            if len(credentials) > 1 and await is_key_exhausted(
                "opensky", f"{client_id}:{client_secret}"
            ):
                logger.debug("Skipping exhausted OpenSky credentials")
                continue
            
            async with self._lock:
                now = time.time()
                # If we have a valid token from current credentials, use it
                if (self._token and 
                    now < self._expires_at - _TOKEN_REFRESH_MARGIN_SECONDS and
                    self._current_credentials == (client_id, client_secret)):
                    return self._token

                try:
                    token = await self._fetch_token(client, client_id, client_secret)
                    self._token = token
                    self._expires_at = time.time() + 1800
                    self._current_credentials = (client_id, client_secret)
                    return token
                except Exception as exc:
                    logger.warning("Failed to fetch OpenSky OAuth token with credentials %s: %s", 
                                client_id, exc)
                    # Mark these credentials as exhausted if using rotation
                    if len(credentials) > 1:
                        await mark_key_exhausted("opensky", f"{client_id}:{client_secret}")
                    continue
        
        return None

    async def _fetch_token(
        self, client: httpx.AsyncClient, client_id: str, client_secret: str
    ) -> str:
        response = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        response.raise_for_status()
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise OpenSkyClientError(f"OpenSky token endpoint returned no access_token: {payload}")
        return token

    def invalidate(self) -> None:
        """Drop the cached token (e.g. on 401 from the API)."""
        self._token = None
        self._expires_at = 0.0
        self._current_credentials = None


_token_cache = _TokenCache()


def _parse_state_vector(state: list) -> AircraftPosition | None:
    """Parse a single OpenSky state vector list into an AircraftPosition.

    Returns None if the aircraft has no position fix (lat/lng are null).
    """
    try:
        lat = state[6]
        lng = state[5]
        if lat is None or lng is None:
            return None

        callsign_raw = state[1]
        callsign = callsign_raw.strip() if callsign_raw else None

        return AircraftPosition(
            icao24=state[0],
            callsign=callsign or None,
            latitude=float(lat),
            longitude=float(lng),
            baro_altitude_m=float(state[7]) if state[7] is not None else None,
            velocity_ms=float(state[9]) if state[9] is not None else None,
            true_track_deg=float(state[10]) if state[10] is not None else None,
            vertical_rate_ms=float(state[11]) if state[11] is not None else None,
            on_ground=bool(state[8]) if state[8] is not None else False,
        )
    except (IndexError, TypeError, ValueError):
        return None


async def get_aircraft_position(
    client: httpx.AsyncClient,
    *,
    icao24: str | None = None,
    callsign: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> AircraftPosition:
    """Look up the current live position of an aircraft.

    Exactly one of `icao24` or `callsign` must be provided.

    Uses OAuth2 client-credentials flow when OPENSKY_CLIENT_ID /
    OPENSKY_CLIENT_SECRET are configured. Falls back to anonymous
    access (no auth) when they are not set. The legacy
    `username`/`password` parameters are accepted for backward
    compatibility but are no longer used for API auth.

    Args:
        client: shared httpx AsyncClient.
        icao24: ICAO 24-bit transponder address in hex (e.g. "4b1806").
        callsign: Flight callsign (e.g. "ET308"). Padded to 8 chars
            per the OpenSky API requirement.

    Returns:
        AircraftPosition with the current lat/lng and speed/heading.

    Raises:
        AircraftNotFoundError: aircraft not visible in OpenSky feed right now.
        OpenSkyClientError: network failure or API error after retries.
        ValueError: neither or both of icao24/callsign were provided.
    """
    if not icao24 and not callsign:
        raise ValueError("Provide either icao24 or callsign.")
    if icao24 and callsign:
        raise ValueError("Provide icao24 or callsign, not both.")

    params: dict[str, str] = {}
    if icao24:
        params["icao24"] = icao24.lower()
    if callsign:
        params["callsign"] = callsign.upper().ljust(8)

    token = await _token_cache.get_token_with_rotation(client)
    headers: dict[str, str] = {"User-Agent": "AloftFlightNarrationApp/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.get(
                _STATES_ENDPOINT,
                params=params,
                headers=headers,
                timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            )
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning("OpenSky network error attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc)
        else:
            if response.status_code == 401 and token:
                _token_cache.invalidate()
                token = await _token_cache.get_token_with_rotation(client)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                    continue
            if response.status_code == 200:
                data = response.json()
                states = data.get("states") or []
                if not states:
                    raise AircraftNotFoundError(
                        f"Aircraft not found in OpenSky feed "
                        f"(icao24={icao24!r}, callsign={callsign!r}). "
                        "The aircraft may be on the ground, out of ADS-B coverage, "
                        "or the transponder may be off."
                    )
                position = _parse_state_vector(states[0])
                if position is None:
                    raise AircraftNotFoundError(
                        f"Aircraft found in OpenSky feed but has no position fix "
                        f"(icao24={icao24!r}, callsign={callsign!r})."
                    )
                logger.debug(
                    "OpenSky position for %s: lat=%.4f lng=%.4f",
                    icao24 or callsign,
                    position.latitude,
                    position.longitude,
                )
                return position

            if response.status_code == 404:
                raise AircraftNotFoundError(
                    f"Aircraft not found (HTTP 404) for icao24={icao24!r}, callsign={callsign!r}."
                )
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = OpenSkyClientError(f"OpenSky returned HTTP {response.status_code}")
                logger.warning(
                    "OpenSky retryable error %d, attempt %d/%d",
                    response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )
            else:
                raise OpenSkyClientError(
                    f"OpenSky non-retryable HTTP {response.status_code}: {response.text[:200]}"
                )

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise OpenSkyClientError(
        f"OpenSky request failed after {_MAX_ATTEMPTS} attempts"
    ) from last_error
