"""
OpenStreetMap Overpass API client -- POI discovery via OSM tags.

Queries the Overpass API for named nodes/ways/relations within a bounding
circle. OSM covers physical infrastructure that Wikidata/GeoNames miss:
airports, railway stations, harbours, tourist attractions explicitly tagged
as such, and almost every named building in urban areas.

No API key required. Rate limits are generous but require a descriptive
User-Agent and are enforced per-IP. Use the public instance:
  https://overpass-api.de/api/interpreter

Rate limits: ~10,000 queries/day per IP, no per-minute hard cap, but
requests that take >180s are killed. Keep radius and result count small.

Overpass QL docs: https://wiki.openstreetmap.org/wiki/Overpass_API/Overpass_QL
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import BaseModel

logger = logging.getLogger("aloft.clients.overpass")

_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_MAX_RADIUS_M = 50_000  # 50 km -- consistent with Wikidata/Wikipedia
_DEFAULT_RADIUS_M = 10_000  # 10 km default
_DEFAULT_LIMIT = 50
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 2.0  # Overpass is slower to recover than REST APIs

# OSM tags that mark something worth narrating about from an aircraft.
# Each tuple is (key, value). A node/way/relation matching ANY of these
# is included. We deliberately exclude generic tags like amenity=restaurant
# that are fine for navigation but not interesting at 35,000 feet.
_POI_TAG_FILTERS: list[tuple[str, str]] = [
    ("tourism", "attraction"),
    ("tourism", "museum"),
    ("tourism", "viewpoint"),
    ("tourism", "artwork"),
    ("historic", "castle"),
    ("historic", "ruins"),
    ("historic", "monument"),
    ("historic", "memorial"),
    ("historic", "archaeological_site"),
    ("historic", "battlefield"),
    ("natural", "peak"),
    ("natural", "volcano"),
    ("natural", "bay"),
    ("natural", "cape"),
    ("natural", "strait"),
    ("natural", "cliff"),
    ("waterway", "waterfall"),
    ("aeroway", "aerodrome"),
    ("railway", "station"),
    ("harbour", "*"),
    ("place", "city"),
    ("place", "town"),
    ("place", "village"),
    ("place", "island"),
    ("place", "archipelago"),
]


class OverpassPoi(BaseModel):
    """A named POI from OpenStreetMap."""

    osm_type: str  # "node", "way", or "relation"
    osm_id: int
    name: str
    lat: float
    lng: float
    tags: dict[str, str]  # all OSM tags on this element


class OverpassClientError(Exception):
    """Raised when the Overpass API request fails."""


def _build_overpass_query(lat: float, lng: float, radius_m: int, limit: int) -> str:
    """Build an Overpass QL query that finds named POIs within radius_m of (lat, lng)."""
    # Build filter clauses: one union branch per tag filter.
    # We use `nwr` (node/way/relation) for each tag so we catch all element types.
    tag_clauses = []
    for key, value in _POI_TAG_FILTERS:
        if value == "*":
            tag_clauses.append(f'nwr["{key}"](around:{radius_m},{lat},{lng});')
        else:
            tag_clauses.append(f'nwr["{key}"="{value}"](around:{radius_m},{lat},{lng});')

    union_body = "\n  ".join(tag_clauses)

    return f"""
[out:json][timeout:25][maxsize:16777216];
(
  {union_body}
);
// Only keep elements with a name tag -- unnamed POIs aren't narrate-able.
// Use ._ (input set) without repeating the around constraint -- it is
// already baked into every branch of the union above, so re-specifying
// (around:...) here would be redundant and could confuse some Overpass
// interpreter versions into double-counting results.
node._[name];
way._[name];
relation._[name];
out center {limit};
""".strip()


async def geosearch(
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
    radius_m: int = _DEFAULT_RADIUS_M,
    limit: int = _DEFAULT_LIMIT,
) -> list[OverpassPoi]:
    """Find named POIs within radius_m of (lat, lng) via the Overpass API.

    Returns an empty list on a successful query with no results.
    Raises OverpassClientError on network failure or API error after retries.

    Args:
        client: shared httpx AsyncClient.
        lat, lng: WGS-84 coordinates of the search centre.
        radius_m: search radius in metres (max 50,000).
        limit: maximum number of results to return per query.
    """
    radius_m = min(radius_m, _MAX_RADIUS_M)
    query = _build_overpass_query(lat, lng, radius_m, limit)
    headers = {
        "User-Agent": "AloftFlightNarrationApp/0.1 (https://github.com/JoshuaMinase/Aloft)",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.post(
                _OVERPASS_URL,
                data={"data": query},
                headers=headers,
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=10.0, pool=5.0),
            )
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning("Overpass network error attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc)
        else:
            if response.status_code == 200:
                return _parse_response(response, lat, lng)
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = OverpassClientError(f"Overpass returned HTTP {response.status_code}")
                logger.warning(
                    "Overpass retryable error %d, attempt %d/%d",
                    response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )
            else:
                raise OverpassClientError(
                    f"Overpass non-retryable HTTP {response.status_code}: {response.text[:200]}"
                )

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise OverpassClientError(
        f"Overpass geosearch failed after {_MAX_ATTEMPTS} attempts"
    ) from last_error


def _parse_response(
    response: httpx.Response, query_lat: float, query_lng: float
) -> list[OverpassPoi]:
    """Parse Overpass JSON response into OverpassPoi objects."""
    try:
        data = response.json()
    except Exception as exc:
        raise OverpassClientError(f"Overpass response is not valid JSON: {exc}") from exc

    elements = data.get("elements", [])
    pois: list[OverpassPoi] = []
    seen_ids: set[tuple[str, int]] = set()  # (type, id) pairs

    for elem in elements:
        try:
            osm_type = elem.get("type", "")
            osm_id = int(elem["id"])
            tags: dict[str, str] = elem.get("tags", {})

            name = tags.get("name", "").strip()
            if not name:
                continue  # skip unnamed elements -- not narrate-able

            key = (osm_type, osm_id)
            if key in seen_ids:
                continue
            seen_ids.add(key)

            # Nodes have lat/lon directly; ways/relations have a `center` object
            if osm_type == "node":
                lat = float(elem["lat"])
                lng = float(elem["lon"])
            else:
                center = elem.get("center", {})
                lat = float(center.get("lat", 0))
                lng = float(center.get("lon", 0))
                if lat == 0 and lng == 0:
                    # `out center` should always provide a center, but skip if missing
                    continue

            pois.append(
                OverpassPoi(
                    osm_type=osm_type,
                    osm_id=osm_id,
                    name=name,
                    lat=lat,
                    lng=lng,
                    tags=tags,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug(
                "Overpass: skipping malformed element near (%s, %s): %s",
                query_lat,
                query_lng,
                exc,
            )

    logger.debug("Overpass geosearch near (%s, %s): %d results", query_lat, query_lng, len(pois))
    return pois
