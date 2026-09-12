"""
Tests for the BigDataCloud reverse geocoding client (app/clients/geocoding_client.py).

Covers:
  - 200 response with ocean coordinates → returns RegionInfo with is_ocean=True
  - 200 response with land coordinates → returns RegionInfo with is_ocean=False
  - Network errors → returns fallback RegionInfo
  - Malformed response → returns fallback RegionInfo
"""

from __future__ import annotations

import httpx
import pytest
import respx

from app.clients.geocoding_client import _parse_response, reverse_geocode

_GEOCODING_API_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"


@pytest.fixture
def http_client():
    return httpx.AsyncClient()


# ---------------------------------------------------------------------------
# _parse_response unit tests
# ---------------------------------------------------------------------------


def test_parse_ocean_response():
    """Ocean coordinates return is_ocean=True with ocean name."""
    data = {
        "countryName": "",
        "continent": "North America",
        "locality": "North Atlantic Ocean",
        "localityInfo": {"informative": [{"name": "North Atlantic Ocean"}]},
        "principalSubdivision": "",
    }
    result = _parse_response(data)
    assert result.is_ocean is True
    assert result.description == "North Atlantic Ocean"
    assert result.country == ""  # Empty string for ocean coordinates


def test_parse_land_response_with_full_details():
    """Land coordinates with all fields populated."""
    data = {
        "countryName": "Ireland",
        "continent": "Europe",
        "locality": "Cork",
        "principalSubdivision": "County Cork",
    }
    result = _parse_response(data)
    assert result.is_ocean is False
    assert result.description == "Cork, County Cork, Ireland"
    assert result.country == "Ireland"
    assert result.locality == "Cork"


def test_parse_land_response_without_locality():
    """Land coordinates without locality fall back to subdivision + country."""
    data = {
        "countryName": "United Kingdom",
        "continent": "Europe",
        "locality": None,
        "principalSubdivision": "England",
    }
    result = _parse_response(data)
    assert result.is_ocean is False
    assert result.description == "England, United Kingdom"
    assert result.country == "United Kingdom"
    assert result.locality is None


def test_parse_land_response_with_only_country():
    """Minimal land response with only country."""
    data = {
        "countryName": "France",
        "continent": "Europe",
        "locality": None,
        "principalSubdivision": None,
    }
    result = _parse_response(data)
    assert result.is_ocean is False
    assert result.description == "France"
    assert result.country == "France"


def test_parse_empty_response_returns_fallback():
    """Completely empty response returns generic fallback."""
    data = {
        "countryName": None,
        "continent": None,
        "locality": None,
        "principalSubdivision": None,
    }
    result = _parse_response(data)
    assert result.is_ocean is False  # None country is not ocean
    assert result.description == "a remote area"
    assert result.country is None


def test_parse_ocean_without_localityinfo():
    """Ocean response without localityInfo field still works."""
    data = {
        "countryName": "",
        "continent": "North America",
        "locality": "Pacific Ocean",
        "principalSubdivision": "",
    }
    result = _parse_response(data)
    assert result.is_ocean is True
    assert result.description == "Pacific Ocean"  # Should use locality when localityInfo is missing


# ---------------------------------------------------------------------------
# reverse_geocode integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_reverse_geocode_ocean_coordinates(http_client):
    """Ocean coordinates return correct RegionInfo."""
    mock_response = {
        "countryName": "",
        "continent": "North America",
        "locality": "North Atlantic Ocean",
        "localityInfo": {"informative": [{"name": "North Atlantic Ocean"}]},
        "principalSubdivision": "",
    }
    respx.get(_GEOCODING_API_URL).mock(return_value=httpx.Response(200, json=mock_response))

    result = await reverse_geocode(http_client, 42.66, -8.46)
    assert result.is_ocean is True
    assert "North Atlantic" in result.description
    assert result.country == ""  # Empty string for ocean


@pytest.mark.asyncio
@respx.mock
async def test_reverse_geocode_land_coordinates(http_client):
    """Land coordinates return correct RegionInfo."""
    mock_response = {
        "countryName": "Ireland",
        "continent": "Europe",
        "locality": "Dublin",
        "principalSubdivision": "County Dublin",
    }
    respx.get(_GEOCODING_API_URL).mock(return_value=httpx.Response(200, json=mock_response))

    result = await reverse_geocode(http_client, 53.34, -6.26)
    assert result.is_ocean is False
    assert result.description == "Dublin, County Dublin, Ireland"
    assert result.country == "Ireland"


@pytest.mark.asyncio
@respx.mock
async def test_reverse_geocode_network_error_returns_fallback(http_client):
    """Network errors return generic fallback instead of raising."""
    respx.get(_GEOCODING_API_URL).mock(side_effect=httpx.ConnectError("Connection failed"))

    result = await reverse_geocode(http_client, 42.66, -8.46)
    assert result.is_ocean is False
    assert result.description == "a remote area"
    assert result.country is None


@pytest.mark.asyncio
@respx.mock
async def test_reverse_geocode_http_error_returns_fallback(http_client):
    """HTTP errors return generic fallback instead of raising."""
    respx.get(_GEOCODING_API_URL).mock(
        return_value=httpx.Response(500, text="Internal Server Error")
    )

    result = await reverse_geocode(http_client, 42.66, -8.46)
    assert result.is_ocean is False
    assert result.description == "a remote area"


@pytest.mark.asyncio
@respx.mock
async def test_reverse_geocode_timeout_returns_fallback(http_client):
    """Timeouts return generic fallback instead of raising."""
    respx.get(_GEOCODING_API_URL).mock(side_effect=Exception("Request timed out"))

    result = await reverse_geocode(http_client, 42.66, -8.46)
    assert result.is_ocean is False
    assert result.description == "a remote area"


@pytest.mark.asyncio
@respx.mock
async def test_reverse_geocode_malformed_json_returns_fallback(http_client):
    """Malformed JSON response returns generic fallback."""
    respx.get(_GEOCODING_API_URL).mock(return_value=httpx.Response(200, text="not valid json"))

    result = await reverse_geocode(http_client, 42.66, -8.46)
    assert result.is_ocean is False
    assert result.description == "a remote area"


@pytest.mark.asyncio
@respx.mock
async def test_reverse_geocode_sends_correct_params(http_client):
    """Verify correct query parameters are sent to API."""
    route = respx.get(_GEOCODING_API_URL).mock(
        return_value=httpx.Response(200, json={"countryName": "Ireland"})
    )

    await reverse_geocode(http_client, 53.34, -6.26)

    request = route.calls.last.request
    assert "latitude=53.34" in str(request.url)
    assert "longitude=-6.26" in str(request.url)
    assert "localityLanguage=en" in str(request.url)
