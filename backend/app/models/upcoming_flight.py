from datetime import UTC, datetime

from pydantic import BaseModel, Field


class UpcomingFlight(BaseModel):
    flight_id: str
    user_id: str
    flight_iata: str | None = None
    departure_iata: str
    arrival_iata: str
    departure_time: datetime
    notification_sent: bool = False
    bundle_ready: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def to_mongo_dict(self) -> dict:
        return self.model_dump()
