import pytest
from mongomock_motor import AsyncMongoMockClient

from app.clients.wikipedia import RawPoi
from app.core.db import ensure_indexes
from app.services.poi_repository import (
    _source_and_id_from_raw,
    get_poi,
    save_poi_images,
    save_pois,
)

CATHEDRAL = RawPoi(title="Cathedral", page_id=1001, lat=9.0177, lng=38.7669, distance_m=450.2)
MUSEUM = RawPoi(title="Museum", page_id=1002, lat=9.0339, lng=38.7611, distance_m=1820.7)


@pytest.fixture
async def db():
    """A fresh in-memory database per test, with the same indexes
    production actually runs -- if an index constraint matters, it should
    be exercised here too, not just assumed.
    """
    client = AsyncMongoMockClient()
    database = client["test_aloft"]
    await ensure_indexes(database)
    return database


@pytest.mark.asyncio
async def test_save_pois_inserts_new_pois(db):
    inserted, source_ids = await save_pois(db, [CATHEDRAL, MUSEUM])

    assert inserted == 2
    assert source_ids == ["wikipedia:1001", "wikipedia:1002"]
    assert await db.pois.count_documents({}) == 2


@pytest.mark.asyncio
async def test_save_pois_does_not_duplicate_on_rerun(db):
    await save_pois(db, [CATHEDRAL])
    inserted_second_time, _ = await save_pois(db, [CATHEDRAL])  # same page_id again

    assert inserted_second_time == 0  # already existed -- refreshed, not inserted
    assert await db.pois.count_documents({}) == 1


@pytest.mark.asyncio
async def test_save_pois_stores_correct_geojson_point(db):
    await save_pois(db, [CATHEDRAL])

    doc = await db.pois.find_one({"source_id": "wikipedia:1001"})
    # GeoJSON order is (lng, lat) -- the same gotcha as corridor.py, worth
    # re-verifying here since it's a completely separate piece of code.
    assert doc["location"] == {"type": "Point", "coordinates": [38.7669, 9.0177]}


@pytest.mark.asyncio
async def test_save_pois_refreshes_fields_on_rerun(db):
    stale = RawPoi(title="Old Name", page_id=1001, lat=9.0177, lng=38.7669, distance_m=450.2)
    fresh = RawPoi(title="Corrected Name", page_id=1001, lat=9.0177, lng=38.7669, distance_m=450.2)

    await save_pois(db, [stale])
    await save_pois(db, [fresh])

    doc = await db.pois.find_one({"source_id": "wikipedia:1001"})
    assert doc["name"] == "Corrected Name"
    assert await db.pois.count_documents({}) == 1


@pytest.mark.asyncio
async def test_save_pois_handles_empty_list(db):
    inserted, source_ids = await save_pois(db, [])

    assert inserted == 0
    assert source_ids == []
    assert await db.pois.count_documents({}) == 0


@pytest.mark.asyncio
async def test_get_poi_returns_none_when_not_found(db):
    result = await get_poi(db, "wikipedia:does-not-exist")

    assert result is None


@pytest.mark.asyncio
async def test_get_poi_returns_the_saved_poi(db):
    await save_pois(db, [CATHEDRAL])

    poi = await get_poi(db, "wikipedia:1001")

    assert poi is not None
    assert poi.name == "Cathedral"
    assert poi.source_id == "wikipedia:1001"
    assert poi.location == {"type": "Point", "coordinates": [38.7669, 9.0177]}


@pytest.mark.asyncio
async def test_save_poi_images_updates_image_refs(db):
    await save_pois(db, [CATHEDRAL])

    await save_poi_images(db, "wikipedia:1001", ["https://example.com/photo1.jpg"])

    poi = await get_poi(db, "wikipedia:1001")
    assert poi.image_refs == ["https://example.com/photo1.jpg"]


@pytest.mark.asyncio
async def test_save_poi_images_accepts_empty_list_as_honest_result(db):
    await save_pois(db, [CATHEDRAL])

    await save_poi_images(db, "wikipedia:1001", [])

    poi = await get_poi(db, "wikipedia:1001")
    assert poi.image_refs == []


# ---------------------------------------------------------------------------
# _source_and_id_from_raw: boundary tests for synthetic ID ranges
# ---------------------------------------------------------------------------


def _raw(page_id: int) -> RawPoi:
    return RawPoi(title="Test", page_id=page_id, lat=0.0, lng=0.0, distance_m=0.0)


def test_source_wikipedia_positive_id():
    source, sid = _source_and_id_from_raw(_raw(12345))
    assert source == "wikipedia"
    assert sid == "wikipedia:12345"


def test_source_wikipedia_zero_id():
    # Edge: page_id == 0 is treated as Wikipedia (pid >= 0)
    source, sid = _source_and_id_from_raw(_raw(0))
    assert source == "wikipedia"
    assert sid == "wikipedia:0"


def test_source_wikidata_typical():
    # Q42 → -(1_000_000_000 + 42) = -1_000_000_042
    source, sid = _source_and_id_from_raw(_raw(-1_000_000_042))
    assert source == "wikidata"
    assert "wikidata" in sid


def test_source_wikidata_floor_boundary():
    # -1_999_999_999 is still in Wikidata range (>= _GEONAMES_ID_FLOOR = -2_000_000_000)
    source, _ = _source_and_id_from_raw(_raw(-1_999_999_999))
    assert source == "wikidata"


def test_source_geonames_floor_boundary():
    # -2_000_000_000 is exactly the GeoNames floor
    source, sid = _source_and_id_from_raw(_raw(-2_000_000_000))
    assert source == "geonames"
    assert sid == "geonames:0"


def test_source_geonames_typical():
    # geonames_id 12345 → -(2_000_000_000 + 12345) = -2_000_012_345
    source, sid = _source_and_id_from_raw(_raw(-2_000_012_345))
    assert source == "geonames"
    assert sid == "geonames:12345"


def test_source_overpass_node_floor_boundary():
    # -3_000_000_000 is exactly the Overpass node floor
    source, sid = _source_and_id_from_raw(_raw(-3_000_000_000))
    assert source == "overpass"
    assert sid == "overpass:node:0"


def test_source_overpass_node_typical():
    # osm_id 99 → -(3_000_000_000 + 99)
    source, sid = _source_and_id_from_raw(_raw(-3_000_000_099))
    assert source == "overpass"
    assert sid == "overpass:node:99"


def test_source_overpass_way_floor_boundary():
    source, sid = _source_and_id_from_raw(_raw(-4_000_000_000))
    assert source == "overpass"
    assert sid == "overpass:way:0"


def test_source_overpass_relation_floor_boundary():
    source, sid = _source_and_id_from_raw(_raw(-5_000_000_000))
    assert source == "overpass"
    assert sid == "overpass:relation:0"
