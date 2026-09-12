"""
POI discovery service: finds Points of Interest along a flight corridor.

PRIMARY SOURCE: Wikipedia (always enabled).
  Uses the Wikipedia geosearch API (via app/clients/wikipedia.py) to find
  articles with coordinates within the flight corridor.

ADDITIONAL SOURCES (optional, disabled by default):
  - Wikidata: structured data for millions of places without Wikipedia articles.
    Enable: POI_SOURCE_WIKIDATA_ENABLED=true in .env

  - GeoNames: populated places and geographic features in 11 languages.
    Enable: POI_SOURCE_GEONAMES_ENABLED=true + GEONAMES_USERNAME=<user> in .env

  - OSM Overpass: named physical features (peaks, ruins, airports, stations).
    Enable: POI_SOURCE_OVERPASS_ENABLED=true in .env
    No API key required.

DEDUPLICATION:
  Wikipedia results are deduplicated by page_id (their native ID).
  Wikidata, GeoNames, and Overpass results are deduplicated against Wikipedia
  and each other by a composite key: rounded (lat, lng) at ~100m resolution.
  If a place is found by multiple sources, the Wikipedia entry wins (it has
  more content downstream -- summaries, images, etc.).
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from shapely.geometry import Polygon

from app.clients.geonames import GeoNamesClientError, GeoNamesPoi
from app.clients.geonames import geosearch as geonames_geosearch
from app.clients.overpass import OverpassClientError, OverpassPoi
from app.clients.overpass import geosearch as overpass_geosearch
from app.clients.wikidata import WikidataClientError, WikidataPoi
from app.clients.wikidata import geosearch as wikidata_geosearch
from app.clients.wikipedia import MAX_RADIUS_M, RawPoi, WikipediaClientError, geosearch
from app.core.config import get_settings
from app.services.corridor import build_corridor, point_in_corridor, sample_points_by_spacing

logger = logging.getLogger("aloft.services.poi")

_SAMPLE_OVERLAP_FACTOR = 1.5
_DEFAULT_MAX_CONCURRENT_REQUESTS = 3

# Coordinate rounding for cross-source deduplication.
# ~100m resolution -- two results within 100m of each other are treated as
# the same place when one is from Wikipedia (which wins, for content reasons).
_DEDUP_COORD_PRECISION = 3  # decimal degrees, ~110m at equator


def _coord_key(lat: float, lng: float) -> tuple[float, float]:
    """Stable dedup key at ~100m resolution."""
    return round(lat, _DEDUP_COORD_PRECISION), round(lng, _DEDUP_COORD_PRECISION)


async def find_pois_along_corridor(
    client: httpx.AsyncClient,
    departure: tuple[float, float],
    arrival: tuple[float, float],
    width_km: float = 20.0,
    max_concurrent_requests: int = _DEFAULT_MAX_CONCURRENT_REQUESTS,
) -> list[RawPoi]:
    """Find POIs along the flight corridor from all enabled sources.

    Always queries Wikipedia. Optionally queries Wikidata, GeoNames, and
    OSM Overpass when their feature flags are enabled in settings.
    Additional-source results are merged as synthetic RawPoi entries,
    deduplicated against Wikipedia by coordinate proximity (~100m).
    """
    corridor = build_corridor(departure, arrival, width_km=width_km)

    search_radius_km = min(width_km / 2, MAX_RADIUS_M / 1000)
    if search_radius_km * 2 < width_km:
        logger.warning(
            "width_km=%.0f exceeds single-lane coverage (max %.0fkm) -- only "
            "a %.0fkm-wide band along the centerline will actually be searched.",
            width_km,
            MAX_RADIUS_M / 1000 * 2,
            search_radius_km * 2,
        )

    spacing_km = search_radius_km * _SAMPLE_OVERLAP_FACTOR
    sample_points = sample_points_by_spacing(departure, arrival, spacing_km=spacing_km)
    semaphore = asyncio.Semaphore(max_concurrent_requests)
    radius_m = int(search_radius_km * 1000)

    # --- Wikipedia (primary source, always enabled) ---
    async def _search_one_wikipedia(point: tuple[float, float]) -> list[RawPoi]:
        lat, lng = point
        async with semaphore:
            try:
                return await geosearch(client, lat, lng, radius_m=radius_m)
            except WikipediaClientError as exc:
                logger.warning("Wikipedia: skipping sample point (%s, %s): %s", lat, lng, exc)
                return []

    wikipedia_results = await asyncio.gather(*[_search_one_wikipedia(p) for p in sample_points])
    wikipedia_pois = _dedupe_and_filter(wikipedia_results, corridor)

    # Coordinate keys of Wikipedia results -- used to skip duplicates from
    # additional sources.
    seen_coord_keys: set[tuple[float, float]] = {
        _coord_key(poi.lat, poi.lng) for poi in wikipedia_pois
    }

    settings = get_settings()
    extra_pois: list[RawPoi] = []

    # --- Wikidata (optional) ---
    if settings.poi_source_wikidata_enabled:
        wikidata_pois = await _fetch_wikidata_pois(
            client, sample_points, search_radius_km, corridor, semaphore
        )
        for poi in wikidata_pois:
            key = _coord_key(poi.lat, poi.lng)
            if key not in seen_coord_keys:
                seen_coord_keys.add(key)
                extra_pois.append(
                    RawPoi(
                        title=poi.title,
                        page_id=_wikidata_entity_to_synthetic_id(poi.entity_id),
                        lat=poi.lat,
                        lng=poi.lng,
                        distance_m=0.0,
                    )
                )

    # --- GeoNames (optional) ---
    if settings.poi_source_geonames_enabled:
        username = settings.geonames_username
        if not username:
            logger.warning(
                "POI_SOURCE_GEONAMES_ENABLED=true but GEONAMES_USERNAME is not set "
                "-- skipping GeoNames. Add GEONAMES_USERNAME to .env."
            )
        else:
            geonames_pois = await _fetch_geonames_pois(
                client, sample_points, search_radius_km, corridor, semaphore, username
            )
            for poi in geonames_pois:
                key = _coord_key(poi.lat, poi.lng)
                if key not in seen_coord_keys:
                    seen_coord_keys.add(key)
                    extra_pois.append(
                        RawPoi(
                            title=poi.name,
                            page_id=_geonames_to_synthetic_id(poi.geonames_id),
                            lat=poi.lat,
                            lng=poi.lng,
                            distance_m=poi.distance_km * 1000,
                        )
                    )

    # --- OSM Overpass (optional) ---
    if settings.poi_source_overpass_enabled:
        overpass_pois = await _fetch_overpass_pois(
            client, sample_points, radius_m, corridor, semaphore
        )
        for poi in overpass_pois:
            key = _coord_key(poi.lat, poi.lng)
            if key not in seen_coord_keys:
                seen_coord_keys.add(key)
                extra_pois.append(
                    RawPoi(
                        title=poi.name,
                        page_id=_overpass_to_synthetic_id(poi.osm_type, poi.osm_id),
                        lat=poi.lat,
                        lng=poi.lng,
                        distance_m=0.0,
                    )
                )

    return wikipedia_pois + extra_pois


# ---------------------------------------------------------------------------
# Per-source fetch helpers
# ---------------------------------------------------------------------------


async def _fetch_wikidata_pois(
    client: httpx.AsyncClient,
    sample_points: list[tuple[float, float]],
    radius_km: float,
    corridor: Polygon,
    semaphore: asyncio.Semaphore,
) -> list[WikidataPoi]:
    async def _one(point: tuple[float, float]) -> list[WikidataPoi]:
        lat, lng = point
        async with semaphore:
            try:
                return await wikidata_geosearch(client, lat, lng, radius_km=radius_km)
            except WikidataClientError as exc:
                logger.warning("Wikidata: skipping (%s, %s): %s", lat, lng, exc)
                return []

    results_per_point: list[list[WikidataPoi]] = await asyncio.gather(
        *[_one(p) for p in sample_points]
    )
    seen_ids: set[str] = set()
    out: list[WikidataPoi] = []
    for results in results_per_point:
        for poi in results:
            if not point_in_corridor(corridor, poi.lat, poi.lng):
                continue
            if poi.entity_id in seen_ids:
                continue
            seen_ids.add(poi.entity_id)
            out.append(poi)
    return out


async def _fetch_geonames_pois(
    client: httpx.AsyncClient,
    sample_points: list[tuple[float, float]],
    radius_km: float,
    corridor: Polygon,
    semaphore: asyncio.Semaphore,
    username: str,
) -> list[GeoNamesPoi]:
    async def _one(point: tuple[float, float]) -> list[GeoNamesPoi]:
        lat, lng = point
        async with semaphore:
            try:
                return await geonames_geosearch(
                    client, lat, lng, username=username, radius_km=radius_km
                )
            except GeoNamesClientError as exc:
                logger.warning("GeoNames: skipping (%s, %s): %s", lat, lng, exc)
                return []

    results_per_point: list[list[GeoNamesPoi]] = await asyncio.gather(
        *[_one(p) for p in sample_points]
    )
    seen_ids: set[int] = set()
    out: list[GeoNamesPoi] = []
    for results in results_per_point:
        for poi in results:
            if not point_in_corridor(corridor, poi.lat, poi.lng):
                continue
            if poi.geonames_id in seen_ids:
                continue
            seen_ids.add(poi.geonames_id)
            out.append(poi)
    return out


async def _fetch_overpass_pois(
    client: httpx.AsyncClient,
    sample_points: list[tuple[float, float]],
    radius_m: int,
    corridor: Polygon,
    semaphore: asyncio.Semaphore,
) -> list[OverpassPoi]:
    async def _one(point: tuple[float, float]) -> list[OverpassPoi]:
        lat, lng = point
        async with semaphore:
            try:
                return await overpass_geosearch(client, lat, lng, radius_m=radius_m)
            except OverpassClientError as exc:
                logger.warning("Overpass: skipping (%s, %s): %s", lat, lng, exc)
                return []

    results_per_point: list[list[OverpassPoi]] = await asyncio.gather(
        *[_one(p) for p in sample_points]
    )
    seen_ids: set[tuple[str, int]] = set()
    out: list[OverpassPoi] = []
    for results in results_per_point:
        for poi in results:
            if not point_in_corridor(corridor, poi.lat, poi.lng):
                continue
            key = (poi.osm_type, poi.osm_id)
            if key in seen_ids:
                continue
            seen_ids.add(key)
            out.append(poi)
    return out


# ---------------------------------------------------------------------------
# Synthetic ID helpers -- negative to avoid collisions with Wikipedia page_ids
# ---------------------------------------------------------------------------


def _wikidata_entity_to_synthetic_id(entity_id: str) -> int:
    """Convert "Q12345" to a stable negative int in range -(1e9 + N)."""
    try:
        q_number = int(entity_id.lstrip("Qq"))
        return -(1_000_000_000 + q_number)
    except ValueError:
        return -(abs(hash(entity_id)) % 2_000_000_000 + 1)


def _geonames_to_synthetic_id(geonames_id: int) -> int:
    """Convert a GeoNames ID to a stable negative int in range -(2e9 + N)."""
    return -(2_000_000_000 + geonames_id)


def _overpass_to_synthetic_id(osm_type: str, osm_id: int) -> int:
    """Convert an OSM (type, id) pair to a stable negative int.

    Uses range -(3e9 + N) for nodes, -(4e9 + N) for ways, -(5e9 + N) for
    relations -- stays well clear of Wikidata/GeoNames ranges.
    """
    base = {"node": 3, "way": 4, "relation": 5}.get(osm_type, 6)
    return -(base * 1_000_000_000 + osm_id)


# ---------------------------------------------------------------------------
# Wikipedia dedup/filter (primary source)
# ---------------------------------------------------------------------------


def _dedupe_and_filter(results_per_point: list[list[RawPoi]], corridor: Polygon) -> list[RawPoi]:
    """Deduplicate Wikipedia results by page_id and filter to corridor.

    When the same page_id appears at multiple sample points, keeps the entry
    with the smallest distance_m.
    """
    best_by_page_id: dict[int, RawPoi] = {}
    for results in results_per_point:
        for poi in results:
            if not point_in_corridor(corridor, poi.lat, poi.lng):
                continue
            existing = best_by_page_id.get(poi.page_id)
            if existing is None or poi.distance_m < existing.distance_m:
                best_by_page_id[poi.page_id] = poi
    return list(best_by_page_id.values())
