from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Airport(BaseModel):
    iata_code: str
    name: str
    lat: float
    lng: float
    city: str | None = None
    country: str | None = None
    cached_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_mongo_dict(self) -> dict:
        data = self.model_dump(mode="json")
        # Add GeoJSON location for geospatial queries
        data["location"] = {
            "type": "Point",
            "coordinates": [self.lng, self.lat],  # GeoJSON uses [longitude, latitude]
        }
        return data
