from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.dependencies import (
    flight_lookup_rate_limit,
    get_current_user,
    get_database,
    get_http_client,
    require_permission,
)
from app.models.role import Permission
from app.models.user import User
from app.services.location_flight_service import (
    get_flights_by_city,
    get_nearby_airports,
    get_recommended_flights,
)

logger = logging.getLogger("aloft.routers.location_flights")

router = APIRouter(prefix="/v1/flights/location", tags=["location", "recommendations"])


class NearbyAirportsResponse(BaseModel):
    user_location: dict[str, float]
    search_radius_km: float
    nearby_airports: list[dict]
    total_airports: int


class RecommendedFlightsResponse(BaseModel):
    user_location: dict[str, float]
    search_radius_km: float
    nearby_airports: list[dict]
    total_flights: int
    generated_at: str


class CityFlightsResponse(BaseModel):
    city: str
    airport: dict | None
    flights: list[dict]
    total_flights: int
    error: str | None = None


@router.get(
    "/airports/nearby",
    response_model=NearbyAirportsResponse,
    summary="Get nearby airports based on location",
    description="Find airports within a specified radius of the user's location. Returns airports sorted by distance.",
)
async def get_nearby_airports_endpoint(
    lat: float = Query(..., ge=-90, le=90, description="User's latitude"),
    lng: float = Query(..., ge=-180, le=180, description="User's longitude"),
    radius_km: float = Query(
        default=100.0, ge=10, le=500, description="Search radius in kilometers"
    ),
    limit: int = Query(default=20, ge=1, le=50, description="Maximum number of airports to return"),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
) -> NearbyAirportsResponse:
    """Get airports near the user's location.

    Uses the Haversine formula to calculate distances and returns airports
    within the specified radius. Results are sorted by distance (closest first).

    **Example:**
    `GET /v1/flights/location/airports/nearby?lat=9.14&lng=38.76&radius_km=100`

    This would find airports within 100km of Addis Ababa, Ethiopia.
    """
    try:
        airports = await get_nearby_airports(db, lat, lng, radius_km, limit)
        return NearbyAirportsResponse(
            user_location={"lat": lat, "lng": lng},
            search_radius_km=radius_km,
            nearby_airports=airports,
            total_airports=len(airports),
        )
    except Exception as exc:
        logger.error("Error getting nearby airports: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to get nearby airports") from exc


@router.get(
    "/recommendations",
    response_model=RecommendedFlightsResponse,
    summary="Get flight recommendations based on location",
    description="Get recommended flights departing from airports near the user's location. Returns nearby airports with their departing flights.",
    dependencies=[
        Depends(flight_lookup_rate_limit()),
        Depends(require_permission(Permission.LOOKUP_FLIGHT)),
    ],
)
async def get_flight_recommendations(
    lat: float = Query(..., ge=-90, le=90, description="User's latitude"),
    lng: float = Query(..., ge=-180, le=180, description="User's longitude"),
    radius_km: float = Query(
        default=100.0, ge=10, le=500, description="Search radius for nearby airports in kilometers"
    ),
    limit: int = Query(default=10, ge=1, le=20, description="Maximum flights per airport"),
    db: AsyncIOMotorDatabase = Depends(get_database),
    client: httpx.AsyncClient = Depends(get_http_client),
    current_user: User = Depends(get_current_user),
) -> RecommendedFlightsResponse:
    """Get flight recommendations based on user's location.

    Finds nearby airports and queries AviationStack for departing flights.
    Returns flights sorted by airport proximity and departure time.

    **Example:**
    `GET /v1/flights/location/recommendations?lat=9.14&lng=38.76&radius_km=150`

    This would find flights departing from airports within 150km of Addis Ababa.

    **Rate-limited** (10 requests/hour per IP by default) to protect AviationStack quota.
    """
    try:
        recommendations = await get_recommended_flights(db, client, lat, lng, radius_km, limit)
        return RecommendedFlightsResponse(**recommendations)
    except Exception as exc:
        logger.error("Error getting flight recommendations: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to get flight recommendations") from exc


@router.get(
    "/city/{city_name}",
    response_model=CityFlightsResponse,
    summary="Get flights departing from a specific city",
    description="Get flights departing from airports in or near a specific city. Useful when users know their city but not their exact coordinates.",
)
async def get_flights_by_city_endpoint(
    city_name: str,
    limit: int = Query(default=10, ge=1, le=20, description="Maximum number of flights to return"),
    db: AsyncIOMotorDatabase = Depends(get_database),
    client: httpx.AsyncClient = Depends(get_http_client),
    current_user: User = Depends(get_current_user),
) -> CityFlightsResponse:
    """Get flights departing from a specific city.

    Searches for airports matching the city name and returns departing flights.
    This is useful when users know their city but don't have GPS coordinates.

    **Example:**
    `GET /v1/flights/location/city/Addis%20Ababa`

    This would find flights departing from Addis Ababa airports.
    """
    try:
        flights = await get_flights_by_city(db, client, city_name, limit)
        return CityFlightsResponse(**flights)
    except Exception as exc:
        logger.error("Error getting flights by city: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to get flights by city") from exc
