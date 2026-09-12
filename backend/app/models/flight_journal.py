from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class FlightJournalEntry(BaseModel):
    entry_id: str
    user_id: str
    route_key: str
    flight_iata: str | None = None
    departure_name: str
    arrival_name: str
    departure_iata: str | None = None
    arrival_iata: str | None = None
    distance_km: float
    narrated_poi_names: list[str] = Field(default_factory=list)
    countries_flown_over: list[str] = Field(default_factory=list)
    favorited_place_names: list[str] = Field(default_factory=list)
    flight_date: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    # Set once the owner enables public sharing via POST /journal/{entry_id}/share.
    # None means sharing is off; the token is opaque and unguessable, and looked
    # up directly (never enumerated), same pattern as FlightSession.share_token.
    share_token: str | None = None

    def to_mongo_dict(self) -> dict:
        return self.model_dump()


class UserStats(BaseModel):
    user_id: str
    total_flights: int = 0
    total_distance_km: float = 0.0
    total_countries: int = 0
    total_places_narrated: int = 0
    all_countries: list[str] = Field(default_factory=list)
    all_narrated_poi_names: list[str] = Field(default_factory=list)
    badges_earned: list[str] = Field(default_factory=list)
