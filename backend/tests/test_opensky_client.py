"""
Tests for the OpenSky Network client (app/clients/opensky.py).

Covers:
  - 200 response with state vectors → returns AircraftPosition
  - 200 response with no states (empty list) → raises AircraftNotFoundError
  - Retryable error (e.g. 429) → retries and eventually raises OpenSkyClientError
  - _parse_state_vector: null lat/lng → returns None
  - _parse_state_vector: full vector → returns correct AircraftPosition
  - ValueError when neither or both of icao24/callsign are provided
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.opensky import (
    AircraftNotFoundError,
    AircraftPosition,
    OpenSkyClientError,
    _parse_state_vector,
    get_aircraft_position,
)

_STATES_URL = "https://opensky-network.org/api/states/all"

# A minimal valid state vector.  Indices:
#   0  icao24, 1 callsign, 2 origin_country, 3 time_position,
#   4  last_contact, 5 longitude, 6 latitude, 7 baro_altitude,
#   8  on_ground, 9 velocity, 10 true_track, 11 vertical_rate, ...
_VALID_STATE = [
    "4b1806",  # 0  icao24
    "SWR123  ",  # 1  callsign (padded to 8)
    "Switzerland",  # 2  origin_country
    1718000000,  # 3  time_position
    1718000001,  # 4  last_contact
    8.5417,  # 5  longitude
    47.4647,  # 6  latitude
    10668.0,  # 7  baro_altitude (metres)
    False,  # 8  on_ground
    245.0,  # 9  velocity (m/s)
    92.3,  # 10 true_track (degrees)
    -2.6,  # 11 vertical_rate (m/s)
]


@pytest.fixture(autouse=True)
def no_real_sleep():
    with patch("app.clients.opensky.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.fixture
def http_client():
    return httpx.AsyncClient()


# ---------------------------------------------------------------------------
# _parse_state_vector unit tests
# ---------------------------------------------------------------------------


def test_parse_state_vector_returns_position_for_valid_vector():
    pos = _parse_state_vector(_VALID_STATE)
    assert pos is not None
    assert pos.icao24 == "4b1806"
    assert pos.callsign == "SWR123"  # whitespace stripped
    assert pos.latitude == pytest.approx(47.4647)
    assert pos.longitude == pytest.approx(8.5417)
    assert pos.baro_altitude_m == pytest.approx(10668.0)
    assert pos.velocity_ms == pytest.approx(245.0)
    assert pos.true_track_deg == pytest.approx(92.3)
    assert pos.vertical_rate_ms == pytest.approx(-2.6)
    assert pos.on_ground is False


def test_parse_state_vector_returns_none_when_lat_is_null():
    state = list(_VALID_STATE)
    state[6] = None  # latitude is null — no position fix
    assert _parse_state_vector(state) is None


def test_parse_state_vector_returns_none_when_lng_is_null():
    state = list(_VALID_STATE)
    state[5] = None  # longitude is null
    assert _parse_state_vector(state) is None


def test_parse_state_vector_handles_null_optional_fields():
    state = list(_VALID_STATE)
    state[7] = None  # baro_altitude
    state[9] = None  # velocity
    state[10] = None  # true_track
    state[11] = None  # vertical_rate
    state[8] = None  # on_ground
    pos = _parse_state_vector(state)
    assert pos is not None
    assert pos.baro_altitude_m is None
    assert pos.velocity_ms is None
    assert pos.true_track_deg is None
    assert pos.vertical_rate_ms is None
    assert pos.on_ground is False  # defaults to False when null


def test_parse_state_vector_strips_callsign_whitespace():
    state = list(_VALID_STATE)
    state[1] = "ET308   "  # padded callsign
    pos = _parse_state_vector(state)
    assert pos is not None
    assert pos.callsign == "ET308"


def test_parse_state_vector_returns_none_for_null_callsign():
    state = list(_VALID_STATE)
    state[1] = None
    pos = _parse_state_vector(state)
    assert pos is not None
    assert pos.callsign is None


# ---------------------------------------------------------------------------
# get_aircraft_position: input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_raises_value_error_when_neither_icao24_nor_callsign(http_client):
    with pytest.raises(ValueError, match="icao24 or callsign"):
        await get_aircraft_position(http_client)


@pytest.mark.asyncio
async def test_raises_value_error_when_both_icao24_and_callsign(http_client):
    with pytest.raises(ValueError, match="not both"):
        await get_aircraft_position(http_client, icao24="4b1806", callsign="SWR123")


# ---------------------------------------------------------------------------
# get_aircraft_position: 200 with states → success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_returns_position_on_200_with_states(http_client):
    respx.get(_STATES_URL).mock(return_value=httpx.Response(200, json={"states": [_VALID_STATE]}))
    pos = await get_aircraft_position(http_client, icao24="4b1806")
    assert isinstance(pos, AircraftPosition)
    assert pos.icao24 == "4b1806"
    assert pos.latitude == pytest.approx(47.4647)


@pytest.mark.asyncio
@respx.mock
async def test_callsign_is_uppercased_and_padded_in_request(http_client):
    """Callsign param must be upper-cased and padded to 8 chars."""
    route = respx.get(_STATES_URL).mock(
        return_value=httpx.Response(200, json={"states": [_VALID_STATE]})
    )
    await get_aircraft_position(http_client, callsign="et308")
    # respx captures the request; spaces are percent-encoded as + in URL query
    request = route.calls.last.request
    raw_query = request.url.query.decode()
    # "ET308   " (3 padding spaces) URL-encodes to "ET308+++" or "ET308%20%20%20"
    assert "ET308" in raw_query
    assert len(raw_query.split("callsign=")[1]) == 8  # value is exactly 8 chars before encoding


# ---------------------------------------------------------------------------
# get_aircraft_position: 200 with empty states → AircraftNotFoundError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_raises_aircraft_not_found_when_states_is_empty(http_client):
    respx.get(_STATES_URL).mock(return_value=httpx.Response(200, json={"states": []}))
    with pytest.raises(AircraftNotFoundError):
        await get_aircraft_position(http_client, icao24="4b1806")


@pytest.mark.asyncio
@respx.mock
async def test_raises_aircraft_not_found_when_states_key_missing(http_client):
    """states key absent — treated as no results."""
    respx.get(_STATES_URL).mock(return_value=httpx.Response(200, json={}))
    with pytest.raises(AircraftNotFoundError):
        await get_aircraft_position(http_client, icao24="4b1806")


@pytest.mark.asyncio
@respx.mock
async def test_raises_aircraft_not_found_when_state_has_no_position(http_client):
    """State vector returned but lat/lng are null."""
    no_pos = list(_VALID_STATE)
    no_pos[6] = None  # lat null
    respx.get(_STATES_URL).mock(return_value=httpx.Response(200, json={"states": [no_pos]}))
    with pytest.raises(AircraftNotFoundError):
        await get_aircraft_position(http_client, icao24="4b1806")


# ---------------------------------------------------------------------------
# get_aircraft_position: retryable error → OpenSkyClientError after retries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_raises_opensky_error_after_retryable_responses(http_client):
    """Three consecutive 429s exhaust retries and raise OpenSkyClientError."""
    respx.get(_STATES_URL).mock(return_value=httpx.Response(429, text="rate limited"))
    with pytest.raises(OpenSkyClientError):
        await get_aircraft_position(http_client, icao24="4b1806")


@pytest.mark.asyncio
@respx.mock
async def test_raises_opensky_error_on_non_retryable_status(http_client):
    """A 401 Unauthorized should raise immediately without retrying."""
    route = respx.get(_STATES_URL).mock(return_value=httpx.Response(401, text="unauthorized"))
    with pytest.raises(OpenSkyClientError):
        await get_aircraft_position(http_client, icao24="4b1806")
    # Should not have retried — only one call
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_succeeds_on_second_attempt_after_retryable_error(http_client):
    """First attempt returns 503, second returns 200 with valid states."""
    respx.get(_STATES_URL).mock(
        side_effect=[
            httpx.Response(503, text="service unavailable"),
            httpx.Response(200, json={"states": [_VALID_STATE]}),
        ]
    )
    pos = await get_aircraft_position(http_client, icao24="4b1806")
    assert pos.icao24 == "4b1806"
