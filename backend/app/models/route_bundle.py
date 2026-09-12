from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class RouteBundle(BaseModel):
    route_key: str
    departure: tuple[float, float]
    arrival: tuple[float, float]
    poi_source_ids: list[str]
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_mongo_dict(self) -> dict:
        return self.model_dump(mode="json")
