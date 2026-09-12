"""
Tests for POI curation service.
"""

from app.clients.wikipedia import RawPoi
from app.services.poi_curator import ScoredPoi, _score_poi, _too_close_to_existing, curate_pois


def test_curate_pois_small_list():
    """When there are fewer POIs than max_pois, return all of them."""
    pois = [
        RawPoi(title="Place A", page_id=1, lat=52.5, lng=13.4, distance_m=100),
        RawPoi(title="Place B", page_id=2, lat=52.6, lng=13.5, distance_m=200),
    ]
    departure = (52.5, 13.4)
    arrival = (52.6, 13.5)

    result = curate_pois(pois, departure, arrival, max_pois=10)
    assert len(result) == 2
    assert result == pois


def test_curate_pois_large_list():
    """When there are more POIs than max_pois, return only the best ones."""
    pois = [
        RawPoi(
            title=f"Place {i}",
            page_id=i,
            lat=52.5 + i * 0.01,
            lng=13.4 + i * 0.01,
            distance_m=i * 100,
        )
        for i in range(100)
    ]
    departure = (52.5, 13.4)
    arrival = (53.5, 14.4)

    result = curate_pois(pois, departure, arrival, max_pois=10)
    assert len(result) <= 10
    # Result should be sorted by position along route
    lats = [p.lat for p in result]
    assert lats == sorted(lats)


def test_score_poi_proximity():
    """POIs closer to sample points (lower distance_m) get higher scores."""
    poi_close = RawPoi(title="Close Place", page_id=1, lat=52.5, lng=13.4, distance_m=50)
    poi_far = RawPoi(title="Far Place", page_id=2, lat=52.6, lng=13.5, distance_m=5000)
    departure = (52.5, 13.4)
    arrival = (52.6, 13.5)

    scored_close = _score_poi(poi_close, departure, arrival)
    scored_far = _score_poi(poi_far, departure, arrival)

    assert scored_close.score > scored_far.score


def test_score_poi_title_length():
    """POIs with shorter, cleaner titles get higher scores."""
    poi_short = RawPoi(title="London", page_id=1, lat=51.5, lng=-0.1, distance_m=100)
    poi_long = RawPoi(
        title="Small Industrial Estate near Coventry", page_id=2, lat=52.4, lng=-1.5, distance_m=100
    )
    departure = (51.5, -0.1)
    arrival = (52.4, -1.5)

    scored_short = _score_poi(poi_short, departure, arrival)
    scored_long = _score_poi(poi_long, departure, arrival)

    assert scored_short.score > scored_long.score


def test_too_close_to_existing():
    """POIs too close to already-selected ones are rejected."""
    existing = [
        ScoredPoi(
            raw_poi=RawPoi(title="Place A", page_id=1, lat=52.5, lng=13.4, distance_m=100),
            score=50.0,
            position_along_route=0.5,
        )
    ]
    candidate = ScoredPoi(
        raw_poi=RawPoi(title="Place B", page_id=2, lat=52.5001, lng=13.4001, distance_m=100),
        score=45.0,
        position_along_route=0.51,
    )

    # 100m apart, should be rejected (default min spacing is 150km)
    assert _too_close_to_existing(candidate, existing, min_spacing_km=150.0)

    # 200km apart, should be accepted
    candidate_far = ScoredPoi(
        raw_poi=RawPoi(title="Place C", page_id=3, lat=54.5, lng=15.4, distance_m=100),
        score=45.0,
        position_along_route=0.51,
    )
    assert not _too_close_to_existing(candidate_far, existing, min_spacing_km=150.0)


def test_curate_respects_spacing():
    """Curated POIs should be well-spaced along the route."""
    pois = [
        RawPoi(title=f"Place {i}", page_id=i, lat=52.5, lng=13.4 + i * 0.001, distance_m=100)
        for i in range(50)
    ]
    departure = (52.5, 13.4)
    arrival = (52.5, 13.5)

    result = curate_pois(pois, departure, arrival, max_pois=10)

    # Check that no two POIs are too close (within 150km)
    for i in range(len(result)):
        for j in range(i + 1, len(result)):
            from app.services.corridor import distance_km

            dist = distance_km(result[i].lat, result[i].lng, result[j].lat, result[j].lng)
            assert dist >= 150.0, f"POIs {i} and {j} are only {dist}km apart"


def test_curate_sorts_by_position():
    """Curated POIs should be sorted by position along route."""
    pois = [
        RawPoi(title="End", page_id=3, lat=53.0, lng=14.0, distance_m=100),
        RawPoi(title="Start", page_id=1, lat=52.0, lng=13.0, distance_m=100),
        RawPoi(title="Middle", page_id=2, lat=52.5, lng=13.5, distance_m=100),
    ]
    departure = (52.0, 13.0)
    arrival = (53.0, 14.0)

    result = curate_pois(pois, departure, arrival, max_pois=10)

    assert result[0].title == "Start"
    assert result[1].title == "Middle"
    assert result[2].title == "End"
