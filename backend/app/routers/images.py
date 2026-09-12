"""
HTTP layer for fetching real photos of a POI.

Image source priority:
  1. Wikipedia/Wikimedia Commons (get_images) -- editorially curated,
     no attribution required in most contexts, higher average quality.
  2. Openverse (search_images) -- CC-licensed fallback used ONLY when
     Wikipedia returns zero images. Openverse results may require attribution
     display; the response includes creator + licence_url for that purpose.

No AI-generated images, no generic placeholders -- if nothing real and clear
exists from either source, the honest answer is an empty list.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.clients.openverse import OpenverseClientError, OpenverseImage, search_images
from app.clients.wikipedia import WikipediaClientError, get_images
from app.core.dependencies import (
    get_current_user,
    get_database,
    get_http_client,
    image_retrieval_rate_limit,
    require_permission,
)
from app.models.role import Permission
from app.models.user import User
from app.services.poi_repository import get_poi, save_poi_images

router = APIRouter(prefix="/v1/pois", tags=["images"])
logger = logging.getLogger("aloft.routers.images")


class ImageInfo(BaseModel):
    url: str
    width: int
    height: int
    is_lead_image: bool
    # Attribution fields -- only populated for Openverse images (Wikipedia
    # images are CC BY-SA via Wikimedia and credited to Wikipedia).
    source: str = "wikipedia"  # "wikipedia" | "openverse"
    licence: str = ""
    licence_url: str = ""
    creator: str = ""
    creator_url: str = ""


class PoiImagesResponse(BaseModel):
    poi_source_id: str
    images: list[ImageInfo]


@router.post(
    "/{source_id}/images",
    response_model=PoiImagesResponse,
    summary="Fetch real photos for a POI",
    dependencies=[
        Depends(image_retrieval_rate_limit()),
        Depends(require_permission(Permission.READ_POI)),
    ],
)
async def fetch_images(
    source_id: str,
    max_images: int = 4,
    _: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> PoiImagesResponse:
    """Fetch and persist real photos for a previously discovered POI.

    **Image source priority:**
    1. **Wikipedia / Wikimedia Commons** — editorially curated, higher quality,
       no attribution required in most contexts.
    2. **Openverse** (CC-licensed fallback) — queried only when Wikipedia returns
       zero images. Results include `creator` and `licence_url` for attribution.

    An empty `images` list is a valid and honest result — no real, clear photo
    exists for this place from either source.

    404 if the POI was never discovered via `POST /routes/pois`.
    """
    poi = await get_poi(db, source_id)
    if poi is None:
        raise HTTPException(
            status_code=404,
            detail=f"No POI found for source_id '{source_id}'. Discover it first via POST /routes/pois.",
        )

    image_infos: list[ImageInfo] = []

    # --- Primary: Wikipedia ---
    try:
        raw_images = await get_images(client, poi.name, max_images=max_images)
        image_infos = [
            ImageInfo(
                url=img.url,
                width=img.width,
                height=img.height,
                is_lead_image=img.is_lead_image,
                source="wikipedia",
            )
            for img in raw_images
        ]
    except WikipediaClientError as exc:
        logger.warning("Wikipedia image fetch failed for '%s': %s", poi.name, exc)

    # --- Fallback: Openverse ---
    if not image_infos:
        try:
            ov_images: list[OpenverseImage] = await search_images(
                client, poi.name, max_images=max_images
            )
            image_infos = [
                ImageInfo(
                    url=img.url,
                    width=img.width,
                    height=img.height,
                    is_lead_image=False,
                    source="openverse",
                    licence=img.licence,
                    licence_url=img.licence_url,
                    creator=img.creator,
                    creator_url=img.creator_url,
                )
                for img in ov_images
            ]
        except OpenverseClientError:
            pass  # honest empty list is better than a 500

    await save_poi_images(db, source_id, [img.url for img in image_infos])

    return PoiImagesResponse(poi_source_id=source_id, images=image_infos)
