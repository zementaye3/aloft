from __future__ import annotations

import logging as _logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from redis.asyncio import Redis

from app.clients.aviationstack import AviationStackClientError, FlightNotFoundError
from app.core.dependencies import (
    flight_lookup_rate_limit,
    get_current_user,
    get_database,
    get_http_client,
    get_redis,
    live_tracking_rate_limit,
    require_permission,
)
from app.models.role import Permission
from app.models.user import User
from app.services.airport_repository import lookup_static_airport
from app.services.flight_resolution import resolve_flight_route
from app.services.live_flight_service import LiveFlightError, prepare_live_flight_tracking
from app.services.poi_curator import curate_pois
from app.services.poi_repository import save_pois
from app.services.poi_service import find_pois_along_corridor
from app.services.route_bundle_repository import save_route_bundle

router = APIRouter(prefix="/v1/flights", tags=["discovery", "live tracking"])


class DiscoverFlightRequest(BaseModel):
    flightNumber: str | None = None
    departureCode: str | None = None
    arrivalCode: str | None = None
    date: str
    corridorWidth: float = 100.0


class DiscoverFlightResponse(BaseModel):
    routeKey: str
    poisCount: int
    pois: list[dict]


_discover_logger = _logging.getLogger("aloft.routers.flights.discover")


