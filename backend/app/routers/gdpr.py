"""
GDPR compliance router - Data access and deletion endpoints.

Implements GDPR Article 15 (Right of Access) and Article 17 (Right to
Erasure). See app/services/gdpr_service.py for the real implementation --
this router is intentionally thin.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.core.dependencies import (
    download_rate_limit,
    get_current_user,
    get_database,
    get_redis,
)
from app.models.user import User
from app.services.gdpr_service import (
    cancel_deletion,
    collect_user_export,
    get_deletion_status,
    schedule_deletion,
)

router = APIRouter(prefix="/v1/user", tags=["gdpr"])
logger = logging.getLogger("aloft.gdpr")


class DataDeletionConfirmation(BaseModel):
    """Confirmation for data deletion request."""

    require_confirmation: bool = Field(default=True, description="Must be true to confirm deletion")
    reason: str = Field(
        ..., min_length=10, max_length=500, description="Reason for data deletion request"
    )


class DataDeletionResponse(BaseModel):
    """Response for data deletion request."""

    user_id: str
    email: str
    deletion_requested: bool
    deletion_scheduled: datetime
    data_categories_to_delete: list[str]


@router.get(
    "/data",
    summary="Export all user data (GDPR Article 15)",
    description=(
        "Returns all personal data associated with the user account: profile, "
        "favorites, flight journal entries, flight stats, and upcoming flights. "
        "Excludes shared content caches (POIs, narration, audio, routes) since "
        "those aren't tied to any individual user."
    ),
    dependencies=[
        Depends(download_rate_limit()),
    ],
)
async def export_user_data(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> dict[str, Any]:
    """Export all user data for GDPR compliance.

    This endpoint returns all personal data stored about the user,
    implementing the GDPR Right of Access (Article 15).
    """
    logger.info(f"Data export requested by user: {current_user.user_id}")
    return await collect_user_export(db, current_user)


@router.delete(
    "/data",
    response_model=DataDeletionResponse,
    summary="Delete all user data (GDPR Article 17)",
    description=(
        "Schedules deletion of all personal data associated with the account. "
        "The account is deactivated immediately (blocks login) but the actual "
        "data purge happens after a grace period, during which the deletion "
        "can be cancelled via POST /v1/user/data/cancel-deletion. "
        "This action cannot be undone once the grace period elapses."
    ),
)
async def delete_user_data(
    confirmation: DataDeletionConfirmation,
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis = Depends(get_redis),
) -> DataDeletionResponse:
    """Delete all user data for GDPR compliance.

    This endpoint permanently deletes all personal data associated with
    the user account, implementing the GDPR Right to Erasure (Article 17).
    """
    if not confirmation.require_confirmation:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Explicit confirmation required for data deletion",
        )

    result = await schedule_deletion(db, redis, current_user, confirmation.reason)
    return DataDeletionResponse(**result)


@router.post(
    "/data/cancel-deletion",
    summary="Cancel pending data deletion",
    description="Cancel a pending data deletion request if the grace period hasn't elapsed yet.",
)
async def cancel_data_deletion(
    current_user: User = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_database),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Cancel a pending data deletion request."""
    cancelled = await cancel_deletion(db, redis, current_user.user_id)
    if not cancelled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No pending deletion found for this account.",
        )
    return {
        "status": "cancelled",
        "message": "Data deletion has been cancelled. Your account and data remain intact.",
    }


@router.get(
    "/data/status",
    summary="Check pending deletion status",
    description="Check whether this account has a pending scheduled deletion.",
)
async def get_data_status(
    current_user: User = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Check the status of pending data operations."""
    pending = await get_deletion_status(redis, current_user.user_id)
    if pending is None:
        return {
            "user_id": current_user.user_id,
            "deletion_status": "none",
        }
    return {
        "user_id": current_user.user_id,
        "deletion_status": "pending",
        **pending,
    }
