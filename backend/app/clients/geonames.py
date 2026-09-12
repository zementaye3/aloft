"""
GeoNames client -- POI discovery via the GeoNames Web Services API.

Finds populated places and geographic features near a coordinate.
Covers rural/regional geography that Wikipedia often ignores, and
provides place names in local scripts without needing a translation pipeline.

Setup (free):
  1. Sign up at https://www.geonames.org/login
  2. Enable free web services at https://www.geonames.org/manageaccount
  3. Set GEONAMES_USERNAME and POI_SOURCE_GEONAMES_ENABLED=true in .env

Free tier: 1,000 credits/hour, 30,000/day. findNearbyJSON costs 1 credit/call.
Terms: https://www.geonames.org/export/

Security note: The username is sent via HTTPS to secure.geonames.org, which encrypts it
in transit. While it appears in the URL path, it is encrypted and not logged by the
CDN. For extra protection in server access logs, consider using a restricted account
with a strong password.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import BaseModel

logger = logging.getLogger("aloft.clients.geonames")

_API_URL = "https://secure.geonames.org/findNearbyJSON"
_MAX_RADIUS_KM = 50.0
_DEFAULT_LIMIT = 50
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

# Feature classes to search by default.
# P = populated places, S = spots/cultural/historical, T = terrain/mountains
_DEFAULT_FEATURE_CLASSES = ["P", "S", "T"]

# GeoNames API error codes (returned inside {"status": {"value": N}})
_ERR_INVALID_CREDENTIALS = 10
_ERR_AUTHORIZATION_EXCEPTION = 19
_ERR_DAILY_LIMIT_EXCEEDED = 18
_ERR_HOURLY_LIMIT_EXCEEDED = 13  # confirmed against geonames.org/export/ws-overview.html
_ERR_NO_RESULT = 15


class GeoNamesPoi(BaseModel):
    """A POI from GeoNames."""

    geonames_id: int
    name: str
    lat: float
    lng: float
    country_code: str
    feature_class: str  # P, S, T, etc.
    feature_code: str  # PPLC, PPL, MT, etc. -- see geonames.org/export/codes.html
    distance_km: float
    population: int = 0


class GeoNamesClientError(Exception):
    """Raised when the GeoNames API request fails."""


class GeoNamesAuthError(GeoNamesClientError):
    """Raised when the GeoNames username is invalid or web services not enabled."""


async def geosearch(
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
    username: str,
    radius_km: float = 10.0,
    limit: int = _DEFAULT_LIMIT,
    feature_classes: list[str] | None = None,
) -> list[GeoNamesPoi]:
    """Find POIs within radius_km of (lat, lng) via the GeoNames findNearby API.

    Returns an empty list on a successful query with no results.
    Raises GeoNamesAuthError on an invalid username or missing web services access.
    Raises GeoNamesClientError on network failure or quota exhaustion.

    Args:
        client: shared httpx AsyncClient.
        lat, lng: WGS-84 coordinates of the search centre.
        username: GeoNames account username (required by the API).
        radius_km: search radius in km (max 50 on free tier).
        limit: max results per call.
        feature_classes: GeoNames feature classes to include.
            Defaults to P (populated), S (cultural/spots), T (terrain).
    """
    if not username:
        raise GeoNamesAuthError("GEONAMES_USERNAME must be set to use the GeoNames API.")

    radius_km = min(radius_km, _MAX_RADIUS_KM)
    classes = feature_classes or _DEFAULT_FEATURE_CLASSES

    params: list[tuple[str, str]] = [
        ("lat", str(lat)),
        ("lng", str(lng)),
        ("radius", str(radius_km)),
        ("maxRows", str(limit)),
        ("username", username),
        ("type", "json"),
    ]
    for fc in classes:
        params.append(("featureClass", fc))

    headers = {
        "User-Agent": "AloftFlightNarrationApp/0.1 (https://github.com/JoshuaMinase/Aloft)",
        "Accept": "application/json",
    }

    last_error: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            response = await client.get(
                _API_URL,
                params=params,
                headers=headers,
                timeout=httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0),
            )
        except httpx.RequestError as exc:
            last_error = exc
            logger.warning("GeoNames network error attempt %d/%d: %s", attempt, _MAX_ATTEMPTS, exc)
        else:
            if response.status_code in _RETRYABLE_STATUS_CODES:
                last_error = GeoNamesClientError(f"GeoNames returned HTTP {response.status_code}")
                logger.warning(
                    "GeoNames retryable error %d, attempt %d/%d",
                    response.status_code,
                    attempt,
                    _MAX_ATTEMPTS,
                )
            elif response.status_code != 200:
                raise GeoNamesClientError(
                    f"GeoNames non-retryable HTTP {response.status_code}: {response.text[:200]}"
                )
            else:
                return _parse_response(response, lat, lng)

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    raise GeoNamesClientError(
        f"GeoNames geosearch failed after {_MAX_ATTEMPTS} attempts"
    ) from last_error


def _parse_response(
    response: httpx.Response, query_lat: float, query_lng: float
) -> list[GeoNamesPoi]:
    """Parse GeoNames JSON response into GeoNamesPoi objects."""
    try:
        data = response.json()
    except Exception as exc:
        raise GeoNamesClientError(f"GeoNames response is not valid JSON: {exc}") from exc

    # GeoNames returns {"status": {"value": N, "message": "..."}} on error
    if "status" in data:
        code = data["status"].get("value", 0)
        message = data["status"].get("message", "unknown error")
        if code in (_ERR_INVALID_CREDENTIALS, _ERR_AUTHORIZATION_EXCEPTION):
            raise GeoNamesAuthError(
                f"GeoNames auth error (code {code}): {message}. "
                "Check your GEONAMES_USERNAME and ensure web services are enabled at "
                "geonames.org/manageaccount."
            )
        if code in (_ERR_HOURLY_LIMIT_EXCEEDED, _ERR_DAILY_LIMIT_EXCEEDED):
            raise GeoNamesClientError(
                f"GeoNames quota exhausted (code {code}): {message}. "
                "Hourly limit is 1,000 credits; daily limit is 30,000 credits."
            )
        if code == _ERR_NO_RESULT:
            return []
        raise GeoNamesClientError(f"GeoNames API error (code {code}): {message}")

    geonames = data.get("geonames", [])
    pois: list[GeoNamesPoi] = []
    seen_ids: set[int] = set()

    for entry in geonames:
        try:
            geonames_id = int(entry["geonameId"])
            if geonames_id in seen_ids:
                continue

            # lat/lng come as strings in the GeoNames API
            lat = float(entry["lat"])
            lng = float(entry["lng"])
            name = entry.get("name") or entry.get("toponymName", "")
            if not name:
                continue

            seen_ids.add(geonames_id)
            pois.append(
                GeoNamesPoi(
                    geonames_id=geonames_id,
                    name=name,
                    lat=lat,
                    lng=lng,
                    country_code=entry.get("countryCode", ""),
                    feature_class=entry.get("fcl", ""),
                    feature_code=entry.get("fcode", ""),
                    distance_km=float(entry.get("distance", 0)),
                    population=int(entry.get("population", 0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug(
                "GeoNames: skipping malformed entry near (%s, %s): %s",
                query_lat,
                query_lng,
                exc,
            )

    logger.debug("GeoNames geosearch near (%s, %s): %d results", query_lat, query_lng, len(pois))
    return pois
