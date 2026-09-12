"""
Routes for discovering POIs along a flight corridor.

Two ways to describe the route -- user chooses whichever they have:

  1. Lat/lng directly:
       {"departure": {"lat": 8.98, "lng": 38.79},
        "arrival":   {"lat": 25.25, "lng": 55.36}}

  2. IATA airport codes:
       {"departure_iata": "ADD", "arrival_iata": "DXB"}

  3. Flight number (separate endpoint, uses AviationStack):
       POST /flights/{flight_iata}/pois

IATA lookup order for option 2:
  a) bundled static dataset (~80 major airports, no API call)
  b) MongoDB cache (populated by prior AviationStack lookups)
  If neither has the code, a 422 is returned -- use lat/lng directly
  or look it up via the flights endpoint first.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field, model_validator

from app.clients.wikipedia import WikipediaClientError, get_images
from app.core.dependencies import (
    get_current_user,
    get_database,
    get_http_client,
    poi_discovery_rate_limit,
    require_permission,
)
from app.models.role import Permission
from app.models.user import User
from app.services.airport_repository import get_cached_airport, lookup_static_airport
from app.services.poi_curator import curate_pois
from app.services.poi_repository import save_poi_images, save_pois
from app.services.poi_service import find_pois_along_corridor
from app.services.route_bundle_repository import save_route_bundle
from app.utils.pagination import (
    PaginatedResponse,
    PaginationParams,
    build_sort_query,
    calculate_pagination,
    get_skip_limit,
)

router = APIRouter(prefix="/v1/routes", tags=["discovery"])


class Coordinates(BaseModel):
    lat: float
    lng: float


class DiscoverPoisRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "By IATA codes",
                    "value": {"departure_iata": "ADD", "arrival_iata": "DXB", "width_km": 20},
                },
                {
                    "summary": "By coordinates",
                    "value": {
                        "departure": {"lat": 8.98, "lng": 38.79},
                        "arrival": {"lat": 25.25, "lng": 55.36},
                        "width_km": 20,
                    },
                },
            ]
        }
    }

    # Option A: lat/lng directly
    departure: Coordinates | None = None
    arrival: Coordinates | None = None

    # Option B: IATA airport codes
    departure_iata: str | None = None
    arrival_iata: str | None = None

    width_km: float = Field(
        default=20.0, ge=0.1, le=500.0, description="Corridor width in km (0.1–500)"
    )

    @model_validator(mode="after")
    def check_inputs(self) -> DiscoverPoisRequest:
        has_coords = self.departure is not None and self.arrival is not None
        has_iata = self.departure_iata is not None and self.arrival_iata is not None
        if not has_coords and not has_iata:
            raise ValueError(
                "Provide either departure/arrival coordinates "
                "or departure_iata/arrival_iata airport codes."
            )
        if has_coords and has_iata:
            raise ValueError("Provide coordinates or IATA codes, not both.")
        return self


class DiscoverPoisResponse(BaseModel):
    route_key: str
    departure: tuple[float, float]
    arrival: tuple[float, float]
    pois_found: int
    pois_newly_inserted: int
    images_fetched: int = 0
    poi_source_ids: list[str] = []


@router.post(
    "/pois",
    response_model=DiscoverPoisResponse,
    summary="Discover POIs along a flight route",
    dependencies=[
        Depends(poi_discovery_rate_limit()),
        Depends(require_permission(Permission.CREATE_ROUTE)),
    ],
)
async def discover_pois(
    body: DiscoverPoisRequest,
    auto_images: bool = Query(False, description="Auto-fetch Wikipedia images for discovered POIs"),
    _: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> DiscoverPoisResponse:
    """Discover Points of Interest along a flight corridor.

    Supply either **lat/lng coordinates** directly, or **IATA airport codes**.

    IATA codes are resolved against a bundled static dataset (~80 major
    airports) and the local MongoDB cache — no AviationStack request is
    made here. If an IATA code isn't found in either, a 422 is returned;
    in that case use lat/lng directly or look it up first via
    `POST /flights/{flight_iata}/pois` to populate the cache.

    The response includes a `route_key` you pass to subsequent calls:
    - `POST /v1/pois/{source_id}/images` — fetch photos for a POI
    - `POST /v1/pois/{source_id}/story` — generate narration text for a POI
    - `POST /v1/pois/{source_id}/audio` — synthesize audio for a POI
    - `POST /v1/pois/{source_id}/audio/mixed` — audio with music bed for a POI
    - `POST /v1/sessions` — start a live session

    **Auto-images mode:** pass `?auto_images=true` to
    automatically fetch Wikipedia images for every discovered POI.
    Without the flag, only POIs are discovered.
    """
    if body.departure_iata is not None:
        # IATA path -- resolve codes to coords
        departure = await _resolve_iata(db, body.departure_iata)
        arrival = await _resolve_iata(db, body.arrival_iata)  # type: ignore[arg-type]
    else:
        departure = (body.departure.lat, body.departure.lng)  # type: ignore[union-attr]
        arrival = (body.arrival.lat, body.arrival.lng)  # type: ignore[union-attr]

    try:
        pois = await find_pois_along_corridor(
            client, departure=departure, arrival=arrival, width_km=body.width_km
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    inserted, poi_source_ids = await save_pois(db, pois)

    # Curate POIs to keep only the best ones (quality over quantity)
    curated_pois = curate_pois(pois, departure, arrival)
    curated_source_ids = [f"wikipedia:{p.page_id}" for p in curated_pois]

    bundle = await save_route_bundle(db, departure, arrival, curated_source_ids)

    images_fetched = 0
    if auto_images and curated_source_ids:
        wikipedia_pois = [
            (sid, name)
            for sid in curated_source_ids
            if sid.startswith("wikipedia:")
            for name in [
                next((p.title for p in curated_pois if f"wikipedia:{p.page_id}" == sid), "")
            ]
            if name
        ]

        if wikipedia_pois:
            # Limit concurrent Wikipedia requests to avoid 429s.
            # Wikipedia is generous but will rate-limit burst traffic;
            # 5 concurrent is safe for any route length.
            _image_semaphore = asyncio.Semaphore(5)

            async def _fetch_one_image(sid: str, name: str) -> int:
                async with _image_semaphore:
                    try:
                        images = await get_images(client, name, max_images=4)
                        if images:
                            await save_poi_images(db, sid, [img.url for img in images])
                            return len(images)
                    except WikipediaClientError:
                        pass
                    return 0

            results = await asyncio.gather(
                *[_fetch_one_image(sid, name) for sid, name in wikipedia_pois],
                return_exceptions=True,
            )
            images_fetched = sum(r for r in results if isinstance(r, int))

    return DiscoverPoisResponse(
        route_key=bundle.route_key,
        departure=departure,
        arrival=arrival,
        pois_found=len(pois),
        pois_newly_inserted=inserted,
        images_fetched=images_fetched,
        poi_source_ids=curated_source_ids,
    )


async def _resolve_iata(db: AsyncIOMotorDatabase, iata_code: str) -> tuple[float, float]:
    """Resolve an IATA code to (lat, lng).

    Tries the static dataset first (no I/O), then the DB cache.
    Raises HTTP 422 if neither has the code.
    """
    iata_upper = iata_code.upper()

    coords = lookup_static_airport(iata_upper)
    if coords is not None:
        return coords

    cached = await get_cached_airport(db, iata_upper)
    if cached is not None:
        return (cached.lat, cached.lng)

    raise HTTPException(
        status_code=422,
        detail=(
            f"Airport '{iata_upper}' not found in the static dataset or local cache. "
            f"Use lat/lng coordinates directly, or discover it first via "
            f"POST /flights/{{flight_iata}}/pois to populate the cache."
        ),
    )


class PoiListItem(BaseModel):
    """Simplified POI model for list views."""

    source_id: str
    name: str
    location: dict[str, Any]
    source: str
    updated_at: str


@router.get(
    "/list",
    response_model=PaginatedResponse[PoiListItem],
    summary="List POIs with pagination",
    description=(
        "Returns a paginated list of POIs. "
        "Implements secure offset-based pagination with configurable page size. "
        "Prevents over-fetching and improves performance for large datasets."
    ),
)
async def list_pois(
    params: PaginationParams = Depends(),
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
) -> PaginatedResponse[PoiListItem]:
    """List POIs with pagination."""

    # Calculate skip and limit
    skip, limit = get_skip_limit(params.page, params.page_size)

    # Build sort query
    sort_query = build_sort_query(params.sort_by, params.sort_order)

    # Get total count
    total = await db.pois.count_documents({})

    # Query with pagination
    cursor = db.pois.find({}, skip=skip, limit=limit, sort=sort_query)
    pois = await cursor.to_list(length=limit)

    # Convert to response model
    items = []
    for poi in pois:
        updated_at = poi.get("updated_at")
        # Handle both datetime objects and strings
        if updated_at:
            updated_at_str = updated_at if isinstance(updated_at, str) else updated_at.isoformat()
        else:
            updated_at_str = ""
        items.append(
            PoiListItem(
                source_id=poi.get("source_id", ""),
                name=poi.get("name", ""),
                location=poi.get("location", {}),
                source=poi.get("source", ""),
                updated_at=updated_at_str,
            )
        )

    # Calculate pagination metadata
    pagination_meta = calculate_pagination(total, params.page, params.page_size)

    return PaginatedResponse[PoiListItem](items=items, **pagination_meta)
