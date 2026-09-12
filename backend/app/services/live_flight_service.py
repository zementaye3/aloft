from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.clients.aviationstack import (
    AviationStackClientError,
    FlightInfo,
    FlightNotFoundError,
    get_flight,
)
from app.clients.opensky import (
    AircraftNotFoundError,
    OpenSkyClientError,
    get_aircraft_position,
)
from app.core.config import get_settings
from app.models.poi import Poi
from app.services.corridor import distance_km
from app.services.flight_resolution import resolve_flight_route
from app.services.flight_session_repository import create_session
from app.services.poi_curator import curate_pois
from app.services.poi_repository import get_pois_by_source_ids, save_pois
from app.services.poi_service import find_pois_along_corridor
from app.services.position_tracking_service import (
    DEFAULT_TRIGGER_RADIUS_KM,
)
from app.services.route_bundle_repository import save_route_bundle
from app.services.story_repository import get_stories_batch, save_story
from app.services.story_service import (
    InsufficientFactsError,
    generate_story,
    supported_languages,
)

logger = logging.getLogger("aloft.services.live_flight")


class NarrationEntry(BaseModel):
    source_id: str
    name: str
    distance_km: float
    in_range: bool
    story: str | None = None
    story_available: bool = False
    generation_status: str = "ready"


class LiveFlightResponse(BaseModel):
    flight_iata: str
    flight_status: str
    callsign: str | None
    route_key: str
    session_id: str
    departure: tuple[float, float]
    arrival: tuple[float, float]
    pois_found: int
    pois_newly_inserted: int
    position_source: str = "unavailable"
    aircraft: dict[str, Any] | None = None
    nearby_narrations: list[dict[str, Any]] = []
    all_route_pois: list[dict[str, Any]] = []


class LiveFlightError(Exception):
    pass


