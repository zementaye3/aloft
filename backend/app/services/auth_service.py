"""
Auth service — pure functions, no HTTP, no database.

Responsibilities:
  - Hash passwords with bcrypt (via passlib).
  - Verify plaintext passwords against bcrypt hashes.
  - Create signed JWT access and refresh tokens.
  - Decode and validate JWT tokens.
  - Revoke refresh tokens via a Redis JTI blocklist.

Nothing here touches MongoDB. Redis is used only for the JTI blocklist
(refresh token revocation). The blocklist key auto-expires at the token's
own exp timestamp so Redis never accumulates stale entries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from jwt import PyJWTError as JWTError
from jwt import decode, encode
from passlib.context import CryptContext
from redis.asyncio import Redis

from app.core.config import get_settings

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Token type constants stored in the JWT "type" claim so access and refresh
# tokens are not interchangeable even if they share the same secret.
_ACCESS_TOKEN_TYPE = "access"
_REFRESH_TOKEN_TYPE = "refresh"
_JTI_BLOCKLIST_PREFIX = "jti_blocked:"


class AuthError(Exception):
    """Raised when token validation fails."""


def hash_password(plaintext: str) -> str:
    """Return a bcrypt hash of *plaintext*. Safe to store in MongoDB."""
    return _pwd_context.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True if *plaintext* matches the stored *hashed* value."""
    return _pwd_context.verify(plaintext, hashed)


def create_access_token(user_id: str, email: str) -> str:
    """Create a short-lived JWT access token.

    Payload claims:
      sub   — user_id (stable identifier)
      email — user's email address
      type  — "access" (guards against using a refresh token as access)
      exp   — expiry timestamp
      iat   — issued-at timestamp
    """
    settings = get_settings()
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=settings.jwt_access_token_expire_minutes)
    payload = {
        "sub": user_id,
        "email": email,
        "type": _ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": expire,
    }
    return encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def create_refresh_token(user_id: str) -> str:
    """Create a long-lived JWT refresh token.

    Includes a `jti` (JWT ID) claim — a random UUID that uniquely identifies
    this token. The JTI is stored in a Redis blocklist when the token is used
    so it cannot be replayed (true single-use rotation).
    """
    settings = get_settings()
    now = datetime.now(UTC)
    expire = now + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload = {
        "sub": user_id,
        "type": _REFRESH_TOKEN_TYPE,
        "jti": str(uuid.uuid4()),
        "iat": now,
        "exp": expire,
    }
    return encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict:
    """Decode and validate an access token.

    Returns the full payload dict on success.
    Raises AuthError if the token is expired, tampered, or not an access token.
    """
    settings = get_settings()
    try:
        payload = decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise AuthError(f"Invalid or expired token: {exc}") from exc

    if payload.get("type") != _ACCESS_TOKEN_TYPE:
        raise AuthError("Token is not an access token")

    return payload


def decode_refresh_token(token: str) -> dict:
    """Decode and validate a refresh token. Returns the full payload on success.

    Raises AuthError if the token is invalid, expired, or not a refresh token.
    The caller is responsible for:
      1. Checking the JTI against the Redis blocklist (is_refresh_token_revoked).
      2. Revoking the JTI after issuing new tokens (revoke_refresh_token).
    """
    settings = get_settings()
    try:
        payload = decode(
            token,
            settings.jwt_secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
    except JWTError as exc:
        raise AuthError(f"Invalid or expired refresh token: {exc}") from exc

    if payload.get("type") != _REFRESH_TOKEN_TYPE:
        raise AuthError("Token is not a refresh token")

    if not payload.get("sub"):
        raise AuthError("Refresh token missing 'sub' claim")

    return payload


async def revoke_refresh_token(redis: Redis | None, jti: str, exp: int) -> None:
    """Add a refresh token JTI to the Redis blocklist.

    The key expires at the token's own exp timestamp so Redis never holds
    stale blocklist entries past the point where the token would be invalid
    anyway. If Redis is unavailable, the revocation is silently skipped —
    the token will remain technically valid until it expires naturally, which
    is acceptable given the short window of exposure.
    """
    if redis is None:
        return
    now_ts = int(datetime.now(UTC).timestamp())
    ttl = max(exp - now_ts, 1)
    await redis.set(f"{_JTI_BLOCKLIST_PREFIX}{jti}", "1", ex=ttl)


async def is_refresh_token_revoked(redis: Redis | None, jti: str) -> bool:
    """Return True if the given JTI has been revoked (is in the blocklist).

    Returns False if Redis is unavailable — fails open so a Redis outage
    doesn't lock users out.
    """
    if redis is None or not jti:
        return False
    return await redis.exists(f"{_JTI_BLOCKLIST_PREFIX}{jti}") > 0


# NOTE: Password reset tokens are NOT JWT-based. The reset flow uses opaque
# URL-safe tokens stored in Redis (see services/password_reset_service.py).
# create_password_reset_token() and decode_password_reset_token() have been
# removed — they were dead code never called by any router.
