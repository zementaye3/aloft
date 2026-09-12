"""
Polls for GDPR deletions whose grace period has elapsed and actually
purges them. Mirrors notification_worker.py's polling shape.
"""

from __future__ import annotations

import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorDatabase
from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.gdpr_service import process_due_deletions

logger = logging.getLogger("aloft.services.gdpr_worker")


async def run_gdpr_deletion_worker(db: AsyncIOMotorDatabase, redis: Redis | None) -> None:
    settings = get_settings()
    logger.info("GDPR deletion worker started")
    while True:
        try:
            deleted = await process_due_deletions(db, redis)
            if deleted:
                logger.warning("GDPR deletion worker purged %d account(s)", deleted)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("GDPR deletion worker error")
        await asyncio.sleep(settings.gdpr_deletion_worker_interval_seconds)
