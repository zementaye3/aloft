import pytest
from mongomock_motor import AsyncMongoMockClient

from app.core.db import ensure_indexes
from app.services.route_bundle_repository import (
    get_route_bundle,
    make_route_key,
    save_route_bundle,
)

ADD = (8.9806, 38.7992)
DXB = (25.2532, 55.3657)


@pytest.fixture
async def db():
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


def test_make_route_key_is_deterministic():
    assert make_route_key(ADD, DXB) == make_route_key(ADD, DXB)


def test_make_route_key_rounds_to_tolerate_tiny_float_differences():
    almost_same_add = (8.98061, 38.79924)
    assert make_route_key(ADD, DXB) == make_route_key(almost_same_add, DXB)


def test_make_route_key_differs_for_different_routes():
    other_arrival = (51.4700, -0.4543)
    assert make_route_key(ADD, DXB) != make_route_key(ADD, other_arrival)


@pytest.mark.asyncio
async def test_save_and_get_route_bundle_roundtrip(db):
    await save_route_bundle(db, ADD, DXB, ["wikipedia:1001", "wikipedia:1002"])
    bundle = await get_route_bundle(db, make_route_key(ADD, DXB))

    assert bundle is not None
    assert bundle.poi_source_ids == ["wikipedia:1001", "wikipedia:1002"]
    assert bundle.departure == ADD
    assert bundle.arrival == DXB


@pytest.mark.asyncio
async def test_get_route_bundle_returns_none_when_not_found(db):
    assert await get_route_bundle(db, "no-such-route") is None


@pytest.mark.asyncio
async def test_save_route_bundle_replaces_poi_list_on_rerun(db):
    await save_route_bundle(db, ADD, DXB, ["wikipedia:1001"])
    await save_route_bundle(db, ADD, DXB, ["wikipedia:1001", "wikipedia:9999"])

    bundle = await get_route_bundle(db, make_route_key(ADD, DXB))

    assert bundle.poi_source_ids == ["wikipedia:1001", "wikipedia:9999"]
    assert await db.route_bundles.count_documents({}) == 1
