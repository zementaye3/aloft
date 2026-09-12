from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class FlightSession(BaseModel):
    session_id: str
    route_key: str
    # The user_id of the user who created this session.
    # Stored so update_position can verify the caller owns the session
    # and reject cross-user position submissions.
    owner_id: str = ""
    # Narration language captured at session start, so the public spectator
    # view (which has no request body to pass a language in) knows which
    # story text to read back.
    language: str = "en"
    # Opt-in public share token (None = sharing off). Set via POST
    # /{session_id}/share, cleared via DELETE /{session_id}/share.
    share_token: str | None = None
    narrated_poi_source_ids: list[str] = Field(default_factory=list)
    upcoming_poi_triggered_source_ids: list[str] = Field(default_factory=list)
    last_region_narration_at: datetime | None = None
    last_position: tuple[float, float] | None = None

    # Destination tour fields
    arrival_country: str | None = None
    arrival_city: str | None = None
    destination_tour_narrations: list[str] = Field(default_factory=list)
    destination_tour_index: int = 0
    last_destination_tour_at: datetime | None = None

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_mongo_dict(self) -> dict:
        return self.model_dump(mode="json")
