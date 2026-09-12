from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Story(BaseModel):
    poi_source_id: str
    language: str
    text_content: str
    model_version: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_mongo_dict(self) -> dict:
        return self.model_dump(mode="json")
