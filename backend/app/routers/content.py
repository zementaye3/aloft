from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.dependencies import (
    content_generation_rate_limit,
    download_rate_limit,
    get_current_user,
    get_database,
    get_http_client,
    get_redis,
    require_permission,
)
from app.models.role import Permission
from app.models.user import User
from app.services.content_job_service import create_content_job, get_job_status
from app.services.download_service import RouteNotFoundError, build_download_zip
from app.services.route_bundle_repository import get_route_bundle

router = APIRouter(prefix="/v1/routes", tags=["content"])


class CreateContentJobResponse(BaseModel):
    job_id: str
    route_key: str
    total_pois: int
    message: str


class ContentJobStatusResponse(BaseModel):
    job_id: str
    route_key: str
    status: str  # pending | processing | completed | failed
    total: int
    completed: int
    failed: int
    progress_percent: float
    created_at: str
    updated_at: str


@router.post(
    "/{route_key}/content",
    response_model=CreateContentJobResponse,
    dependencies=[
        Depends(content_generation_rate_limit()),
        Depends(require_permission(Permission.CREATE_CONTENT)),
    ],
)
async def start_content_generation(
    route_key: str,
    language: str = "en",
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis = Depends(get_redis),
    _: User = Depends(get_current_user),
) -> CreateContentJobResponse:
    """Queue content generation for a route. Returns immediately with a
    job_id -- poll GET /routes/{route_key}/content/status to track progress.

    This is intentionally asynchronous: generating stories + audio for
    200 POIs on a long-haul route takes 10-15 minutes when respecting
    Groq's free-tier rate limits. A synchronous endpoint would time out.
    """
    if redis is None:
        raise HTTPException(
            status_code=503,
            detail="Content generation queue unavailable (Redis not configured).",
        )

    redis_client = redis

    bundle = await get_route_bundle(db, route_key)
    if bundle is None:
        raise HTTPException(
            status_code=404,
            detail=f"No route found for '{route_key}'. Discover it first.",
        )

    job_id = await create_content_job(redis_client, route_key, bundle.poi_source_ids, language)

    return CreateContentJobResponse(
        job_id=job_id,
        route_key=route_key,
        total_pois=len(bundle.poi_source_ids),
        message=(
            f"Content generation queued for {len(bundle.poi_source_ids)} POIs. "
            f"Poll GET /v1/routes/{route_key}/content/status?job_id={job_id} to track progress."
        ),
    )


@router.get("/{route_key}/content/status", response_model=ContentJobStatusResponse)
async def get_content_generation_status(
    route_key: str,
    job_id: str = Query(..., description="Job ID from POST /routes/{route_key}/content"),
    _: User = Depends(get_current_user),
    __: None = Depends(require_permission(Permission.READ_CONTENT)),
) -> ContentJobStatusResponse:
    """Poll this endpoint to track content generation progress."""
    redis_client = get_redis()
    if redis_client is None:
        raise HTTPException(status_code=503, detail="Queue unavailable.")

    job = await get_job_status(redis_client, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")

    total = job["total"] or 1
    progress = round((job["completed"] / total) * 100, 1)

    return ContentJobStatusResponse(
        job_id=job_id,
        route_key=route_key,
        status=job["status"],
        total=job["total"],
        completed=job["completed"],
        failed=job["failed"],
        progress_percent=progress,
        created_at=job["created_at"],
        updated_at=job["updated_at"],
    )


@router.get(
    "/{route_key}/download",
    dependencies=[
        Depends(download_rate_limit()),
        Depends(require_permission(Permission.DOWNLOAD_ROUTE)),
    ],
)
async def download_route_bundle(
    route_key: str,
    include_images: bool = Query(default=True, description="Bundle POI images into the ZIP"),
    language: str = Query(default="en"),
    _: User = Depends(get_current_user),
    client=Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> Response:
    """Download a route's generated stories + audio (+ images) as a ZIP.

    Returns application/zip with a manifest.json describing the contents.
    404 if route_key doesn't match a previously discovered route.
    """
    try:
        zip_bytes = await build_download_zip(
            client,
            db,
            route_key,
            language=language,
            include_images=include_images,
        )
    except RouteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{route_key}.zip"'},
    )
