from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.aviationstack import (
    AVIATIONSTACK_BASE_URL,
    AviationStackClientError,
    FlightNotFoundError,
    get_airport,
    get_flight,
)

FLIGHT_RESPONSE = {
    "data": [
        {
            "flight_status": "scheduled",
            "departure": {"iata": "ADD"},
            "arrival": {"iata": "DXB"},
            "flight": {"iata": "ET409"},
        }
    ]
}

AIRPORT_RESPONSE = {
    "data": [
        {
            "iata_code": "ADD",
            "airport_name": "Bole International",
            "latitude": "8.9806",
            "longitude": "38.7992",
        }
    ]
}


@pytest.fixture(autouse=True)
def no_real_sleep():
    with patch("app.clients.aviationstack.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.mark.asyncio
async def test_get_flight_returns_departure_and_arrival_iata():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
                return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
            )
            flight = await get_flight(client, "ET409")

    assert flight.departure_iata == "ADD"
    assert flight.arrival_iata == "DXB"
    assert flight.flight_status == "scheduled"


@pytest.mark.asyncio
async def test_get_flight_raises_not_found_when_no_data():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            with pytest.raises(FlightNotFoundError):
                await get_flight(client, "ZZ999")


@pytest.mark.asyncio
async def test_get_airport_returns_coordinates_as_floats():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(
                return_value=httpx.Response(200, json=AIRPORT_RESPONSE)
            )
            airport = await get_airport(client, "ADD")

    assert airport.lat == 8.9806
    assert airport.lng == 38.7992
    assert isinstance(airport.lat, float)
    assert airport.name == "Bole International"


@pytest.mark.asyncio
async def test_get_airport_raises_when_not_found():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(
                return_value=httpx.Response(200, json={"data": []})
            )
            with pytest.raises(AviationStackClientError):
                await get_airport(client, "ZZZ")


@pytest.mark.asyncio
async def test_quota_exhausted_429_fails_immediately_without_retrying(monkeypatch):
    quota_response = {"error": {"code": "usage_limit_reached", "message": "..."}}

    # Force a single API key so the call_count assertion below is deterministic
    # regardless of how many keys are configured in .env.
    monkeypatch.setenv("AVIATIONSTACK_API_KEY", "test-single-key")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.clients import aviationstack as av_module
    av_module.reset_rotation_manager_cache()

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
                return_value=httpx.Response(429, json=quota_response)
            )
            with pytest.raises(AviationStackClientError, match="quota is exhausted"):
                await get_flight(client, "ET409")

    # A quota-exhausted 429 (usage_limit_reached) is a hard, non-transient
    # failure -- retrying the same key won't help, it'll just waste more of
    # the (already exhausted) quota. The client breaks out of the retry loop
    # immediately on the first such response and raises the specific,
    # actionable error instead of burning through all _MAX_ATTEMPTS retries.
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_transient_429_does_retry():
    rate_limit_response = {"error": {"code": "rate_limit_reached", "message": "..."}}

    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
                side_effect=[
                    httpx.Response(429, json=rate_limit_response),
                    httpx.Response(200, json=FLIGHT_RESPONSE),
                ]
            )
            flight = await get_flight(client, "ET409")

    assert route.call_count == 2
    assert flight.departure_iata == "ADD"


@pytest.mark.asyncio
async def test_does_not_retry_invalid_access_key():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
                return_value=httpx.Response(401, json={"error": {"code": "invalid_access_key"}})
            )
            with pytest.raises(AviationStackClientError):
                await get_flight(client, "ET409")

    assert route.call_count == 1


@pytest.mark.asyncio
async def test_retries_on_server_error_then_succeeds():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            route = respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
                side_effect=[httpx.Response(503), httpx.Response(200, json=FLIGHT_RESPONSE)]
            )
            flight = await get_flight(client, "ET409")

    assert route.call_count == 2
    assert flight.arrival_iata == "DXB"
