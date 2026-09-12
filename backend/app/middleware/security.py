"""
Security middleware for HTTP headers and security hardening.

Implements OWASP-recommended security headers and protections:
- Content Security Policy (CSP)
- Strict-Transport-Security (HSTS)
- X-Content-Type-Options
- X-Frame-Options
- X-XSS-Protection
- Referrer-Policy
- Permissions-Policy
"""

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.config import get_settings


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds security headers to all HTTP responses."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Content Security Policy
        # Restricts sources of content (scripts, styles, images, etc.)
        # In production, configure CSP_ALLOWED_ORIGINS in your environment to restrict domains
        settings = get_settings()
        csp_connect_src = "'self'"
        if (
            hasattr(settings, "csp_allowed_connect_origins")
            and settings.csp_allowed_connect_origins
        ):
            csp_connect_src = f"'self' {' '.join(settings.csp_allowed_connect_origins)}"
        else:
            # Development defaults - allow common API endpoints
            csp_connect_src = "'self' https://api.groq.com https://api.elevenlabs.io https://api.aviationstack.com"

        # Build CSP policy.
        # This is a pure REST API — no server-side HTML, no scripts, no styles
        # served from this origin. Script directives are locked down to 'none'
        # so any injected script tag in a hypothetical HTML response has zero
        # execution surface. 'unsafe-inline' and 'unsafe-eval' are explicitly
        # NOT included.
        csp_policy = (
            "default-src 'none'; "
            "script-src 'none'; "
            "style-src 'none'; "
            "img-src 'none'; "
            "font-src 'none'; "
            f"connect-src {csp_connect_src}; "
            "frame-ancestors 'none'; "
            "form-action 'none'; "
            "base-uri 'none'; "
            "report-to csp-endpoint"
        )

        # Add Report-To header for CSP violation reports
        response.headers["Report-To"] = (
            '{"group": "csp-endpoint", "max_age": 86400, "endpoints": [{"url": "/csp-report"}]}'
        )

        # Use report-only mode if configured (for testing)
        if settings.csp_report_only:
            response.headers["Content-Security-Policy-Report-Only"] = csp_policy
        else:
            response.headers["Content-Security-Policy"] = csp_policy

        # Strict-Transport-Security
        # Enforces HTTPS for 1 year including subdomains in production
        # In development, use a shorter max-age to allow testing with HTTP
        if settings.environment.lower() == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )
        else:
            # Development: shorter max-age for testing flexibility
            response.headers["Strict-Transport-Security"] = "max-age=300"

        # X-Content-Type-Options
        # Prevents MIME-type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # X-Frame-Options
        # Prevents clickjacking attacks
        response.headers["X-Frame-Options"] = "DENY"

        # X-XSS-Protection
        # Enables XSS filter (mostly for older browsers)
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Referrer-Policy
        # Controls how much referrer information is sent
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions-Policy (formerly Feature-Policy)
        # Controls browser features access
        response.headers["Permissions-Policy"] = (
            "geolocation=(), "
            "microphone=(), "
            "camera=(), "
            "payment=(), "
            "usb=(), "
            "magnetometer=(), "
            "gyroscope=(), "
            "accelerometer=()"
        )

        # X-DNS-Prefetch-Control
        # Controls DNS prefetching
        response.headers["X-DNS-Prefetch-Control"] = "off"

        # Cross-Origin-Opener-Policy
        # Controls cross-origin opener access
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"

        # Cross-Origin-Resource-Policy
        # Controls cross-origin resource access
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

        # Cache-Control for sensitive endpoints
        # Prevents caching of sensitive data
        if request.url.path in ["/v1/auth/me", "/v1/auth/login", "/v1/auth/refresh"]:
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs all HTTP requests for security auditing and monitoring."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Extract client information
        client_ip = request.client.host if request.client else "unknown"
        raw_ua = request.headers.get("user-agent", "unknown")
        # Sanitize user agent: strip control characters (including newlines, tabs,
        # carriage returns) to prevent log injection attacks where a crafted
        # User-Agent like "Chrome\nFake log line" injects extra log entries.
        user_agent = "".join(ch if ch.isprintable() else " " for ch in raw_ua)[:200]

        # Log request
        import logging

        logger = logging.getLogger("aloft.security")

        logger.info(
            f"Request: {request.method} {request.url.path} | "
            f"IP: {client_ip} | "
            f"User-Agent: {user_agent} | "
            f"Auth: {'Present' if 'authorization' in request.headers else 'Missing'}"
        )

        # Process request
        response = await call_next(request)

        # Log response
        logger.info(
            f"Response: {response.status_code} | Path: {request.url.path} | IP: {client_ip}"
        )

        return response


class RateLimitLoggingMiddleware(BaseHTTPMiddleware):
    """Logs rate limit violations for security monitoring."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # Log rate limit violations
        if response.status_code == 429:
            import logging

            logger = logging.getLogger("aloft.security")

            client_ip = request.client.host if request.client else "unknown"
            raw_ua = request.headers.get("user-agent", "unknown")
            user_agent = "".join(ch if ch.isprintable() else " " for ch in raw_ua)[:200]
            logger.warning(
                f"Rate limit violation: {request.method} {request.url.path} | "
                f"IP: {client_ip} | "
                f"User-Agent: {user_agent}"
            )

            # Log to security monitor
            try:
                from app.services.security_monitoring import log_rate_limit_violation

                # Extract user_id from the JWT Bearer token if present.
                # Decode without verification — we only need the sub claim for
                # logging purposes, and we don't want a verification failure to
                # swallow the rate-limit log entry.
                user_id: str | None = None
                auth_header = request.headers.get("authorization", "")
                if auth_header.startswith("Bearer "):
                    token = auth_header[len("Bearer ") :]
                    try:
                        import jwt

                        payload = jwt.decode(
                            token,
                            options={"verify_signature": False},
                            algorithms=["HS256"],
                        )
                        user_id = payload.get("sub")
                    except Exception:
                        pass  # Malformed token — log without user_id

                await log_rate_limit_violation(
                    user_id=user_id,
                    ip=client_ip,
                    endpoint=str(request.url.path),
                    method=request.method,
                )
            except Exception as e:
                # Don't break the middleware if security monitoring fails
                logger.error(f"Failed to log to security monitor: {e}")

        return response


class CSPReportMiddleware(BaseHTTPMiddleware):
    """Logs Content Security Policy violation reports in report-only mode."""

    async def dispatch(self, request: Request, call_next) -> Response:
        # Handle CSP violation reports
        if request.method == "POST" and request.url.path == "/csp-report":
            import json
            import logging

            logger = logging.getLogger("aloft.security")

            try:
                report_data = await request.json()
                logger.warning(f"CSP violation report: {json.dumps(report_data, indent=2)}")

                # Log to security monitoring if available
                try:
                    from app.core.logging_config import log_security_event

                    csp_report = report_data.get("csp-report", {})
                    log_security_event(
                        event_type="CSP_VIOLATION",
                        severity="warning",
                        violated_directive=csp_report.get("violated-directive"),
                        blocked_uri=csp_report.get("blocked-uri"),
                        document_uri=csp_report.get("document-uri"),
                        original_policy=csp_report.get("original-policy"),
                    )
                except Exception as e:
                    logger.error(f"Failed to log CSP violation to security monitor: {e}")

                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=204, content={})
            except Exception as e:
                logger.error(f"Failed to process CSP report: {e}")
                from fastapi.responses import JSONResponse

                return JSONResponse(status_code=400, content={"error": "Invalid CSP report"})

        return await call_next(request)
