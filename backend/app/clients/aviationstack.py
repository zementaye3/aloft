from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import BaseModel

from app.core.api_key_rotation import ApiKeyRotationManager, is_key_exhausted, mark_key_exhausted
from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.aviationstack")

AVIATIONSTACK_BASE_URL = "https://api.aviationstack.com/v1"

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 0.5
_NON_RETRYABLE_429_CODES = {"usage_limit_reached"}

# Module-level rotation manager cache
_rotation_manager = None


def _get_rotation_manager():
    """Get or create the API key rotation manager for AviationStack."""
    global _rotation_manager
    if _rotation_manager is None:
        settings = get_settings()
        # Handle both the property (real Settings) and direct attribute (mocked Settings in tests)
        api_keys = getattr(settings, "aviationstack_api_keys", None)
        if api_keys is None:
            # Fallback for tests that mock Settings without the property
            api_key = settings.aviationstack_api_key
            api_keys = [api_key] if api_key else []
        if not api_keys:
            logger.warning("No AviationStack API keys configured for rotation")
        _rotation_manager = ApiKeyRotationManager("aviationstack", api_keys)
    return _rotation_manager


def reset_rotation_manager_cache() -> None:
    """Clear the cached rotation manager so it's rebuilt from current settings.

    See app/clients/groq.py's reset_rotation_manager_cache() for why this
    exists — same module-level-cache-survives-across-tests problem.
    """
    global _rotation_manager
    _rotation_manager = None


class FlightInfo(BaseModel):
    flight_iata: str
    departure_iata: str
    arrival_iata: str
    flight_status: str
    callsign: str | None = None
    departure_scheduled: str | None = None
    arrival_scheduled: str | None = None
    airline_name: str | None = None


class AirportInfo(BaseModel):
    iata_code: str
    name: str
    lat: float
    lng: float


class AviationStackClientError(Exception):
    pass


class FlightNotFoundError(AviationStackClientError):
    pass


async def get_flight(client: httpx.AsyncClient, flight_iata: str) -> FlightInfo:
    results = await _request(client, "flights", {"flight_iata": flight_iata})
    if not results:
        raise FlightNotFoundError(f"No flight found for '{flight_iata}'")

    flight = results[0]
    departure_iata = (flight.get("departure") or {}).get("iata")
    arrival_iata = (flight.get("arrival") or {}).get("iata")
    if not departure_iata or not arrival_iata:
        raise AviationStackClientError(
            f"Flight '{flight_iata}' is missing a departure or arrival IATA code"
        )

    callsign = None
    flight_icao = flight.get("flight_icao") or flight.get("icao")
    if flight_icao:
        callsign = flight_icao.strip()
    else:
        airline_data = flight.get("airline") or {}
        airline_icao = airline_data.get("icao")
        if airline_icao:
            callsign = f"{airline_icao.strip()}{flight_iata[:3]}".upper()

    departure_scheduled = (flight.get("departure") or {}).get("scheduled")
    arrival_scheduled = (flight.get("arrival") or {}).get("scheduled")
    airline_name = (flight.get("airline") or {}).get("name")

    return FlightInfo(
        flight_iata=flight_iata,
        departure_iata=departure_iata,
        arrival_iata=arrival_iata,
        flight_status=flight.get("flight_status") or "unknown",
        callsign=callsign,
        departure_scheduled=departure_scheduled,
        arrival_scheduled=arrival_scheduled,
        airline_name=airline_name,
    )


async def get_airport(client: httpx.AsyncClient, iata_code: str) -> AirportInfo:
    results = await _request(client, "airports", {"iata_code": iata_code})
    if not results:
        raise AviationStackClientError(f"No airport found for IATA code '{iata_code}'")

    airport = results[0]
    lat, lng = airport.get("latitude"), airport.get("longitude")
    if lat is None or lng is None:
        raise AviationStackClientError(f"Airport '{iata_code}' is missing coordinates")

    return AirportInfo(
        iata_code=iata_code,
        name=airport.get("airport_name") or iata_code,
        lat=float(lat),
        lng=float(lng),
    )


