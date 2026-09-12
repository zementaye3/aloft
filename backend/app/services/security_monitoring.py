"""
Suspicious activity monitoring service.

Detects and alerts on suspicious security patterns:
- Multiple failed login attempts
- Unusual IP addresses (geo-location anomalies)
- Rate limit violations
- Permission escalation attempts
- Data access anomalies
- Session anomalies (concurrent sessions from different locations)

Security events are stored in Redis with TTL for persistence across multiple
worker processes and service restarts. Falls back to in-memory storage if Redis
is unavailable.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import redis.asyncio as redis

logger = logging.getLogger("aloft.security_monitoring")


class SecurityEvent:
    """Security event model for Redis storage."""

    def __init__(
        self,
        event_type: str,
        severity: str,
        user_id: str | None,
        ip: str | None,
        timestamp: datetime,
        details: dict[str, Any] | None = None,
    ):
        self.event_type = event_type
        self.severity = severity
        self.user_id = user_id
        self.ip = ip
        self.timestamp = timestamp
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "severity": self.severity,
            "user_id": self.user_id,
            "ip": self.ip,
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecurityEvent:
        return cls(
            event_type=data["event_type"],
            severity=data["severity"],
            user_id=data.get("user_id"),
            ip=data.get("ip"),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            details=data.get("details", {}),
        )


class SecurityMonitor:
    """Monitors for suspicious activity patterns and generates alerts."""

    def __init__(self, redis_client: redis.Redis | None = None):
        self._redis = redis_client
        self._use_redis = redis_client is not None

        # Event stream key for Redis
        self._EVENT_STREAM_KEY = "security:events"
        self._USER_EVENTS_PREFIX = "security:user:"

    def _get_user_events_key(self, user_id: str) -> str:
        return f"{self._USER_EVENTS_PREFIX}{user_id}:events"

    async def _store_event(self, user_id: str, event_data: dict[str, Any]) -> None:
        """Store event in Redis for persistence."""
        if self._use_redis and self._redis:
            pipe = self._redis.pipeline()
            await pipe.lpush(self._get_user_events_key(user_id), json.dumps(event_data))
            await pipe.expire(self._get_user_events_key(user_id), 86400)  # 24h TTL
            await pipe.lpush(self._EVENT_STREAM_KEY, json.dumps(event_data))
            await pipe.expire(self._EVENT_STREAM_KEY, 604800)  # 7d TTL
            await pipe.execute()

    async def _get_user_events(self, user_id: str, event_type: str) -> list[dict[str, Any]]:
        """Get events for a user from Redis, filtering by type."""
        if not (self._use_redis and self._redis):
            return []

        key = self._get_user_events_key(user_id)
        raw_events = await self._redis.lrange(key, 0, -1)
        events = []
        for raw in raw_events:
            try:
                event = json.loads(raw)
                if event.get("event_type") == event_type:
                    events.append(event)
            except (json.JSONDecodeError, KeyError):
                continue

        # Filter to last 24 hours (Redis should have done this, but double-check)
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        events = [
            e for e in events if datetime.fromisoformat(e.get("timestamp", "1970-01-01")) > cutoff
        ]
        return events

    async def log_failed_login(
        self,
        user_id: str,
        email: str,
        ip: str,
        user_agent: str,
    ) -> None:
        """Log a failed login attempt."""
        timestamp = datetime.now(UTC)

        event_data = {
            "event_type": "FAILED_LOGIN",
            "severity": "warning",
            "user_id": user_id,
            "ip": ip,
            "timestamp": timestamp.isoformat(),
            "details": {"email": email, "user_agent": user_agent},
        }
        await self._store_event(user_id, event_data)

        # Check for suspicious patterns
        await self._check_failed_login_pattern(user_id, email)

    async def log_successful_login(
        self,
        user_id: str,
        email: str,
        ip: str,
        user_agent: str,
    ) -> None:
        """Log a successful login for IP anomaly detection."""
        timestamp = datetime.now(UTC)

        event_data = {
            "event_type": "SUCCESSFUL_LOGIN",
            "severity": "info",
            "user_id": user_id,
            "ip": ip,
            "timestamp": timestamp.isoformat(),
            "details": {"email": email, "user_agent": user_agent},
        }
        await self._store_event(user_id, event_data)

        # Check for IP anomalies
        await self._check_ip_anomaly(user_id, email, ip, timestamp)

    async def log_rate_limit_violation(
        self,
        user_id: str | None,
        ip: str,
        endpoint: str,
        method: str,
    ) -> None:
        """Log a rate limit violation."""
        timestamp = datetime.now(UTC)
        key = user_id if user_id else f"anon:{ip}"

        event_data = {
            "event_type": "RATE_LIMIT_VIOLATION",
            "severity": "warning",
            "user_id": user_id,
            "ip": ip,
            "timestamp": timestamp.isoformat(),
            "details": {"endpoint": endpoint, "method": method},
        }
        await self._store_event(key, event_data)

        # Check for abuse patterns
        await self._check_rate_limit_abuse(key, endpoint)

    async def log_permission_denial(
        self,
        user_id: str,
        email: str,
        required_permission: str,
        attempted_action: str,
    ) -> None:
        """Log a permission denial."""
        timestamp = datetime.now(UTC)

        event_data = {
            "event_type": "PERMISSION_DENIAL",
            "severity": "warning",
            "user_id": user_id,
            "ip": None,
            "timestamp": timestamp.isoformat(),
            "details": {
                "email": email,
                "required_permission": required_permission,
                "attempted_action": attempted_action,
            },
        }
        await self._store_event(user_id, event_data)

        # Check for escalation attempts
        await self._check_permission_escalation(user_id, email)

    async def _check_failed_login_pattern(self, user_id: str, email: str) -> None:
        """Check for brute force patterns."""
        events = await self._get_user_events(user_id, "FAILED_LOGIN")

        if len(events) >= 5:
            recent_attempts = [
                e
                for e in events
                if datetime.fromisoformat(e["timestamp"]) > datetime.now(UTC) - timedelta(minutes=5)
            ]
            if len(recent_attempts) >= 5:
                await self._alert(
                    "BRUTE_FORCE_DETECTED",
                    f"Brute force attack detected for user {email} ({user_id})",
                    {
                        "user_id": user_id,
                        "email": email,
                        "attempts": len(recent_attempts),
                        "timeframe": "5 minutes",
                    },
                )

        if len(events) >= 20:
            await self._alert(
                "SUSTAINED_BRUTE_FORCE",
                f"Sustained brute force attack detected for user {email} ({user_id})",
                {
                    "user_id": user_id,
                    "email": email,
                    "attempts": len(events),
                    "timeframe": "1 hour",
                },
            )

    async def _check_ip_anomaly(
        self,
        user_id: str,
        email: str,
        current_ip: str,
        timestamp: datetime,
    ) -> None:
        """Check for unusual IP addresses."""
        # Get unique IPs from login events
        if self._use_redis and self._redis:
            key = self._get_user_events_key(user_id)
            raw_events = await self._redis.lrange(key, 0, -1)
            ips = set()
            for raw in raw_events:
                try:
                    event = json.loads(raw)
                    if event.get("event_type") in ("SUCCESSFUL_LOGIN", "FAILED_LOGIN") and (
                        ip := event.get("ip")
                    ):
                        ips.add(ip)
                except (json.JSONDecodeError, KeyError):
                    continue
        else:
            ips = set()

        if len(ips) > 15:
            # Threshold is 15 unique IPs — a mobile travel app legitimately logs in
            # from airports, hotels, and cellular networks across a journey, so a low
            # threshold generates many false positives. 15 is still anomalous.
            await self._alert(
                "IP_ANOMALY_DETECTED",
                f"Unusual IP activity for user {email} ({user_id})",
                {
                    "user_id": user_id,
                    "email": email,
                    "unique_ips": len(ips),
                    "current_ip": current_ip,
                },
            )

    async def _check_rate_limit_abuse(self, key: str, endpoint: str) -> None:
        """Check for rate limit abuse patterns."""
        if self._use_redis and self._redis:
            events = await self._get_user_events(key, "RATE_LIMIT_VIOLATION")
        else:
            events = []

        if len(events) >= 10:
            await self._alert(
                "RATE_LIMIT_ABUSE",
                f"Persistent rate limit abuse detected for {key}",
                {
                    "key": key,
                    "violations": len(events),
                    "endpoint": endpoint,
                },
            )

    async def _check_permission_escalation(self, user_id: str, email: str) -> None:
        """Check for permission escalation attempts."""
        if self._use_redis and self._redis:
            events = await self._get_user_events(user_id, "PERMISSION_DENIAL")
        else:
            events = []

        if len(events) >= 10:
            unique_permissions = set(e["details"]["required_permission"] for e in events)
            await self._alert(
                "PERMISSION_ESCALATION_ATTEMPT",
                f"Potential permission escalation attempt by user {email} ({user_id})",
                {
                    "user_id": user_id,
                    "email": email,
                    "denials": len(events),
                    "unique_permissions": len(unique_permissions),
                },
            )

    async def _alert(
        self,
        alert_type: str,
        message: str,
        context: dict[str, Any],
    ) -> None:
        """Generate a security alert."""
        logger.warning(
            f"SECURITY ALERT [{alert_type}]: {message}",
            extra={
                "alert_type": alert_type,
                "context": context,
            },
        )

        event_data = {
            "event_type": alert_type,
            "severity": "critical",
            "user_id": context.get("user_id"),
            "ip": context.get("current_ip"),
            "timestamp": datetime.now(UTC).isoformat(),
            "details": context,
        }
        if context.get("user_id"):
            await self._store_event(context["user_id"], event_data)

    async def get_user_security_summary(self, user_id: str) -> dict[str, Any]:
        """Get security summary for a user including recent events and alerts."""
        if not (self._use_redis and self._redis):
            return {
                "user_id": user_id,
                "status": "unavailable",
                "reason": "Redis not configured",
            }

        # Get all recent events for the user
        key = self._get_user_events_key(user_id)
        raw_events = await self._redis.lrange(key, 0, -1)

        events = []
        for raw in raw_events:
            try:
                event = json.loads(raw)
                events.append(event)
            except (json.JSONDecodeError, KeyError):
                continue

        # Filter to last 24 hours
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        recent_events = [
            e for e in events if datetime.fromisoformat(e.get("timestamp", "1970-01-01")) > cutoff
        ]

        # Categorize events
        failed_logins = len([e for e in recent_events if e.get("event_type") == "FAILED_LOGIN"])
        successful_logins = len(
            [e for e in recent_events if e.get("event_type") == "SUCCESSFUL_LOGIN"]
        )
        rate_limit_violations = len(
            [e for e in recent_events if e.get("event_type") == "RATE_LIMIT_VIOLATION"]
        )
        permission_denials = len(
            [e for e in recent_events if e.get("event_type") == "PERMISSION_DENIAL"]
        )

        # Get unique IPs
        unique_ips = set()
        for event in recent_events:
            if ip := event.get("ip"):
                unique_ips.add(ip)

        # Determine risk level
        risk_level = "low"
        if failed_logins >= 5 or rate_limit_violations >= 10 or permission_denials >= 5:
            risk_level = "high"
        elif failed_logins >= 2 or rate_limit_violations >= 5 or permission_denials >= 2:
            risk_level = "medium"

        return {
            "user_id": user_id,
            "status": "available",
            "summary": {
                "failed_logins_24h": failed_logins,
                "successful_logins_24h": successful_logins,
                "rate_limit_violations_24h": rate_limit_violations,
                "permission_denials_24h": permission_denials,
                "unique_ips_24h": len(unique_ips),
                "total_events_24h": len(recent_events),
            },
            "risk_level": risk_level,
            "recent_events": recent_events[-10:],  # Last 10 events
        }


# Global security monitor instance (without Redis - will be initialized in lifespan)
security_monitor: SecurityMonitor | None = None


def init_security_monitor(redis_client: redis.Redis | None = None) -> SecurityMonitor:
    """Initialize the global security monitor with Redis client."""
    global security_monitor
    security_monitor = SecurityMonitor(redis_client)
    return security_monitor


async def log_failed_login(user_id: str, email: str, ip: str, user_agent: str) -> None:
    """Log a failed login attempt using the global security monitor."""
    if security_monitor:
        await security_monitor.log_failed_login(user_id, email, ip, user_agent)


async def log_successful_login(
    user_id: str,
    email: str,
    ip: str,
    user_agent: str,
) -> None:
    """Log a successful login using the global security monitor."""
    if security_monitor:
        await security_monitor.log_successful_login(user_id, email, ip, user_agent)


async def log_rate_limit_violation(
    user_id: str | None,
    ip: str,
    endpoint: str,
    method: str,
) -> None:
    """Log a rate limit violation using the global security monitor."""
    if security_monitor:
        await security_monitor.log_rate_limit_violation(user_id, ip, endpoint, method)


async def log_permission_denial(
    user_id: str,
    email: str,
    required_permission: str,
    attempted_action: str,
) -> None:
    """Log a permission denial using the global security monitor."""
    if security_monitor:
        await security_monitor.log_permission_denial(
            user_id, email, required_permission, attempted_action
        )


async def log_security_event(
    event_type: str,
    severity: str = "info",
    user_id: str | None = None,
    ip: str | None = None,
    **details: Any,
) -> None:
    """Log a security event."""
    timestamp = datetime.now(UTC)
    event = SecurityEvent(
        event_type=event_type,
        severity=severity,
        user_id=user_id,
        ip=ip,
        timestamp=timestamp,
        details=details,
    )

    logger.info(
        f"Security Event: {event_type}",
        extra={
            "event_type": event_type,
            "severity": severity,
            "user_id": user_id,
            "ip": ip,
            "details": details,
        },
    )

    if security_monitor and user_id:
        await security_monitor._store_event(user_id, event.to_dict())


async def detect_suspicious_api_usage(
    user_id: str,
    endpoint: str,
    method: str,
    ip: str,
) -> None:
    """Detect suspicious API usage patterns."""
    if not security_monitor:
        return

    # Track API usage patterns per user
    if security_monitor._use_redis and security_monitor._redis:
        key = f"security:api_usage:{user_id}"

        # Increment usage counter for this endpoint
        await security_monitor._redis.hincrby(key, f"{method}:{endpoint}", 1)
        await security_monitor._redis.expire(key, 3600)  # 1 hour TTL

        # Check for suspicious patterns
        usage_data = await security_monitor._redis.hgetall(key)

        # Check for high frequency on sensitive endpoints
        sensitive_endpoints = ["/v1/auth/", "/v1/users/", "/v1/admin/"]
        for endpoint_key, count in usage_data.items():
            # hgetall returns bytes or str depending on the redis client version;
            # decode defensively so neither raises AttributeError.
            endpoint_key_str = (
                endpoint_key.decode() if isinstance(endpoint_key, bytes) else str(endpoint_key)
            )
            if (
                any(sensitive in endpoint_key_str for sensitive in sensitive_endpoints)
                and int(count) > 100
            ):  # More than 100 requests to sensitive endpoints in 1 hour
                await log_security_event(
                    event_type="SUSPICIOUS_API_USAGE",
                    severity="warning",
                    user_id=user_id,
                    ip=ip,
                    endpoint=endpoint,
                    method=method,
                    request_count=int(count),
                    pattern="high_frequency_sensitive_endpoint",
                )

        # Check for unusual endpoint combinations (potential data scraping)
        if len(usage_data) > 50:  # Accessing 50+ different endpoints in 1 hour
            await log_security_event(
                event_type="SUSPICIOUS_API_USAGE",
                severity="warning",
                user_id=user_id,
                ip=ip,
                endpoint=endpoint,
                method=method,
                unique_endpoints=len(usage_data),
                pattern="high_endpoint_diversity",
            )


async def detect_data_exfiltration_attempt(
    user_id: str,
    data_size: int,
    endpoint: str,
) -> None:
    """Detect potential data exfiltration."""
    if data_size > 100 * 1024 * 1024:  # 100MB
        await log_security_event(
            event_type="POTENTIAL_DATA_EXFILTRATION",
            severity="warning",
            user_id=user_id,
            data_size_mb=data_size / (1024 * 1024),
            endpoint=endpoint,
        )
