from __future__ import annotations

import asyncio
import json
import logging
import logging.config
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.config_validation import validate_configuration
from app.core.db import close_mongo_connection, connect_to_mongo
from app.core.logging_config import LoggingMiddleware, setup_logging
from app.core.redis import close_redis_connection, connect_to_redis, get_optional_redis
from app.middleware.security import (
    CSPReportMiddleware,
    RateLimitLoggingMiddleware,
    RequestLoggingMiddleware,
    SecurityHeadersMiddleware,
)
from app.routers import (
    audio,
    auth,
    content,
    favorites,
    flights,
    gdpr,
    images,
    journal,
    legal,
    location_flights,
    pois,
    sessions,
    stories,
    upcoming_flights,
)

# ---------------------------------------------------------------------------
# Structured JSON logging (production) / plain text (development)
# ---------------------------------------------------------------------------


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line.

    Fields: timestamp (ISO-8601), level, logger, message, plus any extra
    fields attached via LogRecord. In production these land in whatever log
    aggregator is consuming stdout (CloudWatch, Datadog, Loki, etc.) and are
    immediately searchable/filterable without regex parsing.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # Include any extra fields added via `logger.info("...", extra={...})`
        for key, value in record.__dict__.items():
            if key not in logging.LogRecord.__dict__ and not key.startswith("_"):
                try:
                    json.dumps(value)  # only include JSON-serialisable extras
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = str(value)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging() -> None:
    """Set up the root logger with structured logging.

    - ENVIRONMENT=production  → JSON formatter to stdout (log aggregator friendly)
    - Any other value         → human-readable format to stdout (dev/test friendly)

    Either way, the log level is controlled by LOG_LEVEL (default INFO).
    """
    settings = get_settings()
    log_format = "json" if settings.environment.lower() == "production" else "console"
    setup_logging(
        environment=settings.environment,
        log_level=settings.log_level,
        log_format=log_format,
    )


