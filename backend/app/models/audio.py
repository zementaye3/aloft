from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class AudioAsset(BaseModel):
    poi_source_id: str
    language: str
    voice_name: str
    file_path: str
    format: str = "mp3"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_mongo_dict(self) -> dict:
        return self.model_dump(mode="json")
