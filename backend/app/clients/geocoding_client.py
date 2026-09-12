"""
Reverse geocoding: given a lat/lng, return a human-readable description
of what's there -- ocean name, country, region, etc. Used when the plane
is over an area with no nearby Wikipedia POIs.

BigDataCloud's free reverse geocoding API: no API key required,
50k requests/month free. Returns ocean names, countries, regions.
https://www.bigdatacloud.com/geocoding-api/reverse-geocoding
"""

from __future__ import annotations

import logging

import httpx
from pydantic import BaseModel

logger = logging.getLogger("aloft.clients.geocoding")

GEOCODING_API_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"


class RegionInfo(BaseModel):
    description: str  # "North Atlantic Ocean" or "County Cork, Ireland"
    is_ocean: bool  # True if over water
    country: str | None  # None if over ocean
    locality: str | None  # Most specific name available


async def reverse_geocode(
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
) -> RegionInfo:
    """Return region info for a coordinate. Never raises -- on any failure
    returns a generic fallback so the caller always has something to work with.
    """
    try:
        response = await client.get(
            GEOCODING_API_URL,
            params={"latitude": lat, "longitude": lng, "localityLanguage": "en"},
            timeout=httpx.Timeout(connect=5.0, read=8.0, write=5.0, pool=5.0),
        )
        response.raise_for_status()
        data = response.json()
        return _parse_response(data)
    except Exception as exc:
        logger.warning("Reverse geocoding failed for (%s, %s): %s", lat, lng, exc)
        return RegionInfo(description="a remote area", is_ocean=False, country=None, locality=None)


def _parse_response(data: dict) -> RegionInfo:
    country = data.get("countryName")
    locality = data.get("locality") or data.get("city") or None
    principal_subdivision = data.get("principalSubdivision") or ""

    # BigDataCloud returns empty country (empty string) for ocean coordinates
    # None country means the response is incomplete or invalid
    is_ocean = country == ""

    if is_ocean:
        # Build ocean description from available fields
        # e.g. "North Atlantic Ocean" comes back in the locality or
        # as part of the continent field for ocean coordinates
        locality_info = data.get("localityInfo", {})
        if locality_info and isinstance(locality_info, dict):
            informative = locality_info.get("informative", [])
            if informative and isinstance(informative, list) and len(informative) > 0:
                ocean_name = informative[0].get("name", locality or "the ocean")
            else:
                ocean_name = locality or "the ocean"
        else:
            ocean_name = locality or "the ocean"
        description = ocean_name
    elif locality and principal_subdivision and country:
        description = f"{locality}, {principal_subdivision}, {country}"
    elif principal_subdivision and country:
        description = f"{principal_subdivision}, {country}"
    elif country:
        description = country
    else:
        description = "a remote area"

    return RegionInfo(
        description=description,
        is_ocean=is_ocean,
        country=country,
        locality=locality,
    )