_configure_logging()
logger = logging.getLogger("aloft.main")


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()

    # Validate configuration before starting
    try:
        validate_configuration(settings)
        logger.info("Configuration validation passed")
    except Exception as exc:
        logger.error("FATAL: Configuration validation failed: %s", exc)
        raise

    # Log secret validation status
    try:
        from app.core.secrets import log_secret_validation_results
        log_secret_validation_results()
    except Exception as exc:
        logger.warning("Secret validation check failed: %s", exc)

    try:
        await connect_to_mongo()
    except Exception as exc:
        logger.error("FATAL: MongoDB connection failed: %s", exc)
        raise
    try:
        await connect_to_redis()
    except Exception as exc:
        logger.error("FATAL: Redis connection failed: %s", exc)
        raise

    # Initialize security monitor with Redis if available
    try:
        from app.services.security_monitoring import init_security_monitor

        init_security_monitor(get_optional_redis())
    except Exception:
        from app.services.security_monitoring import init_security_monitor

        init_security_monitor(None)

    app.state.http_client = httpx.AsyncClient()

    # Start background content worker if Redis is available
    worker_task = None
    notification_task = None
    gdpr_deletion_task = None
    redis_client = get_optional_redis()
    if redis_client is not None:
        from app.core.db import get_db
        from app.services.content_worker import run_worker

        db = get_db()
        worker_task = asyncio.create_task(run_worker(redis_client, db, app.state.http_client))
        logger.info("Content generation worker started")

        # Start notification worker for pre-flight notifications
        from app.services.notification_worker import run_notification_worker

        notification_task = asyncio.create_task(run_notification_worker(db, app.state.http_client))
        logger.info("Notification worker started")

        # Start GDPR deletion worker for scheduled account purges
        from app.services.gdpr_worker import run_gdpr_deletion_worker

        gdpr_deletion_task = asyncio.create_task(run_gdpr_deletion_worker(db, redis_client))
        logger.info("GDPR deletion worker started")
    else:
        logger.warning("Redis not available -- content generation worker disabled")

    _JWT_DEFAULT = "change-me-in-production-use-secrets-token-hex-32"
    if settings.jwt_secret_key.get_secret_value() == _JWT_DEFAULT:
        logger.warning(
            "JWT_SECRET_KEY is set to the insecure default value. "
            "All tokens are trivially forgeable. "
            "Set a strong random secret in your .env file: "
            'python -c "import secrets; print(secrets.token_hex(32))"'
        )
    logger.info(
        "Aloft backend started",
        extra={"environment": settings.environment, "log_level": settings.log_level},
    )
    yield

    if worker_task is not None:
        worker_task.cancel()
        import contextlib

        with contextlib.suppress(asyncio.CancelledError):
            await worker_task

    if notification_task is not None:
        notification_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await notification_task

    if gdpr_deletion_task is not None:
        gdpr_deletion_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await gdpr_deletion_task

    await app.state.http_client.aclose()
    await close_redis_connection()
    await close_mongo_connection()
    logger.info("Aloft backend shut down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Aloft",
    version="1.0.0",
    description=(
        "Aloft turns any commercial flight into a live audio tour.\n\n"
        "**API Versioning:**\n"
        "All endpoints are now prefixed with `/v1/` for version control. "
        "This ensures backward compatibility when future API changes are introduced.\n\n"
        "**Authentication:**\n"
        "All endpoints except `/health`, `POST /v1/auth/signup`, `POST /v1/auth/login`, "
        "`POST /v1/auth/refresh`, `POST /v1/auth/forgot-password`, and `POST /v1/auth/reset-password` "
        "require a valid JWT access token.\n"
        "Include it as `Authorization: Bearer <token>`.\n"
        "Email verification is disabled — accounts are auto-verified on signup.\n"
        "Use `POST /v1/auth/logout` to invalidate refresh tokens.\n"
        "Use `POST /v1/auth/forgot-password` and `POST /v1/auth/reset-password` for password recovery.\n\n"
        "**Core flow:**\n"
        "1. `POST /v1/auth/signup` — create account (auto-verified, can log in immediately)\n"
        "2. `POST /v1/routes/pois` — discover points of interest along a route\n"
        "3. Batch content generation (recommended for routes with many POIs):\n"
        "   - `POST /v1/routes/{route_key}/content` — queue background job for all POIs\n"
        "   - `GET /v1/routes/{route_key}/content/status?job_id=...` — poll progress\n"
        "4. Per-POI content (on demand, only when user selects a POI):\n"
        "   - `POST /v1/pois/{source_id}/images` — fetch photos\n"
        "   - `POST /v1/pois/{source_id}/story` — generate narration text\n"
        "   - `POST /v1/pois/{source_id}/audio` — synthesize audio\n"
        "   - `POST /v1/pois/{source_id}/audio/mixed` — audio with music bed\n"
        "5. `POST /v1/sessions` — start a live flight session\n"
        "6. `POST /v1/sessions/{session_id}/position` — send GPS, get narration triggers\n\n"
        "**Or, all-in-one by flight number:**\n"
        "`POST /v1/flights/{flight_iata}/pois` — looks up the route via AviationStack then runs step 2.\n\n"
        "**Location-based discovery:**\n"
        "`GET /v1/flights/location/airports/nearby?lat=X&lng=Y` — find airports near your location\n"
        "`GET /v1/flights/location/recommendations?lat=X&lng=Y` — get flights departing from nearby airports\n"
        "`GET /v1/flights/location/city/{city_name}` — get flights departing from a specific city"
    ),
    contact={"name": "Aloft", "email": get_settings().app_contact_email},
    license_info={"name": "MIT"},
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Security Middleware
# ---------------------------------------------------------------------------
# Add correlation ID and structured logging middleware
app.add_middleware(LoggingMiddleware)
# Add CSP report handler for report-only mode
app.add_middleware(CSPReportMiddleware)
# Add security headers and logging middleware
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(RateLimitLoggingMiddleware)

# ---------------------------------------------------------------------------
# CORS Configuration
# ---------------------------------------------------------------------------
# Configure CORS for frontend integration. In production, replace "*" with
# specific allowed origins for security.
settings = get_settings()
allowed_origins = (
    settings.cors_allowed_origins if hasattr(settings, "cors_allowed_origins") else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------
app.include_router(auth.router)
app.include_router(pois.router)
app.include_router(stories.router)
app.include_router(audio.router)
app.include_router(content.router)
app.include_router(flights.router)
app.include_router(images.router)
app.include_router(sessions.router)
app.include_router(legal.router)
app.include_router(gdpr.router)
app.include_router(journal.router)
app.include_router(favorites.router)
app.include_router(upcoming_flights.router)
app.include_router(location_flights.router)


@app.get("/health", tags=["meta"], summary="Health check")
async def health() -> dict[str, str]:
    """Returns `{"status": "ok"}` when the service is running.

    Suitable for use as a load balancer or container health check probe.
    Does not check downstream services (MongoDB, Redis) — use this only
    to verify the process is alive and accepting HTTP.
    For a full readiness check including downstream liveness, use GET /health/ready.
    """
    return {"status": "ok"}


@app.get("/health/ready", tags=["meta"], summary="Readiness check — includes downstream liveness")
async def health_ready() -> dict:
    """Checks that MongoDB and Redis are reachable.

    Returns 200 with per-service status when all required services are up.
    Returns 503 if any required service is unreachable.

    Use this for Kubernetes readiness probes or deployment smoke tests.
    Redis failure is reported but does not cause a 503 because Redis is
    optional infrastructure (rate limiting and sessions degrade gracefully).
    """
    from app.core.db import get_db
    from app.core.redis import get_redis as _get_session_redis

    result: dict = {"mongodb": "unknown", "redis": "unknown"}
    failed = False

    # MongoDB check
    try:
        db = get_db()
        await db.command("ping")
        result["mongodb"] = "ok"
    except Exception as exc:
        result["mongodb"] = f"error: {exc}"
        failed = True

    # Redis check (optional — failure is reported but not fatal)
    try:
        redis = _get_session_redis()
        await redis.ping()
        result["redis"] = "ok"
    except RuntimeError:
        # Redis not configured — expected in minimal dev setups
        result["redis"] = "not configured"
    except Exception as exc:
        result["redis"] = f"error: {exc}"
        # Redis is optional; don't fail the readiness check for it

    if failed:
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=503, content={"status": "degraded", **result})

    return {"status": "ok", **result}


def main() -> None:
    """Entry point for running the server directly."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
