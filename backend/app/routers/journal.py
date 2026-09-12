from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response

from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel

from app.core.dependencies import get_current_user, get_database, spectator_view_rate_limit
from app.models.flight_journal import FlightJournalEntry
from app.models.user import User
from app.services.flight_journal_service import (
    disable_journal_sharing,
    enable_journal_sharing,
    get_flight_history,
    get_journal_entry_by_id,
    get_journal_entry_by_share_token,
    get_user_stats,
)

router = APIRouter(prefix="/journal", tags=["journal"])


class ShareJournalEntryResponse(BaseModel):
    share_token: str
    share_path: str


def _share_card_payload(entry: FlightJournalEntry) -> dict:
    """Shape a journal entry into the reduced fields a share card shows.

    Shared by the owner-scoped preview endpoint and the public token
    endpoint, so both surfaces stay in sync automatically.
    """
    return {
        "departure": entry.departure_name,
        "arrival": entry.arrival_name,
        "distance_km": round(entry.distance_km),
        "flight_date": entry.flight_date.strftime("%B %d, %Y"),
        "places_narrated": entry.narrated_poi_names[:5],
        "countries": entry.countries_flown_over[:3],
        "tagline": f"Flew over {len(entry.narrated_poi_names)} amazing places",
    }


@router.get("/stats")
async def my_stats(
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    """Returns total flights, distance, countries, places, badges."""
    stats = await get_user_stats(db, current_user.user_id)
    return stats


@router.get("/history")
async def my_flights(
    limit: int = 20,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    """Returns paginated flight history for the journal."""
    entries = await get_flight_history(db, current_user.user_id, limit=limit)
    return {"flights": entries, "count": len(entries)}


@router.get("/share-card/{entry_id}")
async def get_share_card_data(
    entry_id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
):
    """Owner-scoped preview of the share card, before deciding to share it.

    This is NOT a public link -- it requires being signed in as the entry's
    owner. To get a link that works for people without an account, call
    POST /journal/{entry_id}/share and use the returned token with
    GET /journal/shared/{token} instead.
    """
    entry = await get_journal_entry_by_id(db, entry_id, current_user.user_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Flight not found")
    return _share_card_payload(entry)


@router.post("/{entry_id}/share", response_model=ShareJournalEntryResponse)
async def share_journal_entry(
    entry_id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
) -> ShareJournalEntryResponse:
    """Generate a public, read-only share link for this journal entry.

    Anyone with the resulting token can view the share card via
    GET /journal/shared/{token} -- no account or login required. Idempotent:
    calling this again while sharing is already on returns the same token
    rather than rotating it, so an already-shared link keeps working.

    404 if the entry doesn't exist or isn't owned by you.
    """
    token = await enable_journal_sharing(db, entry_id, current_user.user_id)
    if token is None:
        raise HTTPException(status_code=404, detail="Flight not found")
    return ShareJournalEntryResponse(share_token=token, share_path=f"/journal/shared/{token}")


@router.delete("/{entry_id}/share", status_code=204)
async def unshare_journal_entry(
    entry_id: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
    current_user: User = Depends(get_current_user),
) -> Response:
    """Revoke this journal entry's share link. The old token stops working immediately.

    404 if the entry doesn't exist or isn't owned by you.
    """
    ok = await disable_journal_sharing(db, entry_id, current_user.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Flight not found")
    return Response(status_code=204)


@router.get("/shared/{token}", dependencies=[Depends(spectator_view_rate_limit())])
async def view_shared_journal_entry(
    token: str,
    db: AsyncIOMotorDatabase = Depends(get_database),
):
    """Fully public, unauthenticated view of a shared flight's share card.

    This is the actual public link -- pair it with POST /journal/{entry_id}/share
    to generate the token. Reuses the spectator-view rate-limit bucket since
    it's the same shape of endpoint (public, IP-limited, token-keyed).

    404 if the token is invalid or has been revoked.
    """
    entry = await get_journal_entry_by_share_token(db, token)
    if entry is None:
        raise HTTPException(status_code=404, detail="This share link is invalid or has expired.")
    return _share_card_payload(entry)