async def get_flights_by_airport(
    client: httpx.AsyncClient,
    dep_iata: str,
    limit: int = 20,
) -> list[FlightInfo]:
    """Get flights departing from a specific airport.

    Args:
        client: HTTP client
        dep_iata: Departure airport IATA code
        limit: Maximum number of flights to return

    Returns:
        List of FlightInfo objects
    """
    results = await _request(client, "flights", {"dep_iata": dep_iata, "limit": limit})

    logger.info("Received %d flights from AviationStack for airport %s", len(results), dep_iata)

    flights = []
    for idx, flight in enumerate(results):
        departure_iata = (flight.get("departure") or {}).get("iata")
        arrival_iata = (flight.get("arrival") or {}).get("iata")
        flight_iata = flight.get("flight_iata", "").strip()
        flight_status = flight.get("flight_status", "unknown")

        logger.debug(
            "Flight %d: IATA=%s, Status=%s, Dep=%s, Arr=%s",
            idx,
            flight_iata or "MISSING",
            flight_status,
            departure_iata,
            arrival_iata,
        )

        # Skip flights without valid departure/arrival codes
        if not departure_iata or not arrival_iata:
            logger.debug("Skipping flight %d: missing departure or arrival IATA", idx)
            continue

        # Generate fallback IATA if missing
        if not flight_iata:
            flight_iata = f"{departure_iata}{arrival_iata}"
            logger.debug("Generated fallback IATA for flight %d: %s", idx, flight_iata)

        callsign = None
        flight_icao = flight.get("flight_icao") or flight.get("icao")
        if flight_icao:
            callsign = flight_icao.strip()
        else:
            airline_data = flight.get("airline") or {}
            airline_icao = airline_data.get("icao")
            if airline_icao:
                callsign = f"{airline_icao.strip()}{flight_iata[:3]}".upper()

        departure_scheduled = (flight.get("departure") or {}).get("scheduled")
        arrival_scheduled = (flight.get("arrival") or {}).get("scheduled")
        airline_name = (flight.get("airline") or {}).get("name")

        flights.append(
            FlightInfo(
                flight_iata=flight_iata,
                departure_iata=departure_iata,
                arrival_iata=arrival_iata,
                flight_status=flight.get("flight_status") or "unknown",
                callsign=callsign,
                departure_scheduled=departure_scheduled,
                arrival_scheduled=arrival_scheduled,
                airline_name=airline_name,
            )
        )

    logger.info("Returning %d valid flights for airport %s", len(flights), dep_iata)
    return flights


async def _request(client: httpx.AsyncClient, endpoint: str, params: dict) -> list[dict]:
    settings = get_settings()
    rotation_manager = _get_rotation_manager()

    # Get API keys from rotation manager or fall back to single key
    api_keys = rotation_manager.api_keys if rotation_manager.api_keys else (
        [settings.aviationstack_api_key] if settings.aviationstack_api_key else []
    )

    if not api_keys:
        raise AviationStackClientError("AVIATIONSTACK_API_KEY is not configured")

    url = f"{AVIATIONSTACK_BASE_URL}/{endpoint}"
    log_params = {k: v for k, v in params.items()}  # key excluded for logging
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

    # last_error is set inside the per-key loop below. If every key is
    # skipped as pre-exhausted, the loop body never runs -- keep this
    # defined up front so the final `raise ... from last_error` can't hit
    # an UnboundLocalError in that case.
    last_error: Exception | None = None

    # Try each available API key
    for api_key in api_keys:
        # Skip if this key is marked as exhausted (only when using multiple keys)
        if len(api_keys) > 1 and await is_key_exhausted("aviationstack", api_key):
            logger.debug("Skipping exhausted AviationStack API key")
            continue

        # access_key is passed as a query param (AviationStack's documented method).
        full_params = {"access_key": api_key, **params}

        should_mark_exhausted = False

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.get(url, params=full_params, timeout=timeout)
                if response.status_code == 429 and _is_quota_exhausted(response):
                    should_mark_exhausted = True
                    last_error = AviationStackClientError(
                        "AviationStack monthly request quota is exhausted for this key."
                    )
                    break
                response.raise_for_status()
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning(
                    "AviationStack %s network error, attempt %d/%d: %s (params=%s)",
                    endpoint,
                    attempt,
                    _MAX_ATTEMPTS,
                    exc,
                    log_params,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                    raise AviationStackClientError(
                        f"AviationStack returned non-retryable status {exc.response.status_code} for {endpoint}"
                    ) from exc
                last_error = exc
                logger.warning(
                    "AviationStack %s got retryable status %d, attempt %d/%d (params=%s)",
                    endpoint,
                    exc.response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                    log_params,
                )
            else:
                return response.json().get("data", [])

            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

        # If we got here, this key failed all retries - mark it as exhausted if using rotation
        # and we encountered quota errors
        if len(api_keys) > 1 and should_mark_exhausted:
            logger.warning("Marking AviationStack API key as exhausted after quota errors")
            await mark_key_exhausted("aviationstack", api_key)
        # Continue to next key if using rotation
        if len(api_keys) > 1:
            continue
        # If not using rotation and key failed, raise error. Prefer the
        # specific error we already built (e.g. "quota is exhausted") over
        # the generic fallback below -- it's more useful for debugging and
        # was previously being computed and then silently discarded here.
        if isinstance(last_error, AviationStackClientError):
            raise last_error
        break

    if last_error is None:
        raise AviationStackClientError(
            f"{endpoint} request failed: all configured AviationStack API keys are "
            "currently marked exhausted (cooling down after a previous quota error)."
        )
    if isinstance(last_error, AviationStackClientError):
        raise last_error
    raise AviationStackClientError(
        f"{endpoint} request failed after trying all API keys"
    ) from last_error


def _is_quota_exhausted(response: httpx.Response) -> bool:
    try:
        error_code = response.json().get("error", {}).get("code")
    except ValueError:
        return False
    return error_code in _NON_RETRYABLE_429_CODES
