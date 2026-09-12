from datetime import UTC, datetime

from pydantic import BaseModel, Field


class FavoritePlace(BaseModel):
    favorite_id: str
    user_id: str
    poi_source_id: str
    poi_name: str
    lat: float
    lng: float
    story_snippet: str | None = None  # first 100 chars of the story
    saved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_mongo_dict(self) -> dict:
        return self.model_dump()
