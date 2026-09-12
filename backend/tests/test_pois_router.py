"""
Tests for POST /routes/pois -- both the existing lat/lng path and the
new IATA airport code path added in this change.
"""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.clients.wikipedia import WIKIPEDIA_API_URL
from app.core.config import get_settings
from app.core.db import ensure_indexes
from app.core.dependencies import get_database, get_http_client
from app.main import app
from app.models.airport import Airport
from app.services.airport_repository import lookup_static_airport, save_airport

ADD = {"lat": 8.9806, "lng": 38.7992}
DXB = {"lat": 25.2532, "lng": 55.3657}

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


@pytest.fixture
async def mongomock_db():
    client = AsyncMongoMockClient()
    db = client["test_aloft"]
    await ensure_indexes(db)
    return db


@pytest.fixture
def test_client(mongomock_db, auth_override) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: mongomock_db
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient()
    yield TestClient(app)
    app.dependency_overrides.clear()


# ── Static airport lookup (pure, no I/O) ─────────────────────────────────────


def test_lookup_static_airport_returns_coords_for_known_code():
    coords = lookup_static_airport("ADD")
    assert coords is not None
    lat, lng = coords
    # Addis Ababa is in Ethiopia: ~9°N, ~38°E
    assert 8.0 < lat < 10.0
    assert 37.0 < lng < 40.0


def test_lookup_static_airport_is_case_insensitive():
    assert lookup_static_airport("add") == lookup_static_airport("ADD")


def test_lookup_static_airport_returns_none_for_unknown_code():
    assert lookup_static_airport("ZZZ") is None


def test_lookup_static_airport_covers_dxb():
    coords = lookup_static_airport("DXB")
    assert coords is not None
    lat, lng = coords
    # Dubai: ~25°N, ~55°E
    assert 24.0 < lat < 27.0
    assert 54.0 < lng < 57.0


# ── lat/lng path (existing, must still work) ─────────────────────────────────


def test_health_check():
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_discover_pois_returns_counts_and_persists(test_client, mongomock_db):
    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=FIXED_RESPONSE))
        response = test_client.post(
            "/v1/routes/pois",
            json={"departure": ADD, "arrival": DXB, "width_km": 20},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["pois_found"] == 2
    assert body["pois_newly_inserted"] == 2


def test_discover_pois_rerun_does_not_duplicate(test_client):
    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=FIXED_RESPONSE))
        test_client.post("/v1/routes/pois", json={"departure": ADD, "arrival": DXB, "width_km": 20})
        second = test_client.post(
            "/v1/routes/pois", json={"departure": ADD, "arrival": DXB, "width_km": 20}
        )

    assert second.json()["pois_found"] == 2
    assert second.json()["pois_newly_inserted"] == 0


def test_discover_pois_rejects_degenerate_route_with_400(test_client):
    response = test_client.post(
        "/v1/routes/pois", json={"departure": ADD, "arrival": ADD, "width_km": 20}
    )
    assert response.status_code == 400
    assert "same point" in response.json()["detail"]


def test_discover_pois_rejects_invalid_width_with_422(test_client):
    # width_km < 0.1 is rejected by Pydantic Field validation (ge=0.1)
    response = test_client.post(
        "/v1/routes/pois", json={"departure": ADD, "arrival": DXB, "width_km": -5}
    )
    assert response.status_code == 422


def test_discover_pois_rejects_malformed_request_body(test_client):
    # missing arrival entirely
    response = test_client.post("/v1/routes/pois", json={"departure": ADD})
    assert response.status_code == 422


def test_discover_pois_returns_a_usable_route_key(test_client):
    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=FIXED_RESPONSE))
        response = test_client.post(
            "/v1/routes/pois", json={"departure": ADD, "arrival": DXB, "width_km": 20}
        )

    from app.services.route_bundle_repository import make_route_key

    expected_key = make_route_key((ADD["lat"], ADD["lng"]), (DXB["lat"], DXB["lng"]))
    assert response.json()["route_key"] == expected_key


# ── IATA path (new) ───────────────────────────────────────────────────────────


def test_discover_pois_by_iata_uses_static_dataset(test_client):
    """ADD and DXB are in the static dataset -- no AviationStack call needed."""
    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=FIXED_RESPONSE))
        response = test_client.post(
            "/v1/routes/pois",
            json={"departure_iata": "ADD", "arrival_iata": "DXB", "width_km": 20},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["pois_found"] == 2
    # Coords come from the static dataset -- check they're in the right ballpark
    dep_lat, dep_lng = body["departure"]
    assert 8.0 < dep_lat < 10.0  # Addis Ababa latitude
    assert 37.0 < dep_lng < 40.0  # Addis Ababa longitude


def test_discover_pois_by_iata_is_case_insensitive(test_client):
    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=FIXED_RESPONSE))
        response = test_client.post(
            "/v1/routes/pois",
            json={"departure_iata": "add", "arrival_iata": "dxb"},
        )

    assert response.status_code == 200


def test_discover_pois_by_iata_uses_db_cache_when_not_in_static_dataset(test_client, mongomock_db):
    """An IATA code not in the static dataset should still work if it's
    been cached in the DB from a prior AviationStack lookup.
    """

    async def seed_cache():
        # ZZZ is a made-up code, definitely not in the static dataset
        await save_airport(
            mongomock_db,
            Airport(iata_code="ZZZ", name="Fake Airport", lat=10.0, lng=20.0),
        )
        await save_airport(
            mongomock_db,
            Airport(iata_code="ZZY", name="Other Fake", lat=11.0, lng=21.0),
        )

    import asyncio

    asyncio.get_event_loop().run_until_complete(seed_cache())

    with respx.mock:
        respx.get(WIKIPEDIA_API_URL).mock(return_value=httpx.Response(200, json=FIXED_RESPONSE))
        response = test_client.post(
            "/v1/routes/pois",
            json={"departure_iata": "ZZZ", "arrival_iata": "ZZY"},
        )

    assert response.status_code == 200


def test_discover_pois_by_unknown_iata_returns_422(test_client):
    """IATA code not in static dataset and not in DB cache -> 422 with helpful message."""
    response = test_client.post(
        "/v1/routes/pois",
        json={"departure_iata": "ZZZ", "arrival_iata": "ZZY"},
    )

    assert response.status_code == 422
    assert "ZZZ" in response.json()["detail"]
    assert "lat/lng" in response.json()["detail"]


def test_discover_pois_rejects_mixing_coords_and_iata(test_client):
    """Providing both lat/lng and IATA codes at the same time is an error."""
    response = test_client.post(
        "/v1/routes/pois",
        json={
            "departure": ADD,
            "arrival": DXB,
            "departure_iata": "ADD",
            "arrival_iata": "DXB",
        },
    )
    assert response.status_code == 422


def test_discover_pois_rejects_partial_iata_input(test_client):
    """Providing only one IATA code (no arrival) is rejected."""
    response = test_client.post(
        "/v1/routes/pois",
        json={"departure_iata": "ADD"},
    )
    assert response.status_code == 422
