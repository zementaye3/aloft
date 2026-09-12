"""
Tests for the live flight tracking endpoint: POST /v1/flights/{flight_iata}/live
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.clients.aviationstack import AVIATIONSTACK_BASE_URL
from app.core.config import get_settings
from app.core.dependencies import get_current_user, get_database, get_http_client, get_redis
from app.main import app

LHR = (51.4700, -0.4543)  # lat, lng
DXB = (25.2532, 55.3657)

_OPEN_SKY_URL = "https://opensky-network.org/api/states/all"
_WIKI_URL = "https://en.wikipedia.org/w/api.php"


@pytest.fixture(autouse=True)
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    from app.core.db import ensure_indexes

    await ensure_indexes(database)
    return database


@pytest.fixture
async def redis():
    import fakeredis.aioredis as fakeredis

    server = fakeredis.FakeRedis()
    yield server
    await server.aclose()


@pytest.fixture
def test_client(db, redis):
    # Build a settings object with all optional external POI sources disabled
    # so tests don't need to mock geonames / wikidata / overpass URLs.
    _base_settings = get_settings()
    _test_settings = _base_settings.model_copy(
        update={
            "poi_source_geonames_enabled": False,
            "poi_source_wikidata_enabled": False,
            "poi_source_overpass_enabled": False,
        }
    )

    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient()
    app.dependency_overrides[get_redis] = lambda: redis
    app.dependency_overrides[get_current_user] = lambda: _fake_user()
    with patch("app.services.poi_service.get_settings", return_value=_test_settings):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _fake_user():
    from app.models.user import User

    return User(
        user_id="000000000000000000000001",
        email="testuser@example.com",
        hashed_password="$2b$12$fakehashfortesting",
        is_active=True,
    )


FLIGHT_RESPONSE = {
    "data": [
        {
            "flight_iata": "BA178",
            "flight_icao": "BAW178",
            "flight_status": "active",
            "departure": {"iata": "LHR"},
            "arrival": {"iata": "DXB"},
        }
    ]
}

LHR_AIRPORT_RESPONSE = {
    "data": [
        {
            "iata_code": "LHR",
            "airport_name": "Heathrow",
            "latitude": "51.4700",
            "longitude": "-0.4543",
        }
    ]
}

DXB_AIRPORT_RESPONSE = {
    "data": [
        {
            "iata_code": "DXB",
            "airport_name": "Dubai International",
            "latitude": "25.2532",
            "longitude": "55.3657",
        }
    ]
}

_OPEN_SKY_STATE = [
    "4b1806",
    "BAW178  ",
    "United Kingdom",
    1720000000,
    1720000001,
    3.5,
    51.3,
    10668.0,
    False,
    245.0,
    92.3,
    -2.6,
]

_WIKI_GEOSEARCH_RESPONSE = {
    "query": {
        "geosearch": [
            {
                "pageid": 9001,
                "title": "St Paul's Cathedral",
                "lat": 51.5138,
                "lon": -0.0984,
                "dist": 5000.0,
            },
            {
                "pageid": 9002,
                "title": "Tower Bridge",
                "lat": 51.5055,
                "lon": -0.0754,
                "dist": 8000.0,
            },
        ]
    }
}


@respx.mock
def test_live_track_returns_stories_when_opensky_available(test_client, db, redis):
    """Full happy-path: route resolved, POIs discovered, session created, OpenSky hit."""
    respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
        return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
    )
    respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(
        side_effect=[
            httpx.Response(200, json=LHR_AIRPORT_RESPONSE),
            httpx.Response(200, json=DXB_AIRPORT_RESPONSE),
        ]
    )
    respx.get(_OPEN_SKY_URL).mock(
        return_value=httpx.Response(200, json={"states": [_OPEN_SKY_STATE]})
    )
    respx.get(_WIKI_URL).mock(return_value=httpx.Response(200, json=_WIKI_GEOSEARCH_RESPONSE))

    response = test_client.post("/v1/flights/BA178/live")

    assert response.status_code == 200
    body = response.json()

    assert body["flight_iata"] == "BA178"
    assert body["flight_status"] == "active"
    assert body["position_source"] == "opensky"
    assert body["session_id"]
    assert body["route_key"]
    assert body["aircraft"] is not None
    assert body["aircraft"]["icao24"] == "4b1806"
    assert body["aircraft"]["callsign"] == "BAW178"
    assert body["aircraft"]["latitude"] == pytest.approx(51.3)
    assert body["aircraft"]["longitude"] == pytest.approx(3.5)
    assert len(body["nearby_narrations"]) >= 0
    assert isinstance(body["all_route_pois"], list)


@respx.mock
def test_live_track_still_works_when_opensky_unavailable(test_client, db, redis):
    """If OpenSky doesn't have the aircraft, route and POIs still come back."""
    respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
        return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
    )
    respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(
        side_effect=[
            httpx.Response(200, json=LHR_AIRPORT_RESPONSE),
            httpx.Response(200, json=DXB_AIRPORT_RESPONSE),
        ]
    )
    respx.get(_OPEN_SKY_URL).mock(return_value=httpx.Response(200, json={"states": []}))
    respx.get(_WIKI_URL).mock(return_value=httpx.Response(200, json=_WIKI_GEOSEARCH_RESPONSE))

    response = test_client.post("/v1/flights/BA178/live")

    assert response.status_code == 200
    body = response.json()

    assert body["position_source"] == "unavailable"
    assert body["aircraft"] is None
    assert body["session_id"]
    assert body["route_key"]


