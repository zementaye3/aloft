"""
End-to-end tests for the sessions router.
Sessions now live in Redis -- fakeredis provides the in-process fake.
MongoDB (mongomock) is still needed for route bundles, POIs, and stories.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis as fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.clients.geocoding_client import RegionInfo
from app.clients.wikipedia import RawPoi
from app.core.db import ensure_indexes
from app.core.dependencies import get_current_user, get_database, get_http_client, get_redis
from app.main import app
from app.models.role import Role
from app.models.story import Story
from app.models.user import User
from app.services.poi_repository import save_pois
from app.services.route_bundle_repository import save_route_bundle
from app.services.story_repository import save_story

ADD = (8.9806, 38.7992)
DXB = (25.2532, 55.3657)

NEARBY_POI = RawPoi(title="Cathedral", page_id=1001, lat=9.0, lng=38.0, distance_m=100)


@pytest.fixture(autouse=True)
def stub_external_services():
    """Make the session/position endpoints hermetic.

    start_session reverse-geocodes the arrival point and pre-generates a
    destination tour (Groq); the position endpoint can fire an upcoming-story
    generation (Groq + Wikipedia) or a region narration. None of those should
    hit the real network -- we stub them so position-trigger logic is tested
    in isolation and the tests run without external services.

    Stubbing prepare_destination_tour to return [] is also what makes the
    "a narrated POI does not retrigger" assertions hold: a freshly-narrated
    POI won't immediately also play a destination-tour narration on the next
    ping.
    """
    with (
        patch(
            "app.routers.sessions.reverse_geocode",
            new=AsyncMock(
                return_value=RegionInfo(
                    description="Addis Ababa, Ethiopia",
                    is_ocean=False,
                    country="Ethiopia",
                    locality="Addis Ababa",
                )
            ),
        ),
        patch(
            "app.routers.sessions.prepare_destination_tour",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "app.routers.sessions.generate_upcoming_story",
            new=AsyncMock(
                return_value=Story(
                    poi_source_id="wikipedia:1001",
                    language="en",
                    text_content="An upcoming story about the cathedral.",
                    model_version="test-model",
                )
            ),
        ),
    ):
        yield


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


@pytest.fixture
async def redis():
    server = fakeredis.FakeRedis()
    yield server
    await server.aclose()


@pytest.fixture
def test_client(db, redis, auth_override) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient()
    app.dependency_overrides[get_redis] = lambda: redis
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_start_session_404_for_unknown_route(test_client):
    response = test_client.post("/v1/sessions", json={"route_key": "no-such-route"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_start_session_succeeds_for_known_route(test_client, db):
    await save_pois(db, [NEARBY_POI])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])

    response = test_client.post("/v1/sessions", json={"route_key": bundle.route_key})

    assert response.status_code == 200
    body = response.json()
    assert body["route_key"] == bundle.route_key
    assert "session_id" in body


def test_position_update_404_for_unknown_session(test_client):
    response = test_client.post(
        "/v1/sessions/no-such-session/position", json={"lat": 9.0, "lng": 38.0}
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_position_update_triggers_nearby_poi_with_story(test_client, db):
    await save_pois(db, [NEARBY_POI])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_story(
        db,
        Story(
            poi_source_id="wikipedia:1001",
            language="en",
            text_content="A vivid story about the cathedral.",
            model_version="test-model",
        ),
    )
    session_id = test_client.post("/v1/sessions", json={"route_key": bundle.route_key}).json()[
        "session_id"
    ]

    response = test_client.post(
        f"/v1/sessions/{session_id}/position", json={"lat": 9.0, "lng": 38.0}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["triggered"] is True
    assert body["narration"]["source_id"] == "wikipedia:1001"
    assert body["narration"]["text_content"] == "A vivid story about the cathedral."
    assert body["narration"]["narration_type"] == "poi"
    assert body["position_source"] == "client"
    assert body["lat_used"] == 9.0
    assert body["lng_used"] == 38.0


@pytest.mark.asyncio
async def test_position_update_not_triggered_when_nothing_nearby(test_client, db):
    await save_pois(db, [NEARBY_POI])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    session_id = test_client.post("/v1/sessions", json={"route_key": bundle.route_key}).json()[
        "session_id"
    ]

    # Mock region narration to prevent tier 3 fallback
    with patch("app.routers.sessions.generate_region_narration") as mock_region:
        mock_region.side_effect = Exception("Region narration disabled for test")

        response = test_client.post(
            f"/v1/sessions/{session_id}/position", json={"lat": 0.0, "lng": 0.0}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["triggered"] is False
    assert body["narration"] is None
    assert body["position_source"] == "client"


@pytest.mark.asyncio
async def test_same_poi_does_not_retrigger_on_second_nearby_ping(test_client, db):
    await save_pois(db, [NEARBY_POI])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_story(
        db,
        Story(
            poi_source_id="wikipedia:1001",
            language="en",
            text_content="Story text.",
            model_version="test-model",
        ),
    )
    session_id = test_client.post("/v1/sessions", json={"route_key": bundle.route_key}).json()[
        "session_id"
    ]

    # Mock region narration to prevent tier 3 fallback
    with patch("app.routers.sessions.generate_region_narration") as mock_region:
        mock_region.side_effect = Exception("Region narration disabled for test")

        first = test_client.post(
            f"/v1/sessions/{session_id}/position", json={"lat": 9.0, "lng": 38.0}
        )
        second = test_client.post(
            f"/v1/sessions/{session_id}/position", json={"lat": 9.0, "lng": 38.0}
        )

    assert first.json()["triggered"] is True
    assert second.json()["triggered"] is False


@pytest.mark.asyncio
async def test_triggered_poi_with_no_story_still_marks_narrated(test_client, db):
    await save_pois(db, [NEARBY_POI])
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    session_id = test_client.post("/v1/sessions", json={"route_key": bundle.route_key}).json()[
        "session_id"
    ]

    # Mock region narration to prevent tier 3 fallback
    with patch("app.routers.sessions.generate_region_narration") as mock_region:
        mock_region.side_effect = Exception("Region narration disabled for test")

        first = test_client.post(
            f"/v1/sessions/{session_id}/position", json={"lat": 9.0, "lng": 38.0}
        )
        second = test_client.post(
            f"/v1/sessions/{session_id}/position", json={"lat": 9.0, "lng": 38.0}
        )

    assert first.json()["triggered"] is True
    assert first.json()["narration"]["text_content"] is None
    assert first.json()["narration"]["narration_type"] == "poi"
    assert second.json()["triggered"] is False


_OTHER_USER = User(
    user_id="000000000000000000000002",
    email="otheruser@example.com",
    hashed_password="$2b$12$fakehashfortesting",
    is_active=True,
    is_verified=True,
    role=Role.USER,
)


async def _start_session(test_client, db, route_key: str | None = None) -> str:
    """Save a discoverable route and start a session against it, returning its id."""
    if route_key is None:
        await save_pois(db, [NEARBY_POI])
        bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
        route_key = bundle.route_key
    response = test_client.post("/v1/sessions", json={"route_key": route_key})
    return response.json()["session_id"]


class TestSpectatorSharing:
    @pytest.mark.asyncio
    async def test_share_session_returns_a_token(self, test_client, db):
        session_id = await _start_session(test_client, db)

        response = test_client.post(f"/v1/sessions/{session_id}/share")

        assert response.status_code == 200
        body = response.json()
        assert body["share_token"]
        assert body["share_path"] == f"/v1/sessions/shared/{body['share_token']}"

    @pytest.mark.asyncio
    async def test_share_session_is_idempotent(self, test_client, db):
        session_id = await _start_session(test_client, db)

        first = test_client.post(f"/v1/sessions/{session_id}/share").json()
        second = test_client.post(f"/v1/sessions/{session_id}/share").json()

        assert first["share_token"] == second["share_token"]

    def test_share_session_404_for_unknown_session(self, test_client):
        response = test_client.post("/v1/sessions/no-such-session/share")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_share_session_403_for_non_owner(self, test_client, db):
        session_id = await _start_session(test_client, db)

        app.dependency_overrides[get_current_user] = lambda: _OTHER_USER
        response = test_client.post(f"/v1/sessions/{session_id}/share")

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_spectator_can_view_shared_session(self, test_client, db):
        await save_pois(db, [NEARBY_POI])
        bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
        await save_story(
            db,
            Story(
                poi_source_id="wikipedia:1001",
                language="en",
                text_content="A vivid story about the cathedral.",
                model_version="test-model",
            ),
        )
        session_id = await _start_session(test_client, db, route_key=bundle.route_key)
        test_client.post(f"/v1/sessions/{session_id}/position", json={"lat": 9.0, "lng": 38.0})
        token = test_client.post(f"/v1/sessions/{session_id}/share").json()["share_token"]

        response = test_client.get(f"/v1/sessions/shared/{token}")

        assert response.status_code == 200
        body = response.json()
        assert body["route_key"] == bundle.route_key
        assert body["last_position"] == [9.0, 38.0]
        assert len(body["narrations"]) == 1
        assert body["narrations"][0]["source_id"] == "wikipedia:1001"
        assert body["narrations"][0]["text_content"] == "A vivid story about the cathedral."
        assert body["narrations"][0]["narration_type"] == "poi"

    def test_spectator_view_404_for_invalid_token(self, test_client):
        response = test_client.get("/v1/sessions/shared/no-such-token")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_spectator_view_requires_no_auth(self, test_client, db):
        """The public view must work with dependency_overrides for auth cleared --
        i.e. it must not depend on get_current_user at all.
        """
        session_id = await _start_session(test_client, db)
        token = test_client.post(f"/v1/sessions/{session_id}/share").json()["share_token"]

        app.dependency_overrides.pop(get_current_user, None)
        response = test_client.get(f"/v1/sessions/shared/{token}")

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unshare_session_revokes_the_token(self, test_client, db):
        session_id = await _start_session(test_client, db)
        token = test_client.post(f"/v1/sessions/{session_id}/share").json()["share_token"]

        delete_response = test_client.delete(f"/v1/sessions/{session_id}/share")
        view_response = test_client.get(f"/v1/sessions/shared/{token}")

        assert delete_response.status_code == 204
        assert view_response.status_code == 404

    def test_unshare_session_404_for_unknown_session(self, test_client):
        response = test_client.delete("/v1/sessions/no-such-session/share")

        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_unshare_session_403_for_non_owner(self, test_client, db):
        session_id = await _start_session(test_client, db)

        app.dependency_overrides[get_current_user] = lambda: _OTHER_USER
        response = test_client.delete(f"/v1/sessions/{session_id}/share")

        assert response.status_code == 403
