"""
Tests for the Wikidata and GeoNames clients and the multi-source
poi_service wiring.

These tests verify:
  1. The client implementations return results or [] without raising (mocked HTTP).
  2. When feature flags are disabled (default), poi_service returns only
     Wikipedia results (original behavior preserved).
  3. When Wikidata/GeoNames are enabled, their results are merged in and
     deduplicated against Wikipedia results.

All external HTTP calls are intercepted with respx -- no real network calls.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.geonames import GeoNamesPoi
from app.clients.geonames import geosearch as geonames_geosearch
from app.clients.wikidata import WikidataPoi
from app.clients.wikidata import geosearch as wikidata_geosearch
from app.clients.wikipedia import WIKIPEDIA_API_URL
from app.core.config import Settings
from app.services.poi_service import (
    _coord_key,
    _geonames_to_synthetic_id,
    _wikidata_entity_to_synthetic_id,
    find_pois_along_corridor,
)

ADD = (8.9806, 38.7992)
DXB = (25.2532, 55.3657)

_WIKIPEDIA_RESPONSE = {
    "query": {
        "geosearch": [
            {"pageid": 1001, "title": "Cathedral", "lat": 9.0177, "lon": 38.7669, "dist": 450.0},
        ]
    }
}

_WIKIDATA_URL = "https://query.wikidata.org/sparql"
_GEONAMES_URL = "https://secure.geonames.org/findNearbyJSON"


@pytest.fixture(autouse=True)
def no_real_sleep():
    with (
        patch("app.clients.wikipedia.asyncio.sleep", new=AsyncMock()),
        patch("app.clients.wikidata.asyncio.sleep", new=AsyncMock()),
        patch("app.clients.geonames.asyncio.sleep", new=AsyncMock()),
    ):
        yield


# ---------------------------------------------------------------------------
# Client basic contracts -- return [] on empty results (all HTTP mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_wikidata_client_returns_empty_list_when_no_results():
    """Wikidata client returns [] on empty SPARQL results without raising."""
    respx.post(_WIKIDATA_URL).mock(
        return_value=httpx.Response(200, json={"results": {"bindings": []}})
    )
    async with httpx.AsyncClient() as client:
        result = await wikidata_geosearch(client, lat=9.0, lng=38.7, radius_km=10.0)
    assert result == []


@pytest.mark.asyncio
@respx.mock
async def test_geonames_client_returns_empty_list_when_no_result_status():
    """GeoNames client returns [] when the API says no result found (code 15)."""
    respx.get(_GEONAMES_URL).mock(
        return_value=httpx.Response(
            200, json={"status": {"value": 15, "message": "no result found"}}
        )
    )
    async with httpx.AsyncClient() as client:
        result = await geonames_geosearch(
            client, lat=9.0, lng=38.7, username="testuser", radius_km=10.0
        )
    assert result == []


# ---------------------------------------------------------------------------
# Synthetic ID helpers
# ---------------------------------------------------------------------------


def test_wikidata_synthetic_id_is_negative():
    assert _wikidata_entity_to_synthetic_id("Q60") < 0


def test_wikidata_synthetic_id_is_stable():
    assert _wikidata_entity_to_synthetic_id("Q60") == _wikidata_entity_to_synthetic_id("Q60")


def test_wikidata_synthetic_id_differs_per_entity():
    assert _wikidata_entity_to_synthetic_id("Q60") != _wikidata_entity_to_synthetic_id("Q61")


def test_geonames_synthetic_id_is_negative():
    assert _geonames_to_synthetic_id(2643743) < 0


def test_geonames_synthetic_id_stable():
    assert _geonames_to_synthetic_id(1234) == _geonames_to_synthetic_id(1234)


def test_coord_key_rounds_to_three_decimals():
    k1 = _coord_key(9.01771, 38.76691)
    k2 = _coord_key(9.01772, 38.76692)  # within ~100m
    assert k1 == k2


# ---------------------------------------------------------------------------
# poi_service: feature flags disabled (default behavior preserved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poi_service_returns_only_wikipedia_when_flags_disabled():
    fake_settings = Settings.model_construct(
        poi_source_wikidata_enabled=False,
        poi_source_geonames_enabled=False,
        corridor_width_km=100.0,
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=_WIKIPEDIA_RESPONSE)
            )
            with patch("app.services.poi_service.get_settings", return_value=fake_settings):
                results = await find_pois_along_corridor(client, ADD, DXB, width_km=20)

    assert len(results) >= 1
    assert all(poi.page_id > 0 for poi in results)  # only real Wikipedia IDs


# ---------------------------------------------------------------------------
# poi_service: Wikidata enabled, returns extra POI at a distinct coordinate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poi_service_merges_wikidata_result_at_new_coordinate():
    """A Wikidata POI at a coordinate not in Wikipedia results is included."""
    # Midpoint of ADD-DXB corridor, far enough from Cathedral at (9.017, 38.766)
    # to not be deduped, but well inside the corridor.
    fake_wikidata_poi = WikidataPoi(
        entity_id="Q999",
        title="Ancient Ruin",
        lat=17.1,  # midpoint of ADD (8.98) -> DXB (25.25) corridor
        lng=47.1,
        types=["archaeological site"],
    )
    fake_settings = Settings.model_construct(
        poi_source_wikidata_enabled=True,
        poi_source_geonames_enabled=False,
        corridor_width_km=100.0,
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=_WIKIPEDIA_RESPONSE)
            )
            with (
                patch("app.services.poi_service.get_settings", return_value=fake_settings),
                patch(
                    "app.services.poi_service.wikidata_geosearch",
                    new=AsyncMock(return_value=[fake_wikidata_poi]),
                ),
            ):
                results = await find_pois_along_corridor(client, ADD, DXB, width_km=100)

    titles = {poi.title for poi in results}
    assert "Cathedral" in titles
    assert "Ancient Ruin" in titles


@pytest.mark.asyncio
async def test_poi_service_dedupes_wikidata_poi_at_same_coordinate_as_wikipedia():
    """A Wikidata POI at the same coordinate as a Wikipedia result is dropped."""
    # Same coordinate as Cathedral (9.0177, 38.7669), within _DEDUP_COORD_PRECISION
    fake_wikidata_poi = WikidataPoi(
        entity_id="Q1001",
        title="Cathedral (Wikidata copy)",
        lat=9.0177,
        lng=38.7669,
        types=["church"],
    )
    fake_settings = Settings.model_construct(
        poi_source_wikidata_enabled=True,
        poi_source_geonames_enabled=False,
        corridor_width_km=100.0,
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=_WIKIPEDIA_RESPONSE)
            )
            with (
                patch("app.services.poi_service.get_settings", return_value=fake_settings),
                patch(
                    "app.services.poi_service.wikidata_geosearch",
                    new=AsyncMock(return_value=[fake_wikidata_poi]),
                ),
            ):
                results = await find_pois_along_corridor(client, ADD, DXB, width_km=20)

    # Wikidata duplicate should be dropped; only one result at that coordinate
    titles = [poi.title for poi in results]
    assert "Cathedral (Wikidata copy)" not in titles
    assert "Cathedral" in titles


# ---------------------------------------------------------------------------
# poi_service: GeoNames enabled with username set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poi_service_merges_geonames_result_at_new_coordinate():
    # Midpoint of ADD-DXB corridor -- well inside the route.
    fake_geonames_poi = GeoNamesPoi(
        geonames_id=99999,
        name="Small Town",
        lat=17.2,  # midpoint of ADD (8.98) -> DXB (25.25)
        lng=47.2,
        country_code="YE",
        feature_class="P",
        feature_code="PPL",
        distance_km=3.5,
    )
    fake_settings = Settings.model_construct(
        poi_source_wikidata_enabled=False,
        poi_source_geonames_enabled=True,
        geonames_username="testuser",
        corridor_width_km=100.0,
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=_WIKIPEDIA_RESPONSE)
            )
            with (
                patch("app.services.poi_service.get_settings", return_value=fake_settings),
                patch(
                    "app.services.poi_service.geonames_geosearch",
                    new=AsyncMock(return_value=[fake_geonames_poi]),
                ),
            ):
                results = await find_pois_along_corridor(client, ADD, DXB, width_km=100)

    titles = {poi.title for poi in results}
    assert "Small Town" in titles


@pytest.mark.asyncio
async def test_poi_service_skips_geonames_when_username_missing(caplog):
    """GeoNames enabled but no username -- logs a warning and skips it gracefully."""
    import logging

    fake_settings = Settings.model_construct(
        poi_source_wikidata_enabled=False,
        poi_source_geonames_enabled=True,
        geonames_username=None,
        corridor_width_km=100.0,
    )

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=_WIKIPEDIA_RESPONSE)
            )
            with (
                patch("app.services.poi_service.get_settings", return_value=fake_settings),
                caplog.at_level(logging.WARNING, logger="aloft.services.poi"),
            ):
                results = await find_pois_along_corridor(client, ADD, DXB, width_km=20)

    assert any("GEONAMES_USERNAME" in r.message for r in caplog.records)
    # Wikipedia results still returned
    assert len(results) >= 1
