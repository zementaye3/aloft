"""
Email verification service using Redis for token storage.

Verification flow:
1. User signs up → generate random token → store in Redis (verify:{token} → user_id, TTL 24h)
2. Send email with verification link containing token
3. User clicks link → validate token from Redis → mark user as verified → delete token
4. Tokens are single-use and expire after 24 hours

Configuration:
  - REDIS_URL: Redis connection string (uses rate limit Redis client)
"""

from __future__ import annotations

import logging
import secrets

from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.email_service import EmailError, send_email

logger = logging.getLogger("aloft.verification")

# Redis key prefix for verification tokens
_VERIFICATION_TOKEN_PREFIX = "verify:"
# Token TTL: 24 hours in seconds
_VERIFICATION_TOKEN_TTL = 86400


class VerificationError(Exception):
    """Raised when verification operations fail."""


def generate_verification_token() -> str:
    """Generate a secure random verification token.

    Returns:
        A URL-safe random string suitable for use in email links.
    """
    return secrets.token_urlsafe(32)


async def store_verification_token(redis: Redis | None, user_id: str, token: str) -> None:
    """Store a verification token in Redis.

    Args:
        redis: Redis client (can be None if Redis not configured)
        user_id: The user ID to verify
        token: The verification token

    Raises:
        VerificationError: If Redis is not configured
    """
    if redis is None:
        raise VerificationError(
            "Redis is not configured. Email verification requires Redis. "
            "Set REDIS_URL in your environment variables."
        )

    key = f"{_VERIFICATION_TOKEN_PREFIX}{token}"
    await redis.set(key, user_id, ex=_VERIFICATION_TOKEN_TTL)
    logger.info(f"Stored verification token for user {user_id}")


async def get_user_id_from_token(redis: Redis | None, token: str) -> str | None:
    """Retrieve user_id from a verification token.

    Args:
        redis: Redis client (can be None if Redis not configured)
        token: The verification token

    Returns:
        The user_id if token is valid, None otherwise
    """
    if redis is None:
        return None

    key = f"{_VERIFICATION_TOKEN_PREFIX}{token}"
    user_id = await redis.get(key)
    return user_id or None


async def delete_verification_token(redis: Redis | None, token: str) -> None:
    """Delete a verification token from Redis (single-use).

    Args:
        redis: Redis client (can be None if Redis not configured)
        token: The verification token to delete
    """
    if redis is None:
        return

    key = f"{_VERIFICATION_TOKEN_PREFIX}{token}"
    await redis.delete(key)
    logger.info(f"Deleted verification token {token}")


async def send_verification_email(
    redis: Redis | None,
    user_id: str,
    email: str,
) -> str:
    """Generate a verification token, store it, and send it via email.

    Args:
        redis: Redis client (can be None in development/testing)
        user_id: The user ID to verify
        email: The user's email address

    Returns:
        The verification token (for testing purposes)

    Raises:
        VerificationError: If email sending fails (only in production)
    """
    token = generate_verification_token()

    settings = get_settings()

    # Try to store the token if Redis is available
    if redis is None:
        if settings.environment.lower() == "production":
            raise VerificationError(
                "Redis is not configured. Email verification requires Redis. "
                "Set REDIS_URL in your environment variables."
            )
        logger.warning(
            f"Redis not available - verification token for {email}: "
            f"{settings.frontend_base_url}/verify-email?token={token}"
        )
    else:
        await store_verification_token(redis, user_id, token)

    verification_link = f"{settings.frontend_base_url}/verify-email?token={token}"

    # Email content
    subject = "Verify your Aloft account"
    html_content = f"""
    <html>
        <body>
            <h2>Verify your email address</h2>
            <p>Click the link below to verify your email address and activate your Aloft account.</p>
            <p><a href="{verification_link}">Verify Email</a></p>
            <p>This link expires in 24 hours.</p>
            <p>If you didn't create an account, ignore this email.</p>
            <p>— The Aloft Team</p>
        </body>
    </html>
    """

    try:
        await send_email(
            to_email=email,
            subject=subject,
            html_content=html_content,
        )
        logger.info(f"Verification email sent to {email}")
    except EmailError as exc:
        # Log the error but don't fail the request - the token is still valid
        logger.warning(
            f"Email service not configured. Verification link for {email}: {verification_link}"
        )
        if settings.environment.lower() == "production":
            raise VerificationError(f"Failed to send verification email: {exc}") from exc

    # Always return the token for development/testing purposes
    return token
