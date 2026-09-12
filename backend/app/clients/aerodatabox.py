"""
AeroDataBox via RapidAPI -- flight number to route resolution.
Better than AviationStack for Aloft because it returns airport
coordinates in the same response (no second /airports call needed).

Free tier: 500 requests/month via RapidAPI. No credit card required.
Sign up at: rapidapi.com/aedbx-aedbx/api/aerodatabox
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import BaseModel

from app.core.api_key_rotation import ApiKeyRotationManager, is_key_exhausted, mark_key_exhausted
from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.aerodatabox")

AERODATABOX_BASE_URL = "https://aerodatabox.p.rapidapi.com"

# 429 from RapidAPI means monthly quota exhausted -- not a transient rate limit.
# Retrying will just waste quota. Treat it as a hard, non-retryable failure.
_RETRYABLE_STATUS_CODES = {500, 502, 503, 504}
_MAX_ATTEMPTS = 3

# Module-level rotation manager cache
_rotation_manager = None


def _get_rotation_manager():
    """Get or create the API key rotation manager for AeroDataBox."""
    global _rotation_manager
    if _rotation_manager is None:
        settings = get_settings()
        # Handle both the property (real Settings) and direct attribute (mocked Settings in tests)
        api_keys = getattr(settings, "aerodatabox_api_keys", None)
        if api_keys is None:
            # Fallback for tests that mock Settings without the property
            api_key = settings.aerodatabox_api_key
            api_keys = [api_key] if api_key else []
        if not api_keys:
            logger.warning("No AeroDataBox API keys configured for rotation")
        _rotation_manager = ApiKeyRotationManager("aerodatabox", api_keys)
    return _rotation_manager


def reset_rotation_manager_cache() -> None:
    """Clear the cached rotation manager so it's rebuilt from current settings.

    See app/clients/groq.py's reset_rotation_manager_cache() for why this
    exists — same module-level-cache-survives-across-tests problem.
    """
    global _rotation_manager
    _rotation_manager = None


class AeroDataBoxFlightInfo(BaseModel):
    flight_iata: str
    departure_iata: str
    arrival_iata: str
    departure_lat: float
    departure_lng: float
    arrival_lat: float
    arrival_lng: float
    status: str


class AeroDataBoxClientError(Exception):
    pass


class FlightNotFoundError(AeroDataBoxClientError):
    pass


async def get_flight(
    client: httpx.AsyncClient,
    flight_iata: str,
) -> AeroDataBoxFlightInfo:
    """Resolve a flight number to route + airport coordinates.
    One call returns everything -- no second airport lookup needed.
    """
    settings = get_settings()
    rotation_manager = _get_rotation_manager()

    # Get API keys from rotation manager or fall back to single key
    api_keys = rotation_manager.api_keys if rotation_manager.api_keys else (
        [settings.aerodatabox_api_key] if settings.aerodatabox_api_key else []
    )

    if not api_keys:
        raise AeroDataBoxClientError("AERODATABOX_API_KEY not configured")

    # last_error is set inside the per-key loop below. If every key is
    # skipped as pre-exhausted, the loop body never runs -- keep this
    # defined up front so the final `raise ... from last_error` can't hit
    # an UnboundLocalError in that case.
    last_error: Exception | None = None

    # Try each available API key
    for api_key in api_keys:
        # Skip if this key is marked as exhausted (only when using multiple keys)
        if len(api_keys) > 1 and await is_key_exhausted("aerodatabox", api_key):
            logger.debug("Skipping exhausted AeroDataBox API key")
            continue

        headers = {
            "x-rapidapi-host": "aerodatabox.p.rapidapi.com",
            "x-rapidapi-key": api_key,
        }

        should_mark_exhausted = False

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.get(
                    f"{AERODATABOX_BASE_URL}/flights/number/{flight_iata}",
                    headers=headers,
                    timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
                )
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    raise FlightNotFoundError(f"Flight '{flight_iata}' not found") from exc
                if exc.response.status_code == 429:
                    should_mark_exhausted = True
                    last_error = AeroDataBoxClientError(
                        "AeroDataBox monthly quota exhausted for this key -- rotating to next key"
                    )
                    break
                if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise AeroDataBoxClientError(
                        f"Non-retryable error: {exc.response.status_code}"
                    ) from exc
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(0.5 * (2 ** (attempt - 1)))
                continue
            else:
                return _parse_response(response, flight_iata)

        # If we got here, this key failed all retries - mark it as exhausted if using rotation
        # and we encountered quota errors
        if len(api_keys) > 1 and should_mark_exhausted:
            logger.warning("Marking AeroDataBox API key as exhausted after quota errors")
            await mark_key_exhausted("aerodatabox", api_key)
        # Continue to next key if using rotation
        if len(api_keys) > 1:
            continue
        # If not using rotation and key failed, raise error. Prefer the
        # specific error already built over the generic fallback below.
        if isinstance(last_error, AeroDataBoxClientError):
            raise last_error
        break

    if last_error is None:
        raise AeroDataBoxClientError(
            "Failed: all configured AeroDataBox API keys are currently marked "
            "exhausted (cooling down after a previous quota error)."
        )
    raise AeroDataBoxClientError("Failed after trying all API keys") from last_error


def _parse_response(response: httpx.Response, flight_iata: str) -> AeroDataBoxFlightInfo:
    data = response.json()

    # AeroDataBox returns a list; take the first active/scheduled result
    flights = data if isinstance(data, list) else [data]
    if not flights:
        raise FlightNotFoundError(f"No data for flight '{flight_iata}'")

    flight = flights[0]
    dep = flight.get("departure", {})
    arr = flight.get("arrival", {})
    dep_airport = dep.get("airport", {})
    arr_airport = arr.get("airport", {})

    dep_iata = dep_airport.get("iata")
    arr_iata = arr_airport.get("iata")
    dep_lat = dep_airport.get("location", {}).get("lat")
    dep_lng = dep_airport.get("location", {}).get("lon")
    arr_lat = arr_airport.get("location", {}).get("lat")
    arr_lng = arr_airport.get("location", {}).get("lon")

    if not all([dep_iata, arr_iata, dep_lat, dep_lng, arr_lat, arr_lng]):
        raise AeroDataBoxClientError(
            f"Incomplete airport data for '{flight_iata}': "
            f"dep={dep_iata}({dep_lat},{dep_lng}) arr={arr_iata}({arr_lat},{arr_lng})"
        )

    return AeroDataBoxFlightInfo(
        flight_iata=flight_iata,
        departure_iata=dep_iata,
        arrival_iata=arr_iata,
        departure_lat=float(dep_lat),
        departure_lng=float(dep_lng),
        arrival_lat=float(arr_lat),
        arrival_lng=float(arr_lng),
        status=flight.get("status", "unknown"),
    )
