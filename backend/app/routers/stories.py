from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.dependencies import (
    get_current_user,
    get_database,
    get_http_client,
    require_permission,
    story_generation_rate_limit,
)
from app.models.role import Permission
from app.models.user import User
from app.services.poi_repository import get_poi
from app.services.story_repository import get_story, save_story
from app.services.story_service import (
    InsufficientFactsError,
    generate_story,
    supported_languages,
)

router = APIRouter(prefix="/v1/pois", tags=["stories"])


class StoryResponse(BaseModel):
    poi_source_id: str
    language: str
    text_content: str


@router.post(
    "/{source_id}/story",
    response_model=StoryResponse,
    summary="Generate a narration story for a POI",
    dependencies=[
        Depends(story_generation_rate_limit()),
        Depends(require_permission(Permission.CREATE_CONTENT)),
    ],
)
async def create_story(
    source_id: str = Path(..., max_length=200, description="POI source ID, e.g. `wikipedia:12345`"),
    language: str = "en",
    force: bool = False,
    _: User = Depends(get_current_user),
    client: httpx.AsyncClient = Depends(get_http_client),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> StoryResponse:
    """Generate a spoken-word narration story for a previously discovered POI.

    Uses Groq (LLaMA 3) to produce a 2–3 sentence narrative suitable for
    reading aloud while a passenger looks out the aircraft window.

    - Results are cached in MongoDB — the same POI + language combination
      returns the cached result on subsequent calls.
    - Pass `force=true` to regenerate (useful after editing the prompt).
    - 404 if the POI hasn't been discovered yet — run `POST /routes/pois` first.

    Supported languages: en, ar, fr, de, es, zh, hi, am (and others supported
    by LLaMA 3 — pass any BCP-47 code and it will try).
    """
    if language not in supported_languages():
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language '{language}'. Supported: {', '.join(supported_languages())}",
        )

    poi = await get_poi(db, source_id)
    if poi is None:
        raise HTTPException(
            status_code=404,
            detail=f"No POI found for source_id '{source_id}'. Discover it first via POST /routes/pois.",
        )

    if not force:
        cached = await get_story(db, source_id, language)
        if cached is not None:
            return StoryResponse(
                poi_source_id=cached.poi_source_id,
                language=cached.language,
                text_content=cached.text_content,
            )

    try:
        story = await generate_story(client, source_id, poi.name, language=language)
    except InsufficientFactsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await save_story(db, story)
    return StoryResponse(
        poi_source_id=story.poi_source_id,
        language=story.language,
        text_content=story.text_content,
    )
