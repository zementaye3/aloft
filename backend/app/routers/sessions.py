"""
HTTP layer for live flight sessions.

Sessions are stored in Redis (not MongoDB) -- they're short-lived
per-user state that auto-expires after 12 hours. MongoDB is still used
here for route bundle and story lookups, which are permanent shared data.

Position sources (in priority order)
──────────────────────────────────────
1. OpenSky Network (if icao24 or callsign is provided in the request)
   Fetches the live ADS-B position from OpenSky. Useful for the web
   spectator dashboard, or when the mobile client doesn't have GPS.
2. Client-provided lat/lng (always accepted as the fallback)
   The mobile app sends GPS directly. If OpenSky is also requested but
   fails (aircraft not in coverage, rate limit hit), the client lat/lng
   is used instead and a warning is logged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.clients.geocoding_client import reverse_geocode
from app.clients.opensky import AircraftNotFoundError, OpenSkyClientError, get_aircraft_position
from app.core.config import get_settings
from app.core.dependencies import (
    get_current_user,
    get_database,
    get_http_client,
    get_redis,
    position_update_rate_limit,
    require_permission,
    session_creation_rate_limit,
    spectator_view_rate_limit,
)
from app.models.flight_session import FlightSession
from app.models.role import Permission
from app.models.user import User
from app.services.audio_repository import get_audio, save_audio
from app.services.audio_service import get_voice_id_for_language, synthesize_story_audio
from app.services.corridor import distance_km
from app.services.destination_tour_service import prepare_destination_tour
from app.services.flight_session_repository import (
    create_session,
    disable_sharing,
    enable_sharing,
    get_session,
    get_session_by_share_token,
    record_destination_tour_narration,
    record_position_and_narration,
    record_region_narration,
    record_upcoming_narration,
    update_session_destination_tour,
)
from app.services.poi_repository import get_pois_by_source_ids
from app.services.position_tracking_service import (
    DEFAULT_TRIGGER_RADIUS_KM,
    find_next_poi_to_narrate,
    find_next_upcoming_poi,
)
from app.services.region_narration_service import (
    REGION_NARRATION_COOLDOWN_MINUTES,
    generate_region_narration,
)
from app.services.route_bundle_repository import get_route_bundle
from app.services.story_repository import get_stories_batch, get_story
from app.services.story_service import InsufficientFactsError, generate_upcoming_story

router = APIRouter(prefix="/v1/sessions", tags=["live session"])
logger = logging.getLogger("aloft.routers.sessions")

# Pre-fetch radius for audio generation (before the 8km play trigger)
PRE_FETCH_RADIUS_KM = 50.0


async def _prefetch_audio(
    db: AsyncIOMotorDatabase,
    client: httpx.AsyncClient,
    source_id: str,
    language: str,
) -> None:
    """Background task to pre-fetch audio for a POI."""
    try:
        story = await get_story(db, source_id, language)
        if story:
            voice_id = get_voice_id_for_language(language)
            audio_bytes = await synthesize_story_audio(
                story.text_content, language=language, http_client=client
            )
            await save_audio(db, source_id, language, voice_id, audio_bytes)
    except Exception:
        pass  # fail silently, text still returned


class StartSessionRequest(BaseModel):
    route_key: str
    language: str = "en"

    model_config = {
        "json_schema_extra": {"examples": [{"route_key": "add-dxb-abc123", "language": "en"}]}
    }


class StartSessionResponse(BaseModel):
    session_id: str
    route_key: str
    destination_preview_ready: bool = False
    arrival_city: str | None = None
    arrival_country: str | None = None


class PositionUpdateRequest(BaseModel):
    # Client-provided GPS coordinates (always accepted; used as fallback
    # when OpenSky lookup is requested but fails).
    lat: float = Field(..., ge=-90.0, le=90.0, description="Latitude in WGS-84 degrees")
    lng: float = Field(..., ge=-180.0, le=180.0, description="Longitude in WGS-84 degrees")
    language: str = "en"
    trigger_radius_km: float = Field(default=DEFAULT_TRIGGER_RADIUS_KM, ge=0.1, le=500.0)

    # Optional: request OpenSky live position instead of (or to override)
    # the client's GPS. Provide one of icao24 or callsign, not both.
    # If OpenSky lookup fails, the client lat/lng above is used as fallback.
    icao24: str | None = None
    callsign: str | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "Mobile app (client GPS)",
                    "value": {
                        "lat": 15.5,
                        "lng": 42.3,
                        "language": "en",
                        "trigger_radius_km": 50,
                    },
                },
                {
                    "summary": "Spectator dashboard (OpenSky)",
                    "value": {
                        "lat": 0.0,
                        "lng": 0.0,
                        "icao24": "4b1806",
                        "language": "en",
                        "trigger_radius_km": 50,
                    },
                },
            ]
        }
    }


class NarrationTrigger(BaseModel):
    source_id: str | None = None  # None for region/destination_tour narrations
    name: str
    text_content: str | None = None
    narration_type: str  # "poi" | "upcoming" | "destination_tour" | "region"


class PositionUpdateResponse(BaseModel):
    triggered: bool
    narration: NarrationTrigger | None = None
    # Reflects the position actually used (OpenSky or client-provided)
    lat_used: float
    lng_used: float
    position_source: str = "client"  # "client" | "opensky"


class ShareSessionResponse(BaseModel):
    share_token: str
    share_path: str


class SpectatorNarrationItem(BaseModel):
    source_id: str | None = None  # None for destination_tour narrations
    name: str
    text_content: str | None = None
    narration_type: str  # "poi" | "destination_tour"


class SpectatorSessionView(BaseModel):
    route_key: str
    arrival_city: str | None = None
    arrival_country: str | None = None
    last_position: tuple[float, float] | None = None
    last_updated_at: datetime
    narrations: list[SpectatorNarrationItem]


def _check_session_ownership(session: FlightSession, current_user: User) -> None:
    """Reject cross-user access to a session's share controls.

    owner_id="" is the legacy default for sessions created before this
    check existed; those are left open to avoid breaking existing clients
    (same convention as update_position's ownership check).
    """
    if session.owner_id and session.owner_id != current_user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to manage this session.",
        )


@router.post(
    "",
    response_model=StartSessionResponse,
    summary="Start a live flight session",
    dependencies=[
        Depends(session_creation_rate_limit()),
        Depends(require_permission(Permission.CREATE_SESSION)),
    ],
)
async def start_session(
    body: StartSessionRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis = Depends(get_redis),
) -> StartSessionResponse:
    """Start a new live flight session for a previously discovered route.

    Returns a `session_id` that you send with each GPS position update.

    Sessions are stored in Redis with a **12-hour TTL** — auto-expires after
    the flight lands, never accumulates in MongoDB.

    The destination tour narrations are generated in the background — the
    endpoint returns immediately and `destination_preview_ready` will be True
    only once the background task has stored them in the session. Check this
    flag on subsequent position-update responses.

    404 if `route_key` doesn't match a prior discovery call.
    """
    bundle = await get_route_bundle(db, body.route_key)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=f"No route found for route_key '{body.route_key}'. Discover it first.",
        )

    # Resolve arrival country/city from arrival coordinates
    arrival_lat, arrival_lng = bundle.arrival
    region = await reverse_geocode(client, arrival_lat, arrival_lng)
    arrival_country = region.country or "your destination"
    arrival_city = region.locality or arrival_country

    # Create the session immediately so the client can start sending positions
    # right away.  Destination tour narrations are prepared in a background
    # task (up to ~20 Groq calls) and written back into the session when done.
    session = await create_session(
        redis,
        body.route_key,
        owner_id=current_user.user_id,
        language=body.language,
        arrival_country=arrival_country,
        arrival_city=arrival_city,
        destination_tour_narrations=[],
    )

    async def _prepare_tour_background() -> None:
        """Run prepare_destination_tour and patch the session when done."""
        try:
            tour_narrations = await prepare_destination_tour(
                client,
                db,
                arrival_iata="",
                arrival_country=arrival_country,
                arrival_city=arrival_city,
                language=body.language,
            )
            if tour_narrations:
                await update_session_destination_tour(redis, session.session_id, tour_narrations)
                logger.info(
                    "Destination tour ready for session %s (%d narrations)",
                    session.session_id,
                    len(tour_narrations),
                )
        except Exception as exc:
            logger.warning(
                "Background destination tour preparation failed for session %s: %s",
                session.session_id,
                exc,
            )

    background_tasks.add_task(_prepare_tour_background)

    return StartSessionResponse(
        session_id=session.session_id,
        route_key=session.route_key,
        destination_preview_ready=False,  # Will be ready once background task completes
        arrival_city=arrival_city,
        arrival_country=arrival_country,
    )


@router.post(
    "/{session_id}/share",
    response_model=ShareSessionResponse,
    summary="Enable spectator sharing for a session",
    dependencies=[Depends(require_permission(Permission.UPDATE_SESSION))],
)
async def share_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> ShareSessionResponse:
    """Generate a public, read-only spectator link for this session.

    Anyone with the resulting token can watch live position and every
    narration that's fired so far via `GET /shared/{token}` -- no account
    or login required. Idempotent: calling this again while sharing is
    already on returns the same token rather than rotating it, so an
    already-shared link keeps working.

    404 if the session doesn't exist. 403 if you don't own it.
    """
    session = await get_session(redis, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No session found for '{session_id}'")
    _check_session_ownership(session, current_user)

    token = await enable_sharing(redis, session_id)
    return ShareSessionResponse(share_token=token, share_path=f"/v1/sessions/shared/{token}")


@router.delete(
    "/{session_id}/share",
    status_code=204,
    summary="Revoke spectator sharing for a session",
    dependencies=[Depends(require_permission(Permission.UPDATE_SESSION))],
)
async def unshare_session(
    session_id: str,
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> Response:
    """Revoke this session's share link. The old token stops working immediately.

    404 if the session doesn't exist. 403 if you don't own it.
    """
    session = await get_session(redis, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No session found for '{session_id}'")
    _check_session_ownership(session, current_user)

    await disable_sharing(redis, session_id)
    return Response(status_code=204)


@router.get(
    "/shared/{token}",
    response_model=SpectatorSessionView,
    summary="Public spectator view of a shared session",
    dependencies=[Depends(spectator_view_rate_limit())],
)
async def view_shared_session(
    token: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis = Depends(get_redis),
) -> SpectatorSessionView:
    """Fully public, unauthenticated view of a shared flight session.

    Returns the live position plus every POI story and destination-tour
    highlight narrated so far -- read straight from the same cache the
    traveler's own app is using, so watching a flight costs nothing extra
    in AI generation.

    404 if the token is invalid, revoked, or the session has expired.
    """
    session = await get_session_by_share_token(redis, token)
    if session is None:
        raise HTTPException(status_code=404, detail="This share link is invalid or has expired.")

    narrations: list[SpectatorNarrationItem] = []

    if session.narrated_poi_source_ids:
        pois = await get_pois_by_source_ids(db, session.narrated_poi_source_ids)
        poi_names = {poi.source_id: poi.name for poi in pois}
        stories = await get_stories_batch(db, session.narrated_poi_source_ids, session.language)
        story_text = {story.poi_source_id: story.text_content for story in stories}
        for source_id in session.narrated_poi_source_ids:
            narrations.append(
                SpectatorNarrationItem(
                    source_id=source_id,
                    name=poi_names.get(source_id, source_id),
                    text_content=story_text.get(source_id),
                    narration_type="poi",
                )
            )

    fired_tour_narrations = session.destination_tour_narrations[: session.destination_tour_index]
    for narration_text in fired_tour_narrations:
        narrations.append(
            SpectatorNarrationItem(
                source_id=None,
                name=f"About {session.arrival_city}" if session.arrival_city else "Destination",
                text_content=narration_text,
                narration_type="destination_tour",
            )
        )

    return SpectatorSessionView(
        route_key=session.route_key,
        arrival_city=session.arrival_city,
        arrival_country=session.arrival_country,
        last_position=session.last_position,
        last_updated_at=session.last_updated_at,
        narrations=narrations,
    )


@router.post(
    "/{session_id}/position",
    response_model=PositionUpdateResponse,
    summary="Send GPS position and get narration trigger",
    dependencies=[
        Depends(position_update_rate_limit()),
        Depends(require_permission(Permission.UPDATE_SESSION)),
    ],
)
async def update_position(
    session_id: str,
    body: PositionUpdateRequest,
    current_user: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis = Depends(get_redis),
) -> PositionUpdateResponse:
    """Send the current GPS position and receive a narration trigger if a POI is in range.

    Call this from the mobile app on every GPS fix (every few seconds).

    **Position sources (in priority order):**
    1. **OpenSky Network** — if `icao24` or `callsign` is provided, the live
       ADS-B position is fetched from OpenSky. Useful for the web spectator
       dashboard where the passenger's phone GPS isn't available.
    2. **Client GPS** — the `lat` / `lng` in the request body, used when
       OpenSky is not requested or when OpenSky lookup fails (not in coverage,
       rate limit hit). `position_source` in the response reflects which was used.

    **Four-tier narration logic:**
    1. **POI directly below** — when the aircraft enters `trigger_radius_km` of an
       un-narrated POI, returns `triggered: true` with `narration_type: "poi"`.
    2. **POI coming up ahead** — if no POI is nearby but one is within 300km ahead,
       returns a teaser with `narration_type: "upcoming"`.
    3. **Destination tour** — if no POIs are nearby or upcoming, and enough
       time has passed since the last destination tour narration (8-minute cooldown),
       returns a pre-generated destination highlight with `narration_type: "destination_tour"`.
    4. **Region/ocean context** — if no POIs are nearby or upcoming and the destination
       tour is exhausted or on cooldown, and enough time has passed since the last region
       narration (45-minute cooldown), returns contextual narration with `narration_type: "region"`.

    Each POI triggers **at most once** per session. Region and destination tour narrations are
    rate-limited to avoid repetition during long ocean crossings.
    """
    session = await get_session(redis, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail=f"No session found for '{session_id}'")

    # Enforce session ownership — reject positions submitted by a different user.
    # owner_id="" is the legacy default for sessions created before this check was
    # introduced; those are allowed through to avoid breaking existing clients.
    if session.owner_id and session.owner_id != current_user.user_id:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to update this session.",
        )

    bundle = await get_route_bundle(db, session.route_key)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session's route '{session.route_key}' no longer exists",
        )

    # ---------------------------------------------------------------------------
    # Resolve position: OpenSky first, client GPS as fallback
    # ---------------------------------------------------------------------------
    lat, lng = body.lat, body.lng
    position_source = "client"

    if body.icao24 or body.callsign:
        try:
            pos = await get_aircraft_position(
                client,
                icao24=body.icao24,
                callsign=body.callsign,
            )
            lat, lng = pos.latitude, pos.longitude
            position_source = "opensky"
            logger.debug("OpenSky position used for session %s: %.4f, %.4f", session_id, lat, lng)
        except AircraftNotFoundError as exc:
            logger.warning(
                "OpenSky: aircraft not found for session %s, falling back to client GPS: %s",
                session_id,
                exc,
            )
        except OpenSkyClientError as exc:
            logger.warning(
                "OpenSky request failed for session %s, falling back to client GPS: %s",
                session_id,
                exc,
            )

    # ---------------------------------------------------------------------------
    # Three-tier narration logic
    # ---------------------------------------------------------------------------
    route_pois = await get_pois_by_source_ids(db, bundle.poi_source_ids)
    already_narrated = set(session.narrated_poi_source_ids)
    already_upcoming = set(session.upcoming_poi_triggered_source_ids)

    # --- Tier 1: POI directly below (existing behavior) ---
    next_poi = find_next_poi_to_narrate(
        lat, lng, route_pois, already_narrated, body.trigger_radius_km
    )
    if next_poi is not None:
        # Lazy audio generation: if POI within trigger radius but no audio yet — generate it now
        settings = get_settings()
        voice_id = get_voice_id_for_language(body.language)
        existing_audio = await get_audio(db, next_poi.source_id, body.language, voice_id)
        if existing_audio is None or not Path(existing_audio.file_path).exists():
            try:
                story = await get_story(db, next_poi.source_id, body.language)
                if story:
                    audio_bytes = await synthesize_story_audio(
                        story.text_content, language=body.language, http_client=client
                    )
                    await save_audio(db, next_poi.source_id, body.language, voice_id, audio_bytes)
            except Exception:
                pass  # fail silently, text still returned

        story = await get_story(db, next_poi.source_id, body.language)
        await record_position_and_narration(redis, session_id, lat, lng, next_poi.source_id)
        return PositionUpdateResponse(
            triggered=True,
            narration=NarrationTrigger(
                source_id=next_poi.source_id,
                name=next_poi.name,
                text_content=story.text_content if story else None,
                narration_type="poi",
            ),
            lat_used=lat,
            lng_used=lng,
            position_source=position_source,
        )

    # --- Pre-fetch audio for POIs within 50km so it's ready when needed ---
    pois_to_prefetch = [
        p
        for p in route_pois
        if p.source_id not in already_narrated
        and distance_km(
            body.lat, body.lng, p.location["coordinates"][1], p.location["coordinates"][0]
        )
        <= PRE_FETCH_RADIUS_KM
    ]
    for prefetch_poi in pois_to_prefetch[:3]:  # max 3 at once
        settings = get_settings()
        voice_id = get_voice_id_for_language(body.language)
        existing = await get_audio(db, prefetch_poi.source_id, body.language, voice_id)
        if existing is None:
            asyncio.create_task(_prefetch_audio(db, client, prefetch_poi.source_id, body.language))

    # --- Tier 2: POI coming up ahead ---
    upcoming_result = find_next_upcoming_poi(
        lat, lng, route_pois, already_narrated | already_upcoming
    )
    if upcoming_result is not None:
        upcoming_poi, dist = upcoming_result
        try:
            upcoming_story = await generate_upcoming_story(
                client, upcoming_poi.source_id, upcoming_poi.name, dist, body.language
            )
            await record_upcoming_narration(redis, session_id, upcoming_poi.source_id)
            await record_position_and_narration(redis, session_id, lat, lng, None)
            return PositionUpdateResponse(
                triggered=True,
                narration=NarrationTrigger(
                    source_id=upcoming_poi.source_id,
                    name=upcoming_poi.name,
                    text_content=upcoming_story.text_content,
                    narration_type="upcoming",
                ),
                lat_used=lat,
                lng_used=lng,
                position_source=position_source,
            )
        except (httpx.HTTPError, InsufficientFactsError) as exc:
            logger.warning(
                f"Upcoming story generation failed for {upcoming_poi.name}: {exc}, falling through to destination tour"
            )

    # --- Tier 3: Destination tour ---
    settings = get_settings()
    tour_cooldown_passed = session.last_destination_tour_at is None or datetime.now(
        UTC
    ) - session.last_destination_tour_at > timedelta(
        minutes=settings.destination_tour_interval_minutes
    )

    has_tour_content = session.destination_tour_narrations and session.destination_tour_index < len(
        session.destination_tour_narrations
    )

    if tour_cooldown_passed and has_tour_content:
        narration_text = session.destination_tour_narrations[session.destination_tour_index]

        # Update session state for destination tour using repository function
        next_index = session.destination_tour_index + 1
        await record_destination_tour_narration(redis, session_id, lat, lng, next_index)

        return PositionUpdateResponse(
            triggered=True,
            narration=NarrationTrigger(
                source_id=None,
                name=f"About {session.arrival_city}",
                text_content=narration_text,
                narration_type="destination_tour",
            ),
            lat_used=lat,
            lng_used=lng,
            position_source=position_source,
        )

    # --- Tier 4: Region/ocean context (with cooldown) ---
    cooldown_minutes = REGION_NARRATION_COOLDOWN_MINUTES
    last_region = session.last_region_narration_at
    cooldown_passed = last_region is None or datetime.now(UTC) - last_region > timedelta(
        minutes=cooldown_minutes
    )

    if cooldown_passed:
        try:
            region_text = await generate_region_narration(client, lat, lng, language=body.language)
            await record_region_narration(redis, session_id, lat, lng)
            return PositionUpdateResponse(
                triggered=True,
                narration=NarrationTrigger(
                    source_id=None,
                    name="Current region",
                    text_content=region_text,
                    narration_type="region",
                ),
                lat_used=lat,
                lng_used=lng,
                position_source=position_source,
            )
        except Exception as exc:
            # Region narration is best-effort and must never block the flight
            # or crash the position endpoint -- a failure here just means we
            # stay silent until the next GPS ping.
            logger.warning("Region narration failed at (%s, %s): %s", lat, lng, exc)

    # Nothing to narrate right now
    await record_position_and_narration(redis, session_id, lat, lng, None)
    return PositionUpdateResponse(
        triggered=False,
        lat_used=lat,
        lng_used=lng,
        position_source=position_source,
    )
