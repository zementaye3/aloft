from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.dependencies import get_current_user, get_database
from app.models.upcoming_flight import UpcomingFlight
from app.models.user import User

router = APIRouter(prefix="/flights/upcoming", tags=["upcoming_flights"])


class RegisterFlightRequest(BaseModel):
    flight_iata: str | None = None
    departure_iata: str
    arrival_iata: str
    departure_time: datetime


@router.post("")
async def register_upcoming_flight(
    body: RegisterFlightRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    """Register an upcoming flight to get pre-flight notifications."""
    flight = UpcomingFlight(
        flight_id=str(uuid.uuid4()),
        user_id=current_user.user_id,
        flight_iata=body.flight_iata,
        departure_iata=body.departure_iata,
        arrival_iata=body.arrival_iata,
        departure_time=body.departure_time,
    )
    await db.upcoming_flights.insert_one(flight.to_mongo_dict())
    return {"message": "Flight registered", "flight_id": flight.flight_id}


@router.get("")
async def list_upcoming_flights(
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    from datetime import UTC

    cursor = db.upcoming_flights.find(
        {
            "user_id": current_user.user_id,
            "departure_time": {"$gte": datetime.now(UTC)},
        },
        sort=[("departure_time", 1)],
    )
    flights = []
    async for doc in cursor:
        doc.pop("_id", None)
        flights.append(doc)
    return {"upcoming_flights": flights}


@router.delete("/{flight_id}")
async def cancel_upcoming_flight(
    flight_id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    result = await db.upcoming_flights.delete_one(
        {
            "flight_id": flight_id,
            "user_id": current_user.user_id,
        }
    )
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Flight not found")
    return {"message": "Flight removed"}
