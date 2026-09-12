"""
Centralized logging configuration with structured logging and correlation ID support.

Supports multiple log aggregation services:
- Local file logging (development)
- Render logs (production)
- CloudWatch (AWS)
- Datadog
- Logstash/ELK
"""

import logging
import logging.config
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

# Context variable for correlation ID
correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

try:
    import structlog

    STRUCTLOG_AVAILABLE = True
except ImportError:
    STRUCTLOG_AVAILABLE = False


def setup_logging(
    environment: str = "development",
    log_level: str = "INFO",
    log_format: str = "json",
) -> None:
    """Configure structured logging for the application.

    Args:
        environment: Environment name (development, staging, production)
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_format: Log format (json, console, text)
    """
    if STRUCTLOG_AVAILABLE:
        # Configure structlog
        if log_format == "json":
            # JSON-structured logs for production
            structlog.configure(
                processors=[
                    structlog.contextvars.merge_contextvars,
                    structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="iso"),
                    structlog.processors.StackInfoRenderer(),
                    structlog.processors.format_exc_info,
                    structlog.processors.JSONRenderer(),
                ],
                context_class=dict,
                logger_factory=structlog.PrintLoggerFactory(),
                cache_logger_on_first_use=True,
            )
        else:
            # Human-readable console logs for development
            structlog.configure(
                processors=[
                    structlog.contextvars.merge_contextvars,
                    structlog.processors.add_log_level,
                    structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S"),
                    structlog.dev.ConsoleRenderer(),
                ],
                context_class=dict,
                logger_factory=structlog.PrintLoggerFactory(),
                cache_logger_on_first_use=True,
            )

    # Configure standard logging for third-party libraries
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper()),
    )

    # Set up log levels for specific libraries
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("motor").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("redis").setLevel(logging.WARNING)


def get_logger(name: str):
    """Get a structured logger with correlation ID support.

    Args:
        name: Logger name (typically __name__)

    Returns:
        Structured logger instance or standard logger
    """
    if STRUCTLOG_AVAILABLE:
        return structlog.get_logger(name)
    return logging.getLogger(name)


class LoggingMiddleware:
    """Middleware to add correlation ID and request context to logs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            # Extract or generate correlation ID
            headers = dict(scope.get("headers", []))
            correlation_id_value = headers.get(b"x-correlation-id")

            if correlation_id_value:
                correlation_id_value = correlation_id_value.decode()
            else:
                correlation_id_value = str(uuid4())

            # Set correlation ID in context
            correlation_id.set(correlation_id_value)

            if STRUCTLOG_AVAILABLE:
                # Add to structlog context
                structlog.contextvars.bind_contextvars(
                    correlation_id=correlation_id_value,
                    method=scope.get("method"),
                    path=scope.get("path"),
                )

        try:
            await self.app(scope, receive, send)
        finally:
            if STRUCTLOG_AVAILABLE:
                # Clear context after request
                structlog.contextvars.unbind_contextvars("correlation_id", "method", "path")


def log_security_event(
    event_type: str,
    severity: str = "info",
    user_id: str | None = None,
    ip: str | None = None,
    **kwargs: Any,
) -> None:
    """Log a security event with structured data.

    Args:
        event_type: Type of security event (e.g., RATE_LIMIT_VIOLATION)
        severity: Event severity (debug, info, warning, error, critical)
        user_id: User ID if available
        ip: Client IP address
        **kwargs: Additional event data
    """
    logger = get_logger("aloft.security")
    log_data = {
        "event_type": event_type,
        "user_id": user_id,
        "ip": ip,
        "timestamp": datetime.now(UTC).isoformat(),
        **kwargs,
    }

    if STRUCTLOG_AVAILABLE:
        log_func = getattr(logger, severity.lower(), logger.info)
        log_func(**log_data)
    else:
        getattr(logger, severity.upper())(log_data)


def log_api_error(
    endpoint: str,
    method: str,
    status_code: int,
    error: str,
    user_id: str | None = None,
    **kwargs: Any,
) -> None:
    """Log an API error with structured data.

    Args:
        endpoint: API endpoint path
        method: HTTP method
        status_code: HTTP status code
        error: Error message
        user_id: User ID if available
        **kwargs: Additional error data
    """
    logger = get_logger("aloft.api")
    log_data = {
        "endpoint": endpoint,
        "method": method,
        "status_code": status_code,
        "error": error,
        "user_id": user_id,
        "timestamp": datetime.now(UTC).isoformat(),
        **kwargs,
    }

    if STRUCTLOG_AVAILABLE:
        logger.error(**log_data)
    else:
        logger.error(log_data)


def log_performance_metric(
    operation: str,
    duration_ms: float,
    success: bool = True,
    **kwargs: Any,
) -> None:
    """Log a performance metric.

    Args:
        operation: Operation name (e.g., database_query, external_api_call)
        duration_ms: Duration in milliseconds
        success: Whether operation succeeded
        **kwargs: Additional metric data
    """
    logger = get_logger("aloft.performance")
    log_data = {
        "operation": operation,
        "duration_ms": duration_ms,
        "success": success,
        "timestamp": datetime.now(UTC).isoformat(),
        **kwargs,
    }

    if STRUCTLOG_AVAILABLE:
        level = "info" if success else "warning"
        getattr(logger, level)(**log_data)
    else:
        if success:
            logger.info(log_data)
        else:
            logger.warning(log_data)