async def prepare_live_flight_tracking(
    client: httpx.AsyncClient,
    db: AsyncIOMotorDatabase,
    redis: Any,
    flight_iata: str,
    width_km: float = 20.0,
    language: str = "en",
    trigger_radius_km: float = DEFAULT_TRIGGER_RADIUS_KM,
    max_upcoming_pois: int = 20,
    generate_missing_stories: bool = True,
) -> LiveFlightResponse:
    if language not in supported_languages():
        raise ValueError(
            f"Unsupported language '{language}'. Supported: {', '.join(supported_languages())}"
        )

    flight_info = await _resolve_flight(client, flight_iata)
    departure, arrival = await resolve_flight_route(client, db, flight_info.flight_iata)

    corridor_pois = await find_pois_along_corridor(
        client, departure=departure, arrival=arrival, width_km=width_km
    )
    inserted, poi_source_ids = await save_pois(db, corridor_pois)

    # Curate POIs to keep only the best ones (quality over quantity)
    curated_pois = curate_pois(corridor_pois, departure, arrival)
    curated_source_ids = [f"wikipedia:{p.page_id}" for p in curated_pois]

    bundle = await save_route_bundle(db, departure, arrival, curated_source_ids)
    session = await create_session(redis, bundle.route_key)

    aircraft = None
    position_source = "unavailable"

    try:
        aircraft = await _fetch_live_position(client, flight_info)
        position_source = "opensky"
    except (AircraftNotFoundError, OpenSkyClientError) as exc:
        logger.info("Live position unavailable for %s: %s", flight_info.flight_iata, exc)

    route_pois = await get_pois_by_source_ids(db, bundle.poi_source_ids)
    cached_stories = await get_stories_batch(db, poi_source_ids, language)
    story_map = {s.poi_source_id: s.text_content for s in cached_stories}

    missing_source_ids = [poi.source_id for poi in route_pois if poi.source_id not in story_map]

    if missing_source_ids and generate_missing_stories:
        max_concurrent = get_settings().content_generation_max_concurrent
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _generate_one(poi: Poi) -> str | None:
            async with semaphore:
                try:
                    story = await generate_story(client, poi.source_id, poi.name, language=language)
                    await save_story(db, story)
                    return story.text_content
                except InsufficientFactsError as exc:
                    logger.warning("Skipping story for %s: %s", poi.source_id, exc)
                    return None
                except Exception as exc:
                    logger.warning("Story generation failed for %s: %s", poi.source_id, exc)
                    return None

        results = await asyncio.gather(
            *[_generate_one(poi) for poi in route_pois if poi.source_id in missing_source_ids],
            return_exceptions=True,
        )
        for poi, result in zip(
            [p for p in route_pois if p.source_id in missing_source_ids], results, strict=False
        ):
            if isinstance(result, Exception):
                continue
            if result is not None:
                story_map[poi.source_id] = result

    all_poi_details = _build_all_poi_details(
        route_pois=route_pois,
        story_map=story_map,
        current_lat=aircraft.latitude if aircraft else departure[0],
        current_lng=aircraft.longitude if aircraft else departure[1],
        trigger_radius_km=trigger_radius_km,
    )

    nearby = [p for p in all_poi_details if p["in_range"]]
    upcoming = [p for p in all_poi_details if not p["in_range"] and p["distance_km"] is not None][
        :max_upcoming_pois
    ]

    target_pois = upcoming if generate_missing_stories else []
    missing_in_target = [
        poi.source_id
        for poi in route_pois
        if poi.source_id in {p["source_id"] for p in target_pois} and poi.source_id not in story_map
    ]

    if missing_in_target and generate_missing_stories:
        max_concurrent = get_settings().content_generation_max_concurrent
        semaphore = asyncio.Semaphore(max_concurrent)

        async def _generate_one(poi: Poi) -> str | None:
            async with semaphore:
                try:
                    story = await generate_story(client, poi.source_id, poi.name, language=language)
                    await save_story(db, story)
                    return story.text_content
                except InsufficientFactsError as exc:
                    logger.info("Skipping story for %s: %s", poi.source_id, exc)
                    return None
                except Exception as exc:
                    logger.warning("Story generation failed for %s: %s", poi.source_id, exc)
                    return None

        results = await asyncio.gather(
            *[_generate_one(poi) for poi in route_pois if poi.source_id in missing_in_target],
            return_exceptions=True,
        )
        generated_ids = set()
        for poi, result in zip(
            [p for p in route_pois if p.source_id in missing_in_target], results, strict=False
        ):
            if isinstance(result, Exception):
                continue
            if result is not None:
                story_map[poi.source_id] = result
                generated_ids.add(poi.source_id)

        all_poi_details = _build_all_poi_details(
            route_pois=route_pois,
            story_map=story_map,
            current_lat=aircraft.latitude if aircraft else departure[0],
            current_lng=aircraft.longitude if aircraft else departure[1],
            trigger_radius_km=trigger_radius_km,
            generated_ids=generated_ids,
        )
        nearby = [p for p in all_poi_details if p["in_range"]]
        upcoming = [
            p for p in all_poi_details if not p["in_range"] and p["distance_km"] is not None
        ][:max_upcoming_pois]

    aircraft_payload = None
    if aircraft is not None:
        aircraft_payload = aircraft.model_dump(mode="json")
        for k in (
            "latitude",
            "longitude",
            "baro_altitude_m",
            "velocity_ms",
            "true_track_deg",
            "vertical_rate_ms",
            "on_ground",
        ):
            if aircraft_payload.get(k) is None:
                aircraft_payload.pop(k, None)

    return LiveFlightResponse(
        flight_iata=flight_info.flight_iata,
        flight_status=flight_info.flight_status,
        callsign=aircraft.callsign if aircraft else flight_info.callsign,
        route_key=bundle.route_key,
        session_id=session.session_id,
        departure=departure,
        arrival=arrival,
        pois_found=len(corridor_pois),
        pois_newly_inserted=inserted,
        position_source=position_source,
        aircraft=aircraft_payload,
        nearby_narrations=nearby,
        all_route_pois=upcoming,
    )


async def _resolve_flight(client: httpx.AsyncClient, flight_iata: str) -> FlightInfo:
    try:
        return await get_flight(client, flight_iata)
    except FlightNotFoundError as exc:
        raise LiveFlightError(str(exc)) from exc
    except AviationStackClientError as exc:
        raise LiveFlightError(str(exc)) from exc


async def _fetch_live_position(client: httpx.AsyncClient, flight_info: FlightInfo):
    return await get_aircraft_position(
        client,
        callsign=flight_info.callsign,
    )


def _build_all_poi_details(
    route_pois: list[Poi],
    story_map: dict[str, str],
    current_lat: float,
    current_lng: float,
    trigger_radius_km: float,
    generated_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for poi in route_pois:
        poi_lng, poi_lat = poi.location["coordinates"]
        dist = distance_km(current_lat, current_lng, poi_lat, poi_lng)
        in_range = dist <= trigger_radius_km
        text = story_map.get(poi.source_id)
        if generated_ids and poi.source_id in generated_ids or text is not None:
            status = "ready"
        elif in_range:
            status = "pending"
        else:
            status = "ready"
        details.append(
            {
                "source_id": poi.source_id,
                "name": poi.name,
                "distance_km": round(dist, 2),
                "in_range": in_range,
                "story": text,
                "story_available": text is not None,
                "generation_status": status,
            }
        )
    details.sort(key=lambda p: p["distance_km"])
    return details
