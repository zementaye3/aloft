from __future__ import annotations

import logging

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.models.airport import Airport
from app.services.airport_repository import get_cached_airport, save_airport

logger = logging.getLogger("aloft.services.flight_resolution")


async def resolve_flight_route(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    flight_iata: str,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Resolve a flight number to (departure_coords, arrival_coords).

    Tries AeroDataBox first (1 API call, returns coords directly).
    Falls back to AviationStack if AeroDataBox is not configured or fails
    (3 API calls: flight lookup + 2 airport lookups, with DB caching).

    Returns:
        ((dep_lat, dep_lng), (arr_lat, arr_lng))
    """
    settings = get_settings()

    # --- Primary: AeroDataBox (1 call, coords included) ---
    if settings.aerodatabox_api_key:
        try:
            from app.clients.aerodatabox import get_flight as aerodatabox_get_flight

            info = await aerodatabox_get_flight(client, flight_iata)

            # Cache both airports so future lookups hit the DB, not the API
            for iata, lat, lng in [
                (info.departure_iata, info.departure_lat, info.departure_lng),
                (info.arrival_iata, info.arrival_lat, info.arrival_lng),
            ]:
                if await get_cached_airport(db, iata) is None:
                    await save_airport(
                        db,
                        Airport(iata_code=iata, name=iata, lat=lat, lng=lng),
                    )

            return (info.departure_lat, info.departure_lng), (info.arrival_lat, info.arrival_lng)

        except Exception as exc:
            logger.warning(
                "AeroDataBox failed for %s: %s -- falling back to AviationStack",
                flight_iata,
                exc,
            )

    # --- Fallback: AviationStack (flight lookup + 2 airport lookups) ---
    from app.clients.aviationstack import get_flight

    flight = await get_flight(client, flight_iata)
    departure = await _resolve_airport_coords(client, db, flight.departure_iata)
    arrival = await _resolve_airport_coords(client, db, flight.arrival_iata)
    return departure, arrival


async def _resolve_airport_coords(
    client: httpx.AsyncClient, db: AsyncIOMotorDatabase, iata_code: str
) -> tuple[float, float]:
    cached = await get_cached_airport(db, iata_code)
    if cached is not None:
        return (cached.lat, cached.lng)

    from app.clients.aviationstack import get_airport

    fetched = await get_airport(client, iata_code)
    await save_airport(
        db,
        Airport(iata_code=fetched.iata_code, name=fetched.name, lat=fetched.lat, lng=fetched.lng),
    )
    return (fetched.lat, fetched.lng)
