"""
Tests for the three new POI source clients (Wikidata, GeoNames, Overpass)
and the Openverse image client.

All external HTTP calls are intercepted with respx.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.geonames import (
    GeoNamesAuthError,
    GeoNamesClientError,
)
from app.clients.geonames import (
    geosearch as geonames_geosearch,
)
from app.clients.openverse import OpenverseClientError, search_images
from app.clients.overpass import OverpassClientError
from app.clients.overpass import geosearch as overpass_geosearch
from app.clients.wikidata import WikidataClientError
from app.clients.wikidata import geosearch as wikidata_geosearch

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_real_sleep():
    with (
        patch("app.clients.wikidata.asyncio.sleep", new=AsyncMock()),
        patch("app.clients.geonames.asyncio.sleep", new=AsyncMock()),
        patch("app.clients.overpass.asyncio.sleep", new=AsyncMock()),
        patch("app.clients.openverse.asyncio.sleep", new=AsyncMock()),
    ):
        yield


# ─────────────────────────────────────────────────────────────────────────────
# Wikidata
# ─────────────────────────────────────────────────────────────────────────────

_WIKIDATA_URL = "https://query.wikidata.org/sparql"

_WIKIDATA_RESPONSE = {
    "results": {
        "bindings": [
            {
                "place": {"value": "http://www.wikidata.org/entity/Q60"},
                "placeLabel": {"value": "New York City"},
                "location": {"value": "Point(-74.006 40.7128)"},
                "typeLabel": {"value": "city"},
                "description": {"value": "Largest city in the United States"},
            },
            {
                "place": {"value": "http://www.wikidata.org/entity/Q61"},
                "placeLabel": {"value": "Washington, D.C."},
                "location": {"value": "Point(-77.0369 38.9072)"},
                "typeLabel": {"value": "city"},
            },
        ]
    }
}


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_geosearch_returns_pois():
    respx.post(_WIKIDATA_URL).mock(return_value=httpx.Response(200, json=_WIKIDATA_RESPONSE))
    async with httpx.AsyncClient() as client:
        results = await wikidata_geosearch(client, lat=40.7, lng=-74.0, radius_km=10.0)

    assert len(results) == 2
    nyc = next(r for r in results if r.entity_id == "Q60")
    assert nyc.title == "New York City"
    assert abs(nyc.lat - 40.7128) < 0.001
    assert abs(nyc.lng - (-74.006)) < 0.001
    assert nyc.types == ["city"]
    assert "Largest city" in nyc.description


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_geosearch_skips_row_without_label():
    response = {
        "results": {
            "bindings": [
                {
                    "place": {"value": "http://www.wikidata.org/entity/Q9999999"},
                    "placeLabel": {"value": "Q9999999"},  # entity ID as label = no English label
                    "location": {"value": "Point(10.0 20.0)"},
                    "typeLabel": {"value": "city"},
                }
            ]
        }
    }
    respx.post(_WIKIDATA_URL).mock(return_value=httpx.Response(200, json=response))
    async with httpx.AsyncClient() as client:
        results = await wikidata_geosearch(client, lat=20.0, lng=10.0, radius_km=5.0)
    assert results == []


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_geosearch_retries_on_503():
    respx.post(_WIKIDATA_URL).mock(
        side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json=_WIKIDATA_RESPONSE),
        ]
    )
    async with httpx.AsyncClient() as client:
        results = await wikidata_geosearch(client, lat=40.7, lng=-74.0)
    assert len(results) == 2


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_geosearch_raises_after_max_retries():
    respx.post(_WIKIDATA_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(WikidataClientError, match="3 attempts"):
            await wikidata_geosearch(client, lat=40.7, lng=-74.0)


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_geosearch_raises_on_non_retryable_error():
    respx.post(_WIKIDATA_URL).mock(return_value=httpx.Response(400, text="bad query"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(WikidataClientError, match="non-retryable"):
            await wikidata_geosearch(client, lat=40.7, lng=-74.0)


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_geosearch_returns_empty_on_no_results():
    respx.post(_WIKIDATA_URL).mock(
        return_value=httpx.Response(200, json={"results": {"bindings": []}})
    )
    async with httpx.AsyncClient() as client:
        results = await wikidata_geosearch(client, lat=0.0, lng=0.0)
    assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# GeoNames
# ─────────────────────────────────────────────────────────────────────────────

_GEONAMES_URL = "https://secure.geonames.org/findNearbyJSON"

_GEONAMES_RESPONSE = {
    "geonames": [
        {
            "geonameId": 2643743,
            "name": "London",
            "lat": "51.50853",
            "lng": "-0.12574",
            "countryCode": "GB",
            "fcl": "P",
            "fcode": "PPLC",
            "distance": "0.5",
            "population": 7556900,
        },
        {
            "geonameId": 2643741,
            "name": "City of London",
            "lat": "51.5136",
            "lng": "-0.0982",
            "countryCode": "GB",
            "fcl": "P",
            "fcode": "PPL",
            "distance": "3.2",
            "population": 7000,
        },
    ]
}


@pytest.mark.asyncio
@respx.mock
async def test_geonames_geosearch_returns_pois():
    respx.get(_GEONAMES_URL).mock(return_value=httpx.Response(200, json=_GEONAMES_RESPONSE))
    async with httpx.AsyncClient() as client:
        results = await geonames_geosearch(client, lat=51.5, lng=-0.1, username="testuser")

    assert len(results) == 2
    london = next(r for r in results if r.geonames_id == 2643743)
    assert london.name == "London"
    assert abs(london.lat - 51.50853) < 0.001
    assert london.feature_code == "PPLC"
    assert london.population == 7556900


@pytest.mark.asyncio
@respx.mock
async def test_geonames_raises_auth_error_on_invalid_credentials():
    respx.get(_GEONAMES_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": {"value": 10, "message": "invalid credentials"}},
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(GeoNamesAuthError, match="auth error"):
            await geonames_geosearch(client, lat=51.5, lng=-0.1, username="baduser")


@pytest.mark.asyncio
@respx.mock
async def test_geonames_raises_client_error_on_daily_limit():
    respx.get(_GEONAMES_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": {"value": 18, "message": "the daily limit of 30000 credits"}},
        )
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(GeoNamesClientError):
            await geonames_geosearch(client, lat=51.5, lng=-0.1, username="testuser")


@pytest.mark.asyncio
@respx.mock
async def test_geonames_returns_empty_on_no_result_status():
    respx.get(_GEONAMES_URL).mock(
        return_value=httpx.Response(
            200,
            json={"status": {"value": 15, "message": "no result found"}},
        )
    )
    async with httpx.AsyncClient() as client:
        results = await geonames_geosearch(client, lat=0.0, lng=0.0, username="testuser")
    assert results == []


@pytest.mark.asyncio
@respx.mock
async def test_geonames_retries_on_503():
    respx.get(_GEONAMES_URL).mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=_GEONAMES_RESPONSE),
        ]
    )
    async with httpx.AsyncClient() as client:
        results = await geonames_geosearch(client, lat=51.5, lng=-0.1, username="testuser")
    assert len(results) == 2


@pytest.mark.asyncio
async def test_geonames_raises_auth_error_when_username_empty():
    async with httpx.AsyncClient() as client:
        with pytest.raises(GeoNamesAuthError, match="GEONAMES_USERNAME"):
            await geonames_geosearch(client, lat=51.5, lng=-0.1, username="")


# ─────────────────────────────────────────────────────────────────────────────
# Overpass
# ─────────────────────────────────────────────────────────────────────────────

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

_OVERPASS_RESPONSE = {
    "elements": [
        {
            "type": "node",
            "id": 123456,
            "lat": 51.5074,
            "lon": -0.1278,
            "tags": {"name": "Trafalgar Square", "tourism": "attraction"},
        },
        {
            "type": "way",
            "id": 789012,
            "center": {"lat": 51.5000, "lon": -0.1000},
            "tags": {"name": "Palace of Westminster", "historic": "monument"},
        },
        {
            "type": "node",
            "id": 999,
            "lat": 51.0,
            "lon": -0.5,
            "tags": {},  # No name -- should be skipped
        },
    ]
}


@pytest.mark.asyncio
@respx.mock
async def test_overpass_geosearch_returns_named_pois():
    respx.post(_OVERPASS_URL).mock(return_value=httpx.Response(200, json=_OVERPASS_RESPONSE))
    async with httpx.AsyncClient() as client:
        results = await overpass_geosearch(client, lat=51.5, lng=-0.1, radius_m=5000)

    assert len(results) == 2  # unnamed node is filtered out
    trafalgar = next(r for r in results if r.osm_id == 123456)
    assert trafalgar.name == "Trafalgar Square"
    assert trafalgar.osm_type == "node"

    palace = next(r for r in results if r.osm_id == 789012)
    assert palace.name == "Palace of Westminster"
    assert palace.osm_type == "way"
    assert abs(palace.lat - 51.5) < 0.001


@pytest.mark.asyncio
@respx.mock
async def test_overpass_geosearch_retries_on_429():
    respx.post(_OVERPASS_URL).mock(
        side_effect=[
            httpx.Response(429, text="Too many requests"),
            httpx.Response(200, json=_OVERPASS_RESPONSE),
        ]
    )
    async with httpx.AsyncClient() as client:
        results = await overpass_geosearch(client, lat=51.5, lng=-0.1)
    assert len(results) == 2


@pytest.mark.asyncio
@respx.mock
async def test_overpass_geosearch_raises_after_max_retries():
    respx.post(_OVERPASS_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(OverpassClientError, match="3 attempts"):
            await overpass_geosearch(client, lat=51.5, lng=-0.1)


@pytest.mark.asyncio
@respx.mock
async def test_overpass_geosearch_returns_empty_on_no_elements():
    respx.post(_OVERPASS_URL).mock(return_value=httpx.Response(200, json={"elements": []}))
    async with httpx.AsyncClient() as client:
        results = await overpass_geosearch(client, lat=0.0, lng=0.0)
    assert results == []


# ─────────────────────────────────────────────────────────────────────────────
# Openverse
# ─────────────────────────────────────────────────────────────────────────────

_OPENVERSE_IMAGES_URL = "https://api.openverse.org/v1/images/"

_OPENVERSE_RESPONSE = {
    "results": [
        {
            "url": "https://example.com/photo1.jpg",
            "width": 1200,
            "height": 800,
            "license": "cc0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "creator": "Jane Photographer",
            "creator_url": "https://example.com/jane",
            "title": "Eiffel Tower at sunset",
            "foreign_landing_url": "https://flickr.com/photos/example/12345",
        },
        {
            "url": "https://example.com/photo2.jpg",
            "width": 100,  # too small -- should be filtered
            "height": 100,
            "license": "cc0",
            "license_url": "",
            "creator": "",
            "creator_url": "",
            "title": "thumbnail",
            "foreign_landing_url": "",
        },
        {
            "url": "https://example.com/photo3.jpg",
            "width": 900,
            "height": 600,
            "license": "by-sa",
            "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
            "creator": "Bob Shooter",
            "creator_url": "",
            "title": "Street view",
            "foreign_landing_url": "",
        },
    ]
}


@pytest.mark.asyncio
@respx.mock
async def test_openverse_search_returns_filtered_images():
    respx.get(_OPENVERSE_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_OPENVERSE_RESPONSE)
    )
    async with httpx.AsyncClient() as client:
        results = await search_images(client, "Eiffel Tower", max_images=4)

    # Small thumbnail is filtered; two valid images remain
    assert len(results) == 2
    assert results[0].url == "https://example.com/photo1.jpg"
    assert results[0].licence == "cc0"
    assert results[0].creator == "Jane Photographer"
    assert results[1].licence == "by-sa"


@pytest.mark.asyncio
@respx.mock
async def test_openverse_search_respects_max_images():
    respx.get(_OPENVERSE_IMAGES_URL).mock(
        return_value=httpx.Response(200, json=_OPENVERSE_RESPONSE)
    )
    async with httpx.AsyncClient() as client:
        results = await search_images(client, "Eiffel Tower", max_images=1)
    assert len(results) == 1


@pytest.mark.asyncio
@respx.mock
async def test_openverse_search_returns_empty_on_no_results():
    respx.get(_OPENVERSE_IMAGES_URL).mock(return_value=httpx.Response(200, json={"results": []}))
    async with httpx.AsyncClient() as client:
        results = await search_images(client, "Obscure Place Nobody Photographed")
    assert results == []


@pytest.mark.asyncio
@respx.mock
async def test_openverse_search_retries_on_429():
    respx.get(_OPENVERSE_IMAGES_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=_OPENVERSE_RESPONSE),
        ]
    )
    async with httpx.AsyncClient() as client:
        results = await search_images(client, "Paris")
    assert len(results) == 2


@pytest.mark.asyncio
@respx.mock
async def test_openverse_search_raises_after_max_retries():
    respx.get(_OPENVERSE_IMAGES_URL).mock(return_value=httpx.Response(503))
    async with httpx.AsyncClient() as client:
        with pytest.raises(OpenverseClientError, match="3 attempts"):
            await search_images(client, "Paris")
