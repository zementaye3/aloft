from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.dependencies import get_current_user, get_database
from app.models.favorite import FavoritePlace
from app.models.user import User
from app.services.poi_repository import get_poi
from app.services.story_repository import get_story

router = APIRouter(prefix="/favorites", tags=["favorites"])


class AddFavoriteRequest(BaseModel):
    poi_source_id: str
    language: str = "en"


@router.post("")
async def add_favorite(
    body: AddFavoriteRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    """Save a place to the user's wishlist during or after a flight."""
    poi = await get_poi(db, body.poi_source_id)
    if poi is None:
        raise HTTPException(status_code=404, detail="POI not found")

    story = await get_story(db, body.poi_source_id, body.language)
    snippet = story.text_content[:120] if story else None

    existing = await db.favorites.find_one(
        {
            "user_id": current_user.user_id,
            "poi_source_id": body.poi_source_id,
        }
    )
    if existing:
        return {"message": "Already in favorites"}

    poi_lng, poi_lat = poi.location["coordinates"]
    fav = FavoritePlace(
        favorite_id=str(uuid.uuid4()),
        user_id=current_user.user_id,
        poi_source_id=body.poi_source_id,
        poi_name=poi.name,
        lat=poi_lat,
        lng=poi_lng,
        story_snippet=snippet,
    )
    await db.favorites.insert_one(fav.to_mongo_dict())
    return {"message": "Saved to favorites", "favorite": fav}


@router.get("")
async def list_favorites(
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    """Get all saved places."""
    cursor = db.favorites.find(
        {"user_id": current_user.user_id},
        sort=[("saved_at", -1)],
    )
    favs = []
    async for doc in cursor:
        doc.pop("_id", None)
        favs.append(doc)
    return {"favorites": favs, "count": len(favs)}


@router.delete("/{poi_source_id}")
async def remove_favorite(
    poi_source_id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    """Remove a place from favorites."""
    result = await db.favorites.delete_one(
        {
            "user_id": current_user.user_id,
            "poi_source_id": poi_source_id,
        }
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not in favorites")
    return {"message": "Removed from favorites"}
