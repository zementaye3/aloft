"""
Wikidata client -- POI discovery via the Wikidata Query Service (SPARQL).

Finds places that have Wikidata coordinate data (wdt:P625) within a radius
of a point. Returns places Wikipedia geosearch misses: villages, minor
landmarks, historic sites that have a Wikidata entry but no Wikipedia article.

API:
  - Endpoint: https://query.wikidata.org/sparql
  - No authentication required (public SPARQL endpoint).
  - Rate limit: ~5 requests/second per IP; be conservative.
  - Policy: descriptive User-Agent header required.
    See: https://meta.wikimedia.org/wiki/User-Agent_policy
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from pydantic import BaseModel

from app.core.config import get_settings

logger = logging.getLogger("aloft.clients.wikidata")

_SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
_MAX_RADIUS_KM = 50.0
_DEFAULT_LIMIT = 50
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

# Types to include: city, human settlement, archaeological site, museum,
# castle, church, mosque, temple, monument, natural feature, island, lake,
# mountain, volcano, national park, historic district.
# Wikidata Q-IDs for common POI types.
_POI_TYPE_IDS = (
    "wd:Q515",  # city
    "wd:Q486972",  # human settlement
    "wd:Q839954",  # archaeological site
    "wd:Q33506",  # museum
    "wd:Q23413",  # castle
    "wd:Q16970",  # church
    "wd:Q32815",  # mosque
    "wd:Q44613",  # monastery
    "wd:Q4989906",  # monument
    "wd:Q46169",  # palace
    "wd:Q2065736",  # cultural property
    "wd:Q23442",  # island
    "wd:Q23397",  # lake
    "wd:Q8502",  # mountain
    "wd:Q8072",  # volcano
    "wd:Q179049",  # national park
    "wd:Q570116",  # tourist attraction
)

# Wikidata WKT literal pattern: "Point(lng lat)"
_WKT_PATTERN = re.compile(r"Point\(([+-]?\d+\.?\d*)\s+([+-]?\d+\.?\d*)\)", re.IGNORECASE)


class WikidataPoi(BaseModel):
    """A POI from Wikidata, enriched with structured type info."""

    entity_id: str  # Wikidata entity ID, e.g. "Q60"
    title: str  # English label
    lat: float
    lng: float
    types: list[str]  # English labels of instance-of types
    description: str = ""


class WikidataClientError(Exception):
    """Raised when the Wikidata query fails after retries."""


def _build_sparql_query(lat: float, lng: float, radius_km: float, limit: int) -> str:
    type_filter = ", ".join(_POI_TYPE_IDS)
    return f"""
SELECT DISTINCT ?place ?placeLabel ?location ?typeLabel ?description WHERE {{
  SERVICE wikibase:around {{
    ?place wdt:P625 ?location .
    bd:serviceParam wikibase:center "Point({lng} {lat})"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "{radius_km}" .
  }}
  ?place wdt:P31 ?type .
  FILTER(?type IN ({type_filter}))
  OPTIONAL {{ ?place schema:description ?description . FILTER(LANG(?description) = "en") }}
  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "en" .
  }}
}}
LIMIT {limit}
""".strip()


def _parse_wkt_location(wkt: str) -> tuple[float, float] | None:
    """Parse 'Point(lng lat)' WKT literal into (lat, lng). Returns None on parse failure."""
    m = _WKT_PATTERN.search(wkt)
    if not m:
        return None
    try:
        lng = float(m.group(1))
        lat = float(m.group(2))
        return lat, lng
    except ValueError:
        return None


def _extract_entity_id(uri: str) -> str:
    """Extract 'Q12345' from 'http://www.wikidata.org/entity/Q12345'."""
    return uri.rsplit("/", 1)[-1]


async def geosearch(
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
    radius_km: float = 10.0,
    limit: int = _DEFAULT_LIMIT,
) -> list[WikidataPoi]:
    """Find POIs within radius_km of (lat, lng) via the Wikidata SPARQL endpoint.

    Returns an empty list on a successful query with no results.
    Raises WikidataClientError on network failure or SPARQL error after retries.

    Args:
        client: shared httpx AsyncClient.
        lat, lng: WGS-84 coordinates of the search centre.
        radius_km: search radius in km (max 50).
        limit: maximum results per query.
    """
    radius_km = min(radius_km, _MAX_RADIUS_KM)
    query = _build_sparql_query(lat, lng, radius_km, limit)
    settings = get_settings()
    headers = {
        "Accept": "application/sparql-results+json",
        "User-Agent": (
            f"AloftFlightNarrationApp/0.1 ({settings.app_contact_email}; "
            "https://github.com/JoshuaMinase/Aloft)"
        ),
    }

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.post(
                _SPARQL_ENDPOINT,
                data={"query": query},
                headers=headers,
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
            )
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning("Wikidata network error attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc)
        else:
            if response.status_code == 200:
                return _parse_results(response, lat, lng)
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = WikidataClientError(
                    f"Wikidata SPARQL returned HTTP {response.status_code}"
                )
                logger.warning(
                    "Wikidata retryable error %d, attempt %d/%d",
                    response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )
            else:
                raise WikidataClientError(
                    f"Wikidata SPARQL non-retryable error HTTP {response.status_code}: "
                    f"{response.text[:200]}"
                )

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise WikidataClientError(
        f"Wikidata geosearch failed after {_MAX_ATTEMPTS} attempts"
    ) from last_error


def _parse_results(
    response: httpx.Response, query_lat: float, query_lng: float
) -> list[WikidataPoi]:
    """Parse SPARQL JSON results into WikidataPoi objects."""
    try:
        data = response.json()
    except Exception as exc:
        raise WikidataClientError(f"Wikidata response is not valid JSON: {exc}") from exc

    bindings = data.get("results", {}).get("bindings", [])
    pois: list[WikidataPoi] = []
    seen_entity_ids: set[str] = set()

    for row in bindings:
        try:
            entity_uri = row["place"]["value"]
            entity_id = _extract_entity_id(entity_uri)
            if entity_id in seen_entity_ids:
                continue

            label = row.get("placeLabel", {}).get("value", "")
            # Skip rows where the label is just the entity ID (no English label available)
            if not label or label == entity_id:
                continue

            wkt = row.get("location", {}).get("value", "")
            coords = _parse_wkt_location(wkt)
            if coords is None:
                logger.debug("Wikidata: skipping %s -- could not parse WKT: %r", entity_id, wkt)
                continue

            lat, lng = coords
            type_label = row.get("typeLabel", {}).get("value", "")
            description = row.get("description", {}).get("value", "")

            seen_entity_ids.add(entity_id)
            pois.append(
                WikidataPoi(
                    entity_id=entity_id,
                    title=label,
                    lat=lat,
                    lng=lng,
                    types=[type_label] if type_label else [],
                    description=description,
                )
            )
        except (KeyError, TypeError) as exc:
            logger.debug(
                "Wikidata: skipping malformed row near (%s, %s): %s", query_lat, query_lng, exc
            )

    logger.debug("Wikidata geosearch near (%s, %s): %d results", query_lat, query_lng, len(pois))
    return pois
