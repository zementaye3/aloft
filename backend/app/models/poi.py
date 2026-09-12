from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator


class Poi(BaseModel):
    name: str
    location: dict  # GeoJSON Point: {"type": "Point", "coordinates": [lng, lat]}
    source: str
    source_id: str
    image_refs: list[str] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("location")
    @classmethod
    def validate_geojson_point(cls, v: dict) -> dict:
        """Ensure location is a valid GeoJSON Point with in-range coordinates.

        GeoJSON coordinate order is [longitude, latitude] -- the reverse of
        what most people expect. Validates here so bad data is caught at
        model construction rather than silently producing wrong results in
        the 2dsphere index or position-tracking distance calculations.
        """
        if v.get("type") != "Point":
            raise ValueError("location.type must be 'Point'")
        coords = v.get("coordinates", [])
        if len(coords) != 2:
            raise ValueError("location.coordinates must be [longitude, latitude] (2 elements)")
        lng, lat = coords[0], coords[1]
        if not isinstance(lng, int | float) or not isinstance(lat, int | float):
            raise ValueError("location.coordinates must contain numeric values")
        if not (-180 <= lng <= 180):
            raise ValueError(f"longitude {lng} out of range [-180, 180]")
        if not (-90 <= lat <= 90):
            raise ValueError(f"latitude {lat} out of range [-90, 90]")
        return v

    def to_mongo_dict(self) -> dict:
        return self.model_dump(mode="json")