@router.post(
    "/discover",
    response_model=DiscoverFlightResponse,
    summary="Discover POIs for a flight (flexible input)",
    dependencies=[
        Depends(flight_lookup_rate_limit()),
        Depends(require_permission(Permission.LOOKUP_FLIGHT)),
    ],
)
async def discover_flight(
    request: DiscoverFlightRequest,
    _: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> DiscoverFlightResponse:
    """Discover POIs for a flight using either flight number or airport codes.

    Accepts:
    - Flight number alone  (e.g. "ET601", "FLYER60", "BA 456")
    - Airport codes alone  (e.g. ADD → JED)
    - Flight number + airport codes — airport codes used as fallback if the
      flight number can't be resolved via AviationStack.

    Resolution order for coordinates:
      1. AviationStack live lookup (if flight number provided)
      2. Static airport table (built-in, ~200 airports)
      3. MongoDB airports collection (previously cached AviationStack responses)
      4. AviationStack airport lookup (burns one API request per airport)
    """
    dep_code = (request.departureCode or "").strip().upper() or None
    arr_code = (request.arrivalCode or "").strip().upper() or None
    flight_num = (request.flightNumber or "").strip().upper().replace(" ", "") or None

    if not flight_num and not (dep_code and arr_code):
        raise HTTPException(
            status_code=400,
            detail="Provide a flight number, or both a departure and arrival airport code.",
        )

    departure: tuple[float, float] | None = None
    arrival: tuple[float, float] | None = None

    # ── 1. Try resolving via flight number ──────────────────────────────────
    if flight_num:
        try:
            departure, arrival = await resolve_flight_route(client, db, flight_num)
            _discover_logger.info("Resolved %s → %s / %s", flight_num, departure, arrival)
        except FlightNotFoundError:
            _discover_logger.info(
                "Flight %s not found in AviationStack, trying airport code fallback", flight_num
            )
        except AviationStackClientError as exc:
            _discover_logger.warning("AviationStack error for %s: %s", flight_num, exc)

    # ── 2. Fall back to airport codes ───────────────────────────────────────
    if (departure is None or arrival is None) and dep_code and arr_code:
        _discover_logger.info("Resolving coordinates for %s → %s", dep_code, arr_code)

        async def _get_coords(iata: str) -> tuple[float, float] | None:
            # a) static table
            coords = lookup_static_airport(iata)
            if coords:
                return coords
            # b) MongoDB cache
            from app.services.airport_repository import get_cached_airport

            cached = await get_cached_airport(db, iata)
            if cached:
                return (cached.lat, cached.lng)
            # c) live AviationStack airport lookup
            try:
                from app.clients.aviationstack import get_airport

                info = await get_airport(client, iata)
                from app.models.airport import Airport
                from app.services.airport_repository import save_airport

                await save_airport(
                    db,
                    Airport(iata_code=info.iata_code, name=info.name, lat=info.lat, lng=info.lng),
                )
                return (info.lat, info.lng)
            except AviationStackClientError:
                return None

        dep_coords = await _get_coords(dep_code)
        arr_coords = await _get_coords(arr_code)

        if dep_coords is None:
            raise HTTPException(
                status_code=404,
                detail=f"Airport '{dep_code}' not found. Check the IATA code and try again.",
            )
        if arr_coords is None:
            raise HTTPException(
                status_code=404,
                detail=f"Airport '{arr_code}' not found. Check the IATA code and try again.",
            )

        departure = dep_coords
        arrival = arr_coords

    if departure is None or arrival is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Could not resolve route for '{flight_num}'. "
                "Try entering the departure and arrival airport codes instead."
            ),
        )

    # ── 3. Discover POIs ────────────────────────────────────────────────────
    try:
        pois = await find_pois_along_corridor(
            client, departure, arrival, width_km=request.corridorWidth
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inserted, _ = await save_pois(db, pois)
    curated_pois = curate_pois(pois, departure, arrival)
    curated_source_ids = [f"wikipedia:{p.page_id}" for p in curated_pois]
    bundle = await save_route_bundle(db, departure, arrival, curated_source_ids)

    _discover_logger.info(
        "Discovery complete: %d POIs found, %d curated, route_key=%s",
        len(pois),
        len(curated_pois),
        bundle.route_key,
    )

    formatted_pois = [
        {
            "id": f"wikipedia:{p.page_id}",
            "name": p.title,
            "description": "",
            "lat": p.lat,
            "lng": p.lng,
            "country": "",
            "distanceFromPath": p.distance_m / 1000,  # Convert meters to km
            "hasStory": False,
            "hasAudio": False,
        }
        for p in curated_pois
    ]

    return DiscoverFlightResponse(
        routeKey=bundle.route_key,
        poisCount=len(curated_pois),
        pois=formatted_pois,
    )


class DiscoverPoisByFlightResponse(BaseModel):
    flight_iata: str
    route_key: str
    departure: tuple[float, float]
    arrival: tuple[float, float]
    pois_found: int
    pois_newly_inserted: int


@router.post(
    "/{flight_iata}/pois",
    response_model=DiscoverPoisByFlightResponse,
    summary="Discover POIs for a flight number",
    dependencies=[
        Depends(flight_lookup_rate_limit()),
        Depends(require_permission(Permission.LOOKUP_FLIGHT)),
    ],
)
async def discover_pois_for_flight(
    flight_iata: str,
    width_km: float = Query(
        default=20.0, ge=0.1, le=500.0, description="Corridor width in km (0.1–500)"
    ),
    _: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> DiscoverPoisByFlightResponse:
    """Look up a flight's route and discover POIs along the corridor.

    Resolves the flight's departure and arrival airports using AeroDataBox
    (primary) or AviationStack (fallback), then runs the same POI discovery
    as `POST /routes/pois`.

    **Rate-limited** (10 requests/hour per IP by default). Free tier quota:
    - AeroDataBox: 500 req/month via RapidAPI (primary, 1 API call per flight)
    - AviationStack: 500 req/month (fallback, 3 API calls per flight)

    Example: `POST /flights/ET308/pois`
    """
    try:
        departure, arrival = await resolve_flight_route(client, db, flight_iata)
    except FlightNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AviationStackClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    try:
        pois = await find_pois_along_corridor(client, departure, arrival, width_km=width_km)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inserted, poi_source_ids = await save_pois(db, pois)

    # Curate POIs to keep only the best ones (quality over quantity)
    curated_pois = curate_pois(pois, departure, arrival)
    curated_source_ids = [f"wikipedia:{p.page_id}" for p in curated_pois]

    bundle = await save_route_bundle(db, departure, arrival, curated_source_ids)

    return DiscoverPoisByFlightResponse(
        flight_iata=flight_iata,
        route_key=bundle.route_key,
        departure=departure,
        arrival=arrival,
        pois_found=len(pois),
        pois_newly_inserted=inserted,
    )


@router.post(
    "/{flight_iata}/live",
    summary="Live flight tracking with real-time stories",
    description=(
        "One-shot endpoint for tracking a currently airborne flight. "
        "Resolves the route, discovers POIs, starts a live session, "
        "fetches the aircraft's live ADS-B position from OpenSky (by callsign), "
        "and returns all nearby stories sorted by distance. "
        "If the aircraft isn't in ADS-B coverage, the route and POIs "
        "are still returned with position_source='unavailable'."
    ),
    dependencies=[
        Depends(live_tracking_rate_limit()),
        Depends(require_permission(Permission.LOOKUP_FLIGHT)),
    ],
)
async def live_track_flight(
    flight_iata: str,
    language: str = Query("en", description="Story language (e.g. en, ar, fr, de)"),
    width_km: float = Query(default=20.0, ge=0.1, le=500.0, description="Corridor width in km"),
    trigger_radius_km: float = Query(
        default=50.0, ge=0.1, le=500.0, description="Narration trigger radius in km"
    ),
    max_upcoming: int = Query(default=20, ge=0, le=200, description="Max upcoming POIs to return"),
    generate_missing_stories: bool = Query(
        default=True, description="Auto-generate stories for POIs without cached stories"
    ),
    _: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis = Depends(get_redis),
) -> dict:
    """Full live-flight experience in a single API call.

    1. Resolves departure/arrival for the flight via AviationStack.
    2. Discovers POIs along the corridor (caches result for reuse).
    3. Creates a live session and returns a `session_id` you can use with
       `POST /v1/sessions/{session_id}/position` for subsequent polling.
    4. Looks up the aircraft's live ADS-B position from OpenSky using its
       callsign. If the aircraft is on the ground or out of coverage,
       the route bundle is still returned with `position_source: "unavailable"`.
    5. Returns all POIs within `trigger_radius_km` of the aircraft with
       cached stories, plus the next `max_upcoming` POIs sorted by distance.
    6. If `generate_missing_stories=true`, generates narration stories
       for any POI without a cached story using the AI story service.

    **Examples**

    - `POST /flights/BA178/live` — full tracking for British Airways BA178
    - `POST /flights/BA178/live?language=ar&trigger_radius_km=30` — Arabic stories within 30 km
    - `POST /flights/LH400/live?width_km=50&max_upcoming=50` — wide corridor with many upcoming POIs
    """
    try:
        result = await prepare_live_flight_tracking(
            client=client,
            db=db,
            redis=redis,
            flight_iata=flight_iata,
            width_km=width_km,
            language=language,
            trigger_radius_km=trigger_radius_km,
            max_upcoming_pois=max_upcoming,
            generate_missing_stories=generate_missing_stories,
        )
    except LiveFlightError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return result.model_dump(mode="json")
