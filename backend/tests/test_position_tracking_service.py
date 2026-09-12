from app.models.poi import Poi
from app.services.position_tracking_service import find_next_poi_to_narrate, find_next_upcoming_poi

# A point along the real ADD->DXB route, used as "current position".
CURRENT_POS = (12.5, 43.5)


def _poi(source_id: str, lat: float, lng: float) -> Poi:
    return Poi(
        name=source_id,
        source="wikipedia",
        source_id=source_id,
        location={"type": "Point", "coordinates": [lng, lat]},
    )


def test_finds_poi_within_trigger_radius():
    nearby = _poi("wikipedia:1", CURRENT_POS[0] + 0.02, CURRENT_POS[1] + 0.02)  # ~3km away

    result = find_next_poi_to_narrate(*CURRENT_POS, [nearby], set())

    assert result is not None
    assert result.source_id == "wikipedia:1"


def test_ignores_poi_outside_trigger_radius():
    far_away = _poi("wikipedia:1", CURRENT_POS[0] + 5, CURRENT_POS[1] + 5)  # hundreds of km

    assert find_next_poi_to_narrate(*CURRENT_POS, [far_away], set()) is None


def test_returns_none_when_route_has_no_pois():
    assert find_next_poi_to_narrate(*CURRENT_POS, [], set()) is None


def test_skips_already_narrated_poi():
    nearby = _poi("wikipedia:1", CURRENT_POS[0] + 0.01, CURRENT_POS[1] + 0.01)

    assert find_next_poi_to_narrate(*CURRENT_POS, [nearby], {"wikipedia:1"}) is None


def test_returns_closest_when_multiple_pois_in_range():
    closer = _poi("wikipedia:close", CURRENT_POS[0] + 0.01, CURRENT_POS[1] + 0.01)
    farther = _poi("wikipedia:far", CURRENT_POS[0] + 0.05, CURRENT_POS[1] + 0.05)

    # farther is listed FIRST -- distance decides, not list order.
    result = find_next_poi_to_narrate(*CURRENT_POS, [farther, closer], set())

    assert result.source_id == "wikipedia:close"


def test_already_narrated_poi_does_not_block_different_nearby_poi():
    narrated = _poi("wikipedia:done", CURRENT_POS[0] + 0.01, CURRENT_POS[1] + 0.01)
    pending = _poi("wikipedia:pending", CURRENT_POS[0] + 0.02, CURRENT_POS[1] + 0.02)

    result = find_next_poi_to_narrate(*CURRENT_POS, [narrated, pending], {"wikipedia:done"})

    assert result.source_id == "wikipedia:pending"


def test_respects_custom_trigger_radius():
    moderately_close = _poi("wikipedia:1", CURRENT_POS[0] + 0.05, CURRENT_POS[1] + 0.05)

    assert find_next_poi_to_narrate(*CURRENT_POS, [moderately_close], set()) is not None
    assert (
        find_next_poi_to_narrate(*CURRENT_POS, [moderately_close], set(), trigger_radius_km=1.0)
        is None
    )


def test_reads_geojson_lng_lat_order_correctly():
    """Poi.location stores [lng, lat] (GeoJSON), opposite of (lat, lng) params.
    Getting this backwards silently matches completely wrong points as nearby.
    """
    poi_lat, poi_lng = 9.0, 38.0
    poi = _poi("wikipedia:1", poi_lat, poi_lng)

    result = find_next_poi_to_narrate(poi_lat, poi_lng, [poi], set())

    assert result is not None
    assert result.source_id == "wikipedia:1"


# --- Tests for find_next_upcoming_poi ---


def test_finds_upcoming_poi_within_lookahead():
    """POI within 300km should be found as upcoming."""
    # ~200km away (within default 300km lookahead)
    upcoming = _poi("wikipedia:upcoming", CURRENT_POS[0] + 1.8, CURRENT_POS[1] + 1.8)

    result = find_next_upcoming_poi(*CURRENT_POS, [upcoming], set())

    assert result is not None
    poi, distance = result
    assert poi.source_id == "wikipedia:upcoming"
    assert distance > 0


def test_ignores_poi_outside_lookahead():
    """POI beyond 300km should not be found as upcoming."""
    # ~500km away (beyond default 300km lookahead)
    far_away = _poi("wikipedia:far", CURRENT_POS[0] + 4.5, CURRENT_POS[1] + 4.5)

    result = find_next_upcoming_poi(*CURRENT_POS, [far_away], set())

    assert result is None


def test_skips_already_narrated_upcoming_poi():
    """POIs already narrated should not be found as upcoming."""
    upcoming = _poi("wikipedia:upcoming", CURRENT_POS[0] + 1.0, CURRENT_POS[1] + 1.0)

    result = find_next_upcoming_poi(*CURRENT_POS, [upcoming], {"wikipedia:upcoming"})

    assert result is None


def test_returns_closest_upcoming_poi():
    """When multiple POIs are within lookahead, return the closest."""
    closer = _poi("wikipedia:close", CURRENT_POS[0] + 0.5, CURRENT_POS[1] + 0.5)
    farther = _poi("wikipedia:far", CURRENT_POS[0] + 2.0, CURRENT_POS[1] + 2.0)

    result = find_next_upcoming_poi(*CURRENT_POS, [farther, closer], set())

    assert result is not None
    poi, distance = result
    assert poi.source_id == "wikipedia:close"


def test_upcoming_respects_custom_lookahead():
    """Custom lookahead distance should be respected."""
    moderately_close = _poi("wikipedia:1", CURRENT_POS[0] + 1.5, CURRENT_POS[1] + 1.5)

    # Should be found with 300km lookahead
    assert find_next_upcoming_poi(*CURRENT_POS, [moderately_close], set()) is not None

    # Should NOT be found with 100km lookahead
    assert (
        find_next_upcoming_poi(*CURRENT_POS, [moderately_close], set(), lookahead_km=100.0) is None
    )


def test_upcoming_returns_none_when_no_pois():
    """No POIs in route should return None."""
    result = find_next_upcoming_poi(*CURRENT_POS, [], set())
    assert result is None


def test_upcoming_includes_already_narrated_in_exclusion_set():
    """Both narrated and upcoming-triggered POIs should be excluded."""
    narrated = _poi("wikipedia:narrated", CURRENT_POS[0] + 0.5, CURRENT_POS[1] + 0.5)
    upcoming_triggered = _poi("wikipedia:upcoming", CURRENT_POS[0] + 1.0, CURRENT_POS[1] + 1.0)
    pending = _poi("wikipedia:pending", CURRENT_POS[0] + 1.5, CURRENT_POS[1] + 1.5)

    excluded = {"wikipedia:narrated", "wikipedia:upcoming"}
    result = find_next_upcoming_poi(*CURRENT_POS, [narrated, upcoming_triggered, pending], excluded)

    assert result is not None
    poi, _ = result
    assert poi.source_id == "wikipedia:pending"
