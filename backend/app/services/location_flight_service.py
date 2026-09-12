from __future__ import annotations

import logging
import math
from datetime import UTC, datetime

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.clients.aviationstack import AviationStackClientError, get_flights_by_airport
from app.models.airport import Airport
from app.services.airport_repository import _STATIC_AIRPORTS

logger = logging.getLogger("aloft.services.location_flight")


def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Calculate the great circle distance between two points on Earth.

    Args:
        lat1, lng1: Coordinates of first point (in degrees)
        lat2, lng2: Coordinates of second point (in degrees)

    Returns:
        Distance in kilometers
    """
    # Convert to radians
    lat1, lng1, lat2, lng2 = map(math.radians, [lat1, lng1, lat2, lng2])

    # Haversine formula
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    c = 2 * math.asin(math.sqrt(a))

    # Earth's radius in kilometers
    r = 6371.0
    return r * c


async def get_nearby_airports(
    db: AsyncIOMotorDatabase,
    lat: float,
    lng: float,
    radius_km: float = 100.0,
    limit: int = 20,
) -> list[dict]:
    """Get airports within a specified radius of a location.

    Args:
        db: Database instance
        lat: User's latitude
        lng: User's longitude
        radius_km: Search radius in kilometers
        limit: Maximum number of airports to return

    Returns:
        List of airport dictionaries with distance information
    """
    nearby_airports = []

    # Check static airports first (fastest)
    for iata_code, (airport_lat, airport_lng) in _STATIC_AIRPORTS.items():
        distance = haversine_distance(lat, lng, airport_lat, airport_lng)
        if distance <= radius_km:
            nearby_airports.append(
                {
                    "iata_code": iata_code,
                    "name": iata_code,  # Will be updated if we have full data
                    "lat": airport_lat,
                    "lng": airport_lng,
                    "distance_km": round(distance, 2),
                    "source": "static",
                }
            )

    # Check cached airports from database
    cursor = db.airports.find({})
    async for doc in cursor:
        doc.pop("_id", None)
        airport = Airport(**doc)
        distance = haversine_distance(lat, lng, airport.lat, airport.lng)
        if distance <= radius_km:
            # Update if we already have it from static, otherwise add new
            existing = next(
                (a for a in nearby_airports if a["iata_code"] == airport.iata_code), None
            )
            if existing:
                existing["name"] = airport.name
                existing["source"] = "cached"
            else:
                nearby_airports.append(
                    {
                        "iata_code": airport.iata_code,
                        "name": airport.name,
                        "lat": airport.lat,
                        "lng": airport.lng,
                        "distance_km": round(distance, 2),
                        "source": "cached",
                    }
                )

    # Sort by distance and limit results
    nearby_airports.sort(key=lambda x: x["distance_km"])
    return nearby_airports[:limit]


async def get_recommended_flights(
    db: AsyncIOMotorDatabase,
    client: httpx.AsyncClient,
    lat: float,
    lng: float,
    radius_km: float = 100.0,
    limit: int = 10,
) -> dict:
    """Get recommended flights based on user's location.

    Args:
        db: Database instance
        client: HTTP client
        lat: User's latitude
        lng: User's longitude
        radius_km: Search radius for nearby airports
        limit: Maximum number of flights to return per airport

    Returns:
        Dictionary with nearby airports and their departing flights
    """
    # Get nearby airports
    nearby_airports = await get_nearby_airports(db, lat, lng, radius_km, limit=5)

    if not nearby_airports:
        logger.info("No nearby airports found for location (%s, %s)", lat, lng)
        return {
            "user_location": {"lat": lat, "lng": lng},
            "nearby_airports": [],
            "total_flights": 0,
        }

    logger.info(
        "Found %d nearby airports: %s",
        len(nearby_airports),
        [a["iata_code"] for a in nearby_airports],
    )

    # Get flights for each nearby airport
    recommendations = []
    total_flights = 0

    for airport in nearby_airports:
        try:
            logger.info("Fetching flights for airport %s", airport["iata_code"])
            flights = await get_flights_by_airport(client, airport["iata_code"], limit)

            # Filter for active/scheduled flights only
            active_flights = [
                f
                for f in flights
                if f.flight_status in ["active", "scheduled", "en_route", "landed"]
            ]

            # Log status distribution
            status_counts = {}
            for f in flights:
                status_counts[f.flight_status] = status_counts.get(f.flight_status, 0) + 1
            logger.info(
                "Airport %s: %d total flights, status distribution: %s",
                airport["iata_code"],
                len(flights),
                status_counts,
            )
            logger.info(
                "Airport %s: %d active flights after filtering",
                airport["iata_code"],
                len(active_flights),
            )

            if active_flights:
                airport["flights"] = [
                    {
                        "flight_iata": f.flight_iata,
                        "arrival_iata": f.arrival_iata,
                        "flight_status": f.flight_status,
                        "departure_scheduled": f.departure_scheduled,
                        "arrival_scheduled": f.arrival_scheduled,
                        "airline_name": f.airline_name,
                    }
                    for f in active_flights
                ]
                airport["flight_count"] = len(active_flights)
                total_flights += len(active_flights)
                recommendations.append(airport)
        except AviationStackClientError as exc:
            logger.warning("Failed to get flights for airport %s: %s", airport["iata_code"], exc)
            airport["flights"] = []
            airport["flight_count"] = 0
            airport["error"] = str(exc)
            recommendations.append(airport)

    return {
        "user_location": {"lat": lat, "lng": lng},
        "search_radius_km": radius_km,
        "nearby_airports": recommendations,
        "total_flights": total_flights,
        "generated_at": datetime.now(UTC).isoformat(),
    }


async def get_flights_by_city(
    db: AsyncIOMotorDatabase,
    client: httpx.AsyncClient,
    city_name: str,
    limit: int = 10,
) -> dict:
    """Get flights departing from a specific city (using nearest airport).

    Args:
        db: Database instance
        client: HTTP client
        city_name: Name of the city to search for
        limit: Maximum number of flights to return

    Returns:
        Dictionary with city airport information and departing flights
    """
    # This is a simplified version - in production you'd want a proper city-to-airport mapping
    # For now, we'll search cached airports that contain the city name
    matching_airports = []

    cursor = db.airports.find({})
    async for doc in cursor:
        doc.pop("_id", None)
        airport = Airport(**doc)
        if city_name.lower() in airport.name.lower():
            matching_airports.append(airport)

    # Also check static airports
    for iata_code, (lat, lng) in _STATIC_AIRPORTS.items():
        if city_name.lower() in iata_code.lower():
            matching_airports.append(
                Airport(
                    iata_code=iata_code,
                    name=iata_code,
                    lat=lat,
                    lng=lng,
                )
            )

    if not matching_airports:
        return {
            "city": city_name,
            "airports_found": 0,
            "flights": [],
        }

    # Use the first matching airport (could be improved to show all)
    airport = matching_airports[0]

    try:
        flights = await get_flights_by_airport(client, airport.iata_code, limit)

        return {
            "city": city_name,
            "airport": {
                "iata_code": airport.iata_code,
                "name": airport.name,
                "lat": airport.lat,
                "lng": airport.lng,
            },
            "flights": [
                {
                    "flight_iata": f.flight_iata,
                    "arrival_iata": f.arrival_iata,
                    "flight_status": f.flight_status,
                    "departure_scheduled": f.departure_scheduled,
                    "arrival_scheduled": f.arrival_scheduled,
                    "airline_name": f.airline_name,
                }
                for f in flights
            ],
            "total_flights": len(flights),
        }
    except AviationStackClientError as exc:
        logger.warning("Failed to get flights for city %s: %s", city_name, exc)
        return {
            "city": city_name,
            "airport": {
                "iata_code": airport.iata_code,
                "name": airport.name,
            },
            "flights": [],
            "error": str(exc),
        }