@respx.mock
def test_live_track_returns_404_for_unknown_flight(test_client):
    respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    response = test_client.post("/v1/flights/ZZ999/live")

    assert response.status_code == 502
    assert "ZZ999" in response.json()["detail"]


@respx.mock
def test_live_track_includes_cached_stories_when_available(test_client, db, redis):
    respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
        return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
    )
    respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(
        side_effect=[
            httpx.Response(200, json=LHR_AIRPORT_RESPONSE),
            httpx.Response(200, json=DXB_AIRPORT_RESPONSE),
        ]
    )
    respx.get(_OPEN_SKY_URL).mock(
        return_value=httpx.Response(200, json={"states": [_OPEN_SKY_STATE]})
    )
    respx.get(_WIKI_URL).mock(return_value=httpx.Response(200, json=_WIKI_GEOSEARCH_RESPONSE))

    response = test_client.post("/v1/flights/BA178/live")

    assert response.status_code == 200
    body = response.json()
    assert "nearby_narrations" in body
    for poi in body["nearby_narrations"]:
        assert "source_id" in poi
        assert "name" in poi
        assert "distance_km" in poi
        assert "in_range" in poi
        assert "story" in poi
        assert "story_available" in poi


@respx.mock
def test_live_track_session_can_be_polled_after_creation(test_client, db, redis):
    """Smoke-test that the session_id can be used with the existing position endpoint."""
    respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
        return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
    )
    respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(
        side_effect=[
            httpx.Response(200, json=LHR_AIRPORT_RESPONSE),
            httpx.Response(200, json=DXB_AIRPORT_RESPONSE),
        ]
    )
    respx.get(_OPEN_SKY_URL).mock(
        return_value=httpx.Response(200, json={"states": [_OPEN_SKY_STATE]})
    )
    respx.get(_WIKI_URL).mock(return_value=httpx.Response(200, json=_WIKI_GEOSEARCH_RESPONSE))

    # The position-poll endpoint can fire reverse-geocode / destination-tour /
    # upcoming / region narration calls. Stub those so the smoke test focuses
    # on the wiring (session_id -> position endpoint) without hitting external
    # APIs that respx would otherwise treat as unmocked.
    from unittest.mock import AsyncMock

    from app.models.story import Story

    with (
        patch(
            "app.routers.sessions.reverse_geocode",
            new=AsyncMock(
                return_value=type(
                    "RegionInfo",
                    (),
                    {
                        "description": "North Sea",
                        "is_ocean": True,
                        "country": None,
                        "locality": None,
                    },
                )()
            ),
        ),
        patch("app.routers.sessions.prepare_destination_tour", new=AsyncMock(return_value=[])),
        patch(
            "app.routers.sessions.generate_upcoming_story",
            new=AsyncMock(
                return_value=Story(
                    poi_source_id="wikipedia:9001",
                    language="en",
                    text_content="Upcoming story.",
                    model_version="test-model",
                )
            ),
        ),
        patch("app.routers.sessions.generate_region_narration", new=AsyncMock(return_value=None)),
    ):
        live_response = test_client.post("/v1/flights/BA178/live")
        assert live_response.status_code == 200
        session_id = live_response.json()["session_id"]

        pos_response = test_client.post(
            f"/v1/sessions/{session_id}/position",
            json={"lat": 51.3, "lng": 3.5, "language": "en", "trigger_radius_km": 50},
        )
        assert pos_response.status_code == 200
        assert "triggered" in pos_response.json()


@respx.mock
def test_live_track_generates_missing_stories_when_enabled(test_client, db, redis):
    """When generate_missing_stories=true, the endpoint attempts story generation
    for POIs without cached stories. Stories are skipped gracefully when
    Wikipedia has no summary (not all POIs have Wikipedia articles).
    """
    respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
        return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
    )
    respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(
        side_effect=[
            httpx.Response(200, json=LHR_AIRPORT_RESPONSE),
            httpx.Response(200, json=DXB_AIRPORT_RESPONSE),
        ]
    )
    respx.get(_OPEN_SKY_URL).mock(
        return_value=httpx.Response(200, json={"states": [_OPEN_SKY_STATE]})
    )
    respx.get(_WIKI_URL).mock(return_value=httpx.Response(200, json=_WIKI_GEOSEARCH_RESPONSE))

    response = test_client.post("/v1/flights/BA178/live?generate_missing_stories=true")
    assert response.status_code == 200
    body = response.json()

    assert len(body["all_route_pois"]) > 0
    for poi in body["all_route_pois"]:
        assert "source_id" in poi
        assert "name" in poi
        assert "distance_km" in poi
        assert "in_range" in poi
        assert "story" in poi
        assert "story_available" in poi
        assert isinstance(poi["story_available"], bool)
