"""
Checks for upcoming flights every 5 minutes.
Sends pre-flight notification 2 hours before departure.
Uses OneSignal free tier (10,000 notifications/month free).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import httpx
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings

logger = logging.getLogger("aloft.services.notification_worker")


async def run_notification_worker(
    db: AsyncIOMotorDatabase,
    http_client: httpx.AsyncClient,
) -> None:
    logger.info("Notification worker started")
    while True:
        try:
            await _check_and_notify(db, http_client)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Notification worker error")
        await asyncio.sleep(300)  # check every 5 minutes


async def _check_and_notify(db: AsyncIOMotorDatabase, client: httpx.AsyncClient) -> None:
    now = datetime.now(UTC)
    two_hours_from_now = now + timedelta(hours=2)
    notify_window_start = now + timedelta(hours=1, minutes=50)

    # Find flights departing in ~2 hours that haven't been notified
    cursor = db.upcoming_flights.find(
        {
            "departure_time": {
                "$gte": notify_window_start,
                "$lte": two_hours_from_now,
            },
            "notification_sent": False,
        }
    )

    async for doc in cursor:
        user_id = doc["user_id"]
        flight_id = doc["flight_id"]
        departure = doc["departure_iata"]
        arrival = doc["arrival_iata"]

        await _send_push_notification(
            client,
            user_id,
            title="Your flight is in 2 hours ✈️",
            message=(f"{departure} → {arrival} | Download your offline bundle now while on WiFi"),
        )

        await db.upcoming_flights.update_one(
            {"flight_id": flight_id}, {"$set": {"notification_sent": True}}
        )


async def _send_push_notification(
    client: httpx.AsyncClient,
    user_id: str,
    title: str,
    message: str,
) -> None:
    settings = get_settings()
    if not settings.onesignal_app_id or not settings.onesignal_api_key:
        logger.info("OneSignal not configured, skipping notification for user %s", user_id)
        return

    try:
        await client.post(
            "https://onesignal.com/api/v1/notifications",
            headers={
                "Authorization": f"Basic {settings.onesignal_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "app_id": settings.onesignal_app_id,
                "filters": [{"field": "tag", "key": "user_id", "value": user_id}],
                "headings": {"en": title},
                "contents": {"en": message},
            },
            timeout=10.0,
        )
    except Exception as exc:
        logger.warning("Push notification failed for user %s: %s", user_id, exc)
