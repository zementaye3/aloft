"""
Authentication router — account creation, login, token refresh, logout, password reset, and profile.

Public endpoints (no JWT required):
  POST /auth/signup            — create a new account
  POST /auth/login             — exchange email+password for tokens
  POST /auth/refresh           — exchange a refresh token for a new access token
  POST /auth/forgot-password   — request a password reset email
  POST /auth/reset-password    — reset password with a token

Protected endpoint (JWT required):
  POST /auth/logout   — invalidate refresh token
  GET  /auth/me       — return the current user's public profile

Note: Email verification is disabled for development/testing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr, Field
from redis.asyncio import Redis

from app.core.config import get_settings
from app.core.dependencies import get_current_user, get_database, rate_limit
from app.core.redis import get_redis as _get_session_redis
from app.core.redis_client import get_redis as _get_rate_limit_redis
from app.models.user import User, UserPublic
from app.services.auth_service import (
    AuthError,
    create_access_token,
    create_refresh_token,
    decode_refresh_token,
    hash_password,
    is_refresh_token_revoked,
    revoke_refresh_token,
    verify_password,
)
from app.services.password_reset_service import (
    PasswordResetError,
    delete_password_reset_token,
    get_user_id_from_reset_token,
    send_password_reset_email,
)
from app.services.security_monitoring import (
    log_failed_login,
    log_security_event,
    log_successful_login,
)
from app.services.user_repository import (
    create_user,
    get_user_by_email,
    get_user_by_id,
    update_user,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])
logger = logging.getLogger("aloft.auth")

# Brute-force protection for auth endpoints.
# Login: 20 attempts/hour per IP is generous for legitimate use, blocks password guessing.
# Signup: 10 accounts/hour per IP prevents account-creation spam.
_login_rate_limit = rate_limit(
    "auth_login", max_requests=20, window_seconds=3600, use_user_id=False
)
_signup_rate_limit = rate_limit(
    "auth_signup", max_requests=10, window_seconds=3600, use_user_id=False
)


def _get_optional_redis() -> Redis | None:
    """Return the session Redis client, or None if Redis is not configured."""
    try:
        return _get_session_redis()
    except RuntimeError:
        return None


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    model_config = {
        "json_schema_extra": {
            "examples": [{"email": "passenger@example.com", "password": "strongpassword123"}]
        }
    }


class LoginRequest(BaseModel):
    email: EmailStr
    password: str

    model_config = {
        "json_schema_extra": {
            "examples": [{"email": "passenger@example.com", "password": "strongpassword123"}]
        }
    }


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(..., min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class MessageResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/signup",
    response_model=MessageResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new account",
    dependencies=[Depends(_signup_rate_limit)],
)
async def signup(
    body: SignupRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> MessageResponse:
    """Create a new user account (email verification bypassed for development).

    - Email is stored lowercase and must be unique.
    - Password must be at least 8 characters.
    - Account is created with is_verified=True for immediate login.
    - Email verification is disabled for development/testing.

    422 if the email is invalid.
    409 if the email is already registered.
    """
    try:
        user = await create_user(db, body.email, hash_password(body.password))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Auto-verify user for development/testing
    user.is_verified = True
    await update_user(db, user)

    logger.info(f"Account created and auto-verified for {user.email}")

    return MessageResponse(
        message="Account created successfully. You can now log in."
    )


# Email verification endpoints removed for development/testing
# class VerifyEmailRequest(BaseModel):
#     token: str
#
#
# @router.post(
#     "/verify-email",
#     response_model=TokenResponse,
#     summary="Verify email address with token",
# )
# async def verify_email(
#     body: VerifyEmailRequest,
#     db: AsyncIOMotorDatabase = Depends(get_database),
# ) -> TokenResponse:
#     """Verify email address using a token from the verification email.
#
#     Accepts the token in the **request body** (not a query parameter) so the
#     token is never written to server access logs or browser history.
#
#     Validates the token from Redis, marks the user as verified, and returns
#     authentication tokens (this is when the user gets logged in for the first time).
#
#     The token is single-use and expires after 24 hours.
#
#     400 if the token is invalid or expired.
#     404 if the user no longer exists.
#     """
#     redis = _get_rate_limit_redis()
#     user_id = await get_user_id_from_token(redis, body.token)
#
#     if not user_id:
#         raise HTTPException(
#             status_code=status.HTTP_400_BAD_REQUEST,
#             detail="Invalid or expired verification token",
#         )
#
#     user = await get_user_by_id(db, user_id)
#     if user is None:
#         raise HTTPException(
#             status_code=status.HTTP_404_NOT_FOUND,
#             detail="User not found",
#         )
#
#     # Mark user as verified
#     user.is_verified = True
#     await update_user(db, user)
#
#     # Delete the verification token (single-use)
#     await delete_verification_token(redis, body.token)
#
#     logger.info(f"Email verified for user {user.user_id}")
#
#     # Return authentication tokens (user is now logged in)
#     return TokenResponse(
#         access_token=create_access_token(user.user_id, user.email),
#         refresh_token=create_refresh_token(user.user_id),
#     )
#
#
# @router.post(
#     "/resend-verification",
#     response_model=MessageResponse,
#     status_code=status.HTTP_202_ACCEPTED,
#     summary="Resend verification email",
# )
# async def resend_verification(
#     body: ForgotPasswordRequest,  # Reuse ForgotPasswordRequest (just needs email)
#     db: AsyncIOMotorDatabase = Depends(get_database),
# ) -> MessageResponse:
#     """Resend a verification email to the user.
#
#     Always returns 202 Accepted to prevent email enumeration attacks.
#     If the email is not registered, no email is sent but the response is still 202.
#     If the email is registered and not verified, a new verification email is sent.
#
#     Rate limited to prevent abuse.
#     """
#     user = await get_user_by_email(db, body.email)
#
#     if user is None:
#         # Don't reveal whether the email exists - prevent enumeration
#         logger.info(f"Verification resend requested for non-existent email: {body.email}")
#         return MessageResponse(
#             message="If an account with this email exists, a verification email has been sent."
#         )
#
#     if user.is_verified:
#         # Don't reveal verification status to prevent enumeration
#         logger.info(f"Verification resend requested for already verified email: {body.email}")
#         return MessageResponse(
#             message="If an account with this email exists, a verification email has been sent."
#         )
#
#     # Send new verification email
#     try:
#         redis = _get_rate_limit_redis()
#         await send_verification_email(redis, user.user_id, user.email)
#     except VerificationError as exc:
#         # Log the error but don't fail the request
#         logger.warning(f"Failed to resend verification email to {user.email}: {exc}")
#
#     return MessageResponse(
#         message="If an account with this email exists, a verification email has been sent."
#     )


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Log in with email and password",
    dependencies=[Depends(_login_rate_limit)],
)
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> TokenResponse:
    """Authenticate with email + password and receive tokens.

    Returns the same 401 for both "no such user" and "wrong password" —
    deliberately vague to avoid leaking whether an email is registered.

    Implements account lockout after 5 failed login attempts.
    """
    _WRONG_CREDS = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user = await get_user_by_email(db, body.email)
    if user is None:
        # Log failed login for non-existent user (still track IP)
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        await log_failed_login("unknown", body.email, client_ip, user_agent)
        raise _WRONG_CREDS

    # Check if account is locked
    if user.locked_until and user.locked_until > datetime.now(UTC):
        remaining_time = (user.locked_until - datetime.now(UTC)).total_seconds()
        minutes_remaining = int(remaining_time / 60)
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Account is temporarily locked. Try again in {minutes_remaining} minutes.",
        )

    if not verify_password(body.password, user.hashed_password):
        # Increment failed login attempts
        user.failed_login_attempts += 1
        client_ip = request.client.host if request.client else "unknown"

        # Log to security monitor
        await log_failed_login(
            user.user_id, user.email, client_ip, request.headers.get("user-agent", "unknown")
        )

        # Lock account after 5 failed attempts
        if user.failed_login_attempts >= 5:
            user.locked_until = datetime.now(UTC) + timedelta(minutes=30)
            await log_security_event(
                event_type="ACCOUNT_LOCKED",
                severity="warning",
                user_id=user.user_id,
                ip=client_ip,
                failed_attempts=user.failed_login_attempts,
            )
            logger.warning(
                f"Account locked for user {user.user_id} after 5 failed login attempts "
                f"from IP: {client_ip}"
            )

        await update_user(db, user)
        raise _WRONG_CREDS

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated.",
        )

    # Email verification bypassed for development/testing
    # settings = get_settings()
    # if not user.is_verified and settings.environment.lower() != "development":
    #     raise HTTPException(
    #         status_code=status.HTTP_403_FORBIDDEN,
    #         detail="Please verify your email address before logging in. Check your inbox for the verification link or request a new one.",
    #     )

    # Reset failed login attempts on successful login
    if user.failed_login_attempts > 0:
        user.failed_login_attempts = 0
        user.locked_until = None
        await update_user(db, user)

    # Update last login information
    user.last_login_at = datetime.now(UTC)
    user.last_login_ip = request.client.host if request.client else "unknown"
    await update_user(db, user)

    # Log successful login to security monitor
    await log_successful_login(
        user.user_id, user.email, user.last_login_ip, request.headers.get("user-agent", "unknown")
    )

    await log_security_event(
        event_type="SUCCESSFUL_LOGIN",
        severity="info",
        user_id=user.user_id,
        ip=user.last_login_ip,
    )

    logger.info(f"Successful login for user {user.user_id} from IP: {user.last_login_ip}")

    return TokenResponse(
        access_token=create_access_token(user.user_id, user.email),
        refresh_token=create_refresh_token(user.user_id),
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new access token",
)
async def refresh_token(
    body: RefreshRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> TokenResponse:
    """Exchange a valid refresh token for a fresh access token + refresh token.

    Both tokens are rotated on every refresh. The old refresh token's JTI is
    added to a Redis blocklist so it cannot be reused — true single-use
    rotation. If Redis is unavailable the revocation is skipped (fails open)
    so a Redis outage never locks users out.

    401 if the refresh token is expired, tampered, the wrong type, or has
    already been used (JTI found in the blocklist).
    401 if the user no longer exists or is deactivated.
    """
    try:
        payload = decode_refresh_token(body.refresh_token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    jti: str = payload.get("jti", "")
    exp: int = payload.get("exp", 0)
    user_id: str = payload["sub"]

    redis = _get_optional_redis()
    if await is_refresh_token_revoked(redis, jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has already been used. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = await get_user_by_id(db, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or account deactivated.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Revoke the used token before issuing the new one
    await revoke_refresh_token(redis, jti, exp)

    return TokenResponse(
        access_token=create_access_token(user.user_id, user.email),
        refresh_token=create_refresh_token(user.user_id),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Log out and invalidate tokens",
)
async def logout(
    body: LogoutRequest,
    current_user: User = Depends(get_current_user),
) -> None:
    """Invalidate the refresh token to prevent reuse.

    Adds the refresh token's JTI to the Redis blocklist, preventing
    it from being used for future token refreshes. The client should
    discard both access and refresh tokens after calling this endpoint.

    The access token will expire naturally after its TTL (30 minutes by default).
    If immediate access token invalidation is needed, implement a separate
    access token blocklist or use shorter TTLs.

    204 No Content on success.
    401 if the refresh token is invalid or expired.
    """
    try:
        payload = decode_refresh_token(body.refresh_token)
    except AuthError as exc:
        # Still return 204 for logout - we want to invalidate the session
        # even if the token is expired/invalid, as long as the user is authenticated
        logger.warning(
            f"Invalid refresh token provided during logout for user {current_user.user_id}: {exc}"
        )
        return None

    jti: str = payload.get("jti", "")
    exp: int = payload.get("exp", 0)

    # Verify the token belongs to the current user
    if payload.get("sub") != current_user.user_id:
        logger.warning(
            f"Logout attempt with refresh token belonging to different user. "
            f"Current user: {current_user.user_id}, Token user: {payload.get('sub')}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Refresh token does not belong to current user",
        )

    # Revoke the refresh token
    redis = _get_optional_redis()
    await revoke_refresh_token(redis, jti, exp)

    logger.info(f"User {current_user.user_id} logged out successfully")
    return None


@router.post(
    "/forgot-password",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request a password reset email",
)
async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> dict[str, str]:
    """Initiate password reset by sending an email with a reset link.

    Always returns 202 Accepted to prevent email enumeration attacks.
    If the email is not registered, no email is sent but the response is still 202.
    If the email is registered, a password reset email is sent with a 15-minute expiry token.

    The reset link points to your frontend's reset page with the token as a query parameter.
    """
    user = await get_user_by_email(db, body.email)

    if user is None:
        # Don't reveal whether the email exists - prevent enumeration
        logger.info(f"Password reset requested for non-existent email: {body.email}")
        return {
            "message": "If an account with this email exists, a password reset link has been sent."
        }

    if not user.is_active:
        # Don't reveal account status to prevent enumeration
        logger.info(f"Password reset requested for inactive account: {body.email}")
        return {
            "message": "If an account with this email exists, a password reset link has been sent."
        }

    # Send password reset email using Redis-based token storage
    redis = _get_rate_limit_redis()
    reset_token: str | None = None
    try:
        reset_token = await send_password_reset_email(
            redis=redis,
            user_id=user.user_id,
            email=user.email,
        )
    except PasswordResetError as exc:
        # Log the error but don't fail the request - security best practice
        logger.error(f"Failed to send password reset email to {user.email}: {exc}")
        # In development, log the reset link so testing is possible.
        # reset_token may be None if send_password_reset_email raised before
        # returning — guard against UnboundLocalError with the default above.
        settings = get_settings()
        if settings.environment.lower() == "development" and reset_token is not None:
            reset_link = f"{settings.frontend_base_url}/reset-password.html?token={reset_token}"
            logger.warning(
                f"Email service not configured. Reset link for {user.email}: {reset_link}"
            )

    logger.info(f"Password reset email sent to {user.email}")
    return {"message": "If an account with this email exists, a password reset link has been sent."}


@router.post(
    "/reset-password",
    status_code=status.HTTP_200_OK,
    summary="Reset password with a token",
)
async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncIOMotorDatabase = Depends(get_database),
) -> dict[str, str]:
    """Reset password using a token from the forgot-password email.

    Validates the token from Redis and updates the user's password if valid.
    The token is single-use and expires after 15 minutes.

    400 if the token is invalid or expired.
    404 if the user no longer exists.
    """
    redis = _get_rate_limit_redis()

    # Validate token from Redis
    user_id = await get_user_id_from_reset_token(redis, body.token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )

    user = await get_user_by_id(db, user_id)

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Update the password
    user.hashed_password = hash_password(body.new_password)
    user.password_changed_at = datetime.now(UTC)
    user.failed_login_attempts = 0  # Reset failed login attempts
    user.locked_until = None  # Unlock account if it was locked
    await update_user(db, user)

    # Delete the reset token (single-use)
    await delete_password_reset_token(redis, body.token)

    logger.info(f"Password reset successful for user {user.user_id}")
    return {"message": "Password has been reset successfully"}


@router.get(
    "/me",
    response_model=UserPublic,
    summary="Get the current user's profile",
)
async def get_me(current_user: User = Depends(get_current_user)) -> UserPublic:
    """Return the authenticated user's public profile.

    Requires a valid access token in `Authorization: Bearer <token>`.
    """
    return UserPublic(
        user_id=current_user.user_id,
        email=current_user.email,
        role=current_user.role,
        is_active=current_user.is_active,
        is_verified=current_user.is_verified,
        mfa_enabled=current_user.mfa_enabled,
        last_login_at=current_user.last_login_at,
        last_login_ip=current_user.last_login_ip,
        created_at=current_user.created_at,
        updated_at=current_user.updated_at,
    )
