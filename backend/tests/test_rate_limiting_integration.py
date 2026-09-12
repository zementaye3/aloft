"""
Router-level integration tests proving rate limiting fires through real HTTP
for the two protected endpoints, not just at the check_rate_limit() unit
level (see test_rate_limiter.py for that).

Strategy: patch app.core.dependencies._get_rate_limit_redis with a fakeredis
instance. The rate_limit() dependency closure calls _get_rate_limit_redis()
by that name at call-time, so patching the module-level name covers both
endpoint factories without needing to enter the lifespan (which would try a
real MongoDB connection). This matches the pattern every other router test
in this project uses -- bare TestClient(app), no lifespan.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.clients.aviationstack import AVIATIONSTACK_BASE_URL
from app.core.config import get_settings
from app.core.db import ensure_indexes
from app.core.dependencies import get_database, get_http_client, get_redis
from app.main import app
from app.services.route_bundle_repository import save_route_bundle

ADD = (8.9806, 38.7992)
DXB = (25.2532, 55.3657)

FLIGHT_RESPONSE = {
    "data": [
        {
            "flight_status": "scheduled",
            "departure": {"iata": "ADD"},
            "arrival": {"iata": "DXB"},
        }
    ]
}


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


@pytest.fixture
def fake_redis() -> Iterator[fakeredis.aioredis.FakeRedis]:
    """A real fakeredis client, injected via module-level patch so the
    rate_limit() dependency closure picks it up without needing lifespan.
    """
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    with patch("app.core.dependencies._get_rate_limit_redis", return_value=client):
        yield client


@pytest.fixture
def test_client(db, fake_redis, auth_override) -> Iterator[TestClient]:
    app.dependency_overrides[get_database] = lambda: db
    app.dependency_overrides[get_http_client] = lambda: httpx.AsyncClient()
    app.dependency_overrides[get_redis] = lambda: fake_redis
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_flight_lookup_allows_requests_under_the_limit(test_client):
    limit = get_settings().rate_limit_flight_lookups_per_hour

    with respx.mock:
        respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
            return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
        )
        respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(return_value=httpx.Response(404))

        for _ in range(limit):
            response = test_client.post("/v1/flights/ET409/pois")
            assert response.status_code != 429


def test_flight_lookup_returns_429_once_limit_exceeded(test_client):
    limit = get_settings().rate_limit_flight_lookups_per_hour

    with respx.mock:
        respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
            return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
        )
        respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(return_value=httpx.Response(404))

        for _ in range(limit):
            test_client.post("/v1/flights/ET409/pois")

        response = test_client.post("/v1/flights/ET409/pois")

    assert response.status_code == 429
    assert "Retry-After" in response.headers


@pytest.mark.asyncio
async def test_content_generation_returns_429_once_limit_exceeded(test_client, db):
    limit = get_settings().rate_limit_content_generation_per_hour
    bundle = await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])

    # Bypass the real (Redis-backed) job creation so we can exercise the
    # rate-limit dependency in isolation. create_content_job returns a job_id.
    with patch(
        "app.routers.content.create_content_job",
        new=AsyncMock(return_value="fake-job-id"),
    ):
        for _ in range(limit):
            response = test_client.post(f"/v1/routes/{bundle.route_key}/content")
            assert response.status_code == 200

        response = test_client.post(f"/v1/routes/{bundle.route_key}/content")

    assert response.status_code == 429


def test_flight_and_content_limits_are_independent(test_client, fake_redis):
    """Exhausting the flights budget must not affect the content budget.
    The two rate limits are tracked under different Redis keys
    (ratelimit:flights:* vs ratelimit:content:*).
    """
    flight_limit = get_settings().rate_limit_flight_lookups_per_hour

    with respx.mock:
        respx.get(f"{AVIATIONSTACK_BASE_URL}/flights").mock(
            return_value=httpx.Response(200, json=FLIGHT_RESPONSE)
        )
        respx.get(f"{AVIATIONSTACK_BASE_URL}/airports").mock(return_value=httpx.Response(404))

        for _ in range(flight_limit):
            test_client.post("/v1/flights/ET409/pois")
        exhausted = test_client.post("/v1/flights/ET409/pois")

    assert exhausted.status_code == 429

    # The content-generation budget should be completely untouched.
    # A 404 (route not discovered) proves the request reached the route
    # lookup at all -- i.e. it was NOT rejected by rate limiting.
    content_response = test_client.post("/v1/routes/no-such-route/content")
    assert content_response.status_code == 404
