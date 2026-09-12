import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from app.clients.wikipedia import WIKIPEDIA_API_URL
from app.core.config import get_settings
from app.services.corridor import sample_points_by_spacing
from app.services.poi_service import _SAMPLE_OVERLAP_FACTOR, find_pois_along_corridor

ADD = (8.9806, 38.7992)
DXB = (25.2532, 55.3657)

FIXED_RESPONSE = {
    "query": {
        "geosearch": [
            {"pageid": 1001, "title": "Cathedral", "lat": 9.0177, "lon": 38.7669, "dist": 450.2},
            {"pageid": 1002, "title": "Museum", "lat": 9.0339, "lon": 38.7611, "dist": 1820.7},
        ]
    }
}


@pytest.fixture(autouse=True)
def no_real_sleep():
    with patch("app.clients.wikipedia.asyncio.sleep", new=AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def disable_optional_poi_sources():
    """Disable geonames/wikidata/overpass so tests don't need to mock those URLs."""
    _base = get_settings()
    _patched = _base.model_copy(
        update={
            "poi_source_geonames_enabled": False,
            "poi_source_wikidata_enabled": False,
            "poi_source_overpass_enabled": False,
        }
    )
    with patch("app.services.poi_service.get_settings", return_value=_patched):
        yield


@pytest.mark.asyncio
async def test_dedupes_pois_seen_from_multiple_overlapping_sample_points():
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=FIXED_RESPONSE))
            results = await find_pois_along_corridor(client, ADD, DXB, width_km=20)

    page_ids = [poi.page_id for poi in results]
    assert len(page_ids) == len(set(page_ids))
    assert set(page_ids) == {1001, 1002}


@pytest.mark.asyncio
async def test_continues_when_one_sample_point_fails_entirely():
    # width_km=20 -> search_radius_km=10 -> spacing_km = 10 * _SAMPLE_OVERLAP_FACTOR.
    # Compute the same sample points find_pois_along_corridor will, so we
    # know exactly which gscoord to fail.
    spacing_km = 10 * _SAMPLE_OVERLAP_FACTOR
    sample_points = sample_points_by_spacing(ADD, DXB, spacing_km=spacing_km)
    failing_lat, failing_lng = sample_points[0]
    failing_coord = f"{failing_lat}|{failing_lng}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["gscoord"] == failing_coord:
            return httpx.Response(503)
        return httpx.Response(200, json=FIXED_RESPONSE)

    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(side_effect=handler)
            results = await find_pois_along_corridor(client, ADD, DXB, width_km=20)

    # The one failing point (after exhausting its own retries) is skipped;
    # every other point still succeeds, so we still get both POIs.
    assert {poi.page_id for poi in results} == {1001, 1002}


@pytest.mark.asyncio
async def test_excludes_results_outside_the_actual_corridor():
    response_with_far_result = {
        "query": {
            "geosearch": [
                FIXED_RESPONSE["query"]["geosearch"][0],
                {
                    "pageid": 9999,
                    "title": "Suspiciously Far Result",
                    "lat": 6.5,
                    "lon": 3.4,
                    "dist": 9999,
                },
            ]
        }
    }
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json=response_with_far_result)
            )
            results = await find_pois_along_corridor(client, ADD, DXB, width_km=20)

    assert 9999 not in {poi.page_id for poi in results}


@pytest.mark.asyncio
async def test_warns_when_width_exceeds_single_lane_coverage(caplog):
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json={"query": {"geosearch": []}})
            )
            with caplog.at_level(logging.WARNING):
                await find_pois_along_corridor(client, ADD, DXB, width_km=100)

    assert any("exceeds single-lane coverage" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_no_warning_when_width_fits_single_lane_coverage(caplog):
    async with httpx.AsyncClient() as client:
        with respx.mock:
            respx.get(WIKIPEDIA_API_URL).mock(
                return_value=httpx.Response(200, json={"query": {"geosearch": []}})
            )
            with caplog.at_level(logging.WARNING):
                await find_pois_along_corridor(client, ADD, DXB, width_km=20)

    assert not any("exceeds single-lane coverage" in record.message for record in caplog.records)
