"""
Password reset service using Redis for token storage.

Password reset flow:
1. User requests reset → generate random token → store in Redis (reset:{token} → user_id, TTL 15m)
2. Send email with reset link containing token
3. User clicks link → validate token from Redis → update password → delete token
4. Tokens are single-use and expire after 15 minutes

Configuration:
  - REDIS_URL: Redis connection string (uses rate limit Redis client)
"""

from __future__ import annotations

import logging
import secrets

from redis.asyncio import Redis

from app.core.config import get_settings
from app.services.email_service import EmailError, send_email

logger = logging.getLogger("aloft.password_reset")

# Redis key prefix for password reset tokens
_PASSWORD_RESET_TOKEN_PREFIX = "reset:"
# Token TTL: 15 minutes in seconds
_PASSWORD_RESET_TOKEN_TTL = 900


class PasswordResetError(Exception):
    """Raised when password reset operations fail."""


def generate_password_reset_token() -> str:
    """Generate a secure random password reset token.

    Returns:
        A URL-safe random string suitable for use in email links.
    """
    return secrets.token_urlsafe(32)


async def store_password_reset_token(redis: Redis | None, user_id: str, token: str) -> None:
    """Store a password reset token in Redis.

    Args:
        redis: Redis client (can be None if Redis not configured)
        user_id: The user ID to reset password for
        token: The password reset token

    Raises:
        PasswordResetError: If Redis is not configured
    """
    if redis is None:
        raise PasswordResetError(
            "Redis is not configured. Password reset requires Redis. "
            "Set REDIS_URL in your environment variables."
        )

    key = f"{_PASSWORD_RESET_TOKEN_PREFIX}{token}"
    await redis.set(key, user_id, ex=_PASSWORD_RESET_TOKEN_TTL)
    logger.info(f"Stored password reset token for user {user_id}")


async def get_user_id_from_reset_token(redis: Redis | None, token: str) -> str | None:
    """Retrieve user_id from a password reset token.

    Args:
        redis: Redis client (can be None if Redis not configured)
        token: The password reset token

    Returns:
        The user_id if token is valid, None otherwise
    """
    if redis is None:
        return None

    key = f"{_PASSWORD_RESET_TOKEN_PREFIX}{token}"
    user_id = await redis.get(key)
    return user_id or None


async def delete_password_reset_token(redis: Redis | None, token: str) -> None:
    """Delete a password reset token from Redis (single-use).

    Args:
        redis: Redis client (can be None if Redis not configured)
        token: The password reset token to delete
    """
    if redis is None:
        return

    key = f"{_PASSWORD_RESET_TOKEN_PREFIX}{token}"
    await redis.delete(key)
    logger.info(f"Deleted password reset token {token}")


async def send_password_reset_email(
    redis: Redis | None,
    user_id: str,
    email: str,
) -> str:
    """Generate a password reset token, store it, and send it via email.

    Args:
        redis: Redis client (can be None in development/testing)
        user_id: The user ID to reset password for
        email: The user's email address

    Returns:
        The password reset token (for testing purposes)

    Raises:
        PasswordResetError: If email sending fails (only in production)
    """
    token = generate_password_reset_token()

    settings = get_settings()

    # Try to store the token if Redis is available
    if redis is None:
        if settings.environment.lower() == "production":
            raise PasswordResetError(
                "Redis is not configured. Password reset requires Redis. "
                "Set REDIS_URL in your environment variables."
            )
        logger.warning(
            f"Redis not available - password reset token for {email}: "
            f"{settings.frontend_base_url}/reset-password.html?token={token}"
        )
    else:
        await store_password_reset_token(redis, user_id, token)

    reset_link = f"{settings.frontend_base_url}/reset-password.html?token={token}"

    # Email content
    subject = "Reset your Aloft password"
    html_content = f"""
    <html>
        <body>
            <h2>Reset your password</h2>
            <p>Click the link below to reset your Aloft password.</p>
            <p><a href="{reset_link}">Reset Password</a></p>
            <p>This link expires in 15 minutes.</p>
            <p>If you didn't request a password reset, ignore this email.</p>
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
        logger.info(f"Password reset email sent to {email}")
    except EmailError as exc:
        # Log the error but don't fail the request - the token is still valid
        logger.warning(f"Email service not configured. Reset link for {email}: {reset_link}")
        if settings.environment.lower() == "production":
            raise PasswordResetError(f"Failed to send password reset email: {exc}") from exc

    return token
