from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.role import Role


class User(BaseModel):
    """Stored in MongoDB `users` collection.

    `hashed_password` is a bcrypt hash — never the plaintext value.
    `user_id` is the MongoDB _id as a string (set after insert).
    `role` determines access permissions via RBAC.
    """

    user_id: str = ""  # populated from MongoDB _id after insert
    email: EmailStr
    hashed_password: str
    role: Role = Role.USER
    is_active: bool = True
    is_verified: bool = False  # Email verification status
    mfa_enabled: bool = False  # Multi-factor authentication status
    mfa_secret: str | None = None  # TOTP secret for MFA
    failed_login_attempts: int = 0  # Track failed login attempts
    locked_until: datetime | None = None  # Account lockout timestamp
    last_login_at: datetime | None = None  # Last successful login
    last_login_ip: str | None = None  # IP address of last login
    password_changed_at: datetime | None = None  # Last password change
    # NOTE: password reset uses Redis-backed opaque tokens (see password_reset_service.py).
    # There are no password_reset fields on this model — that data never touches MongoDB.
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class UserPublic(BaseModel):
    """Safe subset returned to the client — never exposes hashed_password or sensitive data."""

    user_id: str
    email: EmailStr
    role: Role
    is_active: bool
    is_verified: bool
    mfa_enabled: bool
    last_login_at: datetime | None = None
    last_login_ip: str | None = None
    created_at: datetime
    updated_at: datetime


class UserCreate(BaseModel):
    """Request model for user creation (signup)."""

    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    role: Role = Role.USER  # Default to user, can be set by admin


class UserUpdate(BaseModel):
    """Request model for user updates."""

    email: EmailStr | None = None
    role: Role | None = None
    is_active: bool | None = None
    is_verified: bool | None = None
    mfa_enabled: bool | None = None


class PasswordChange(BaseModel):
    """Request model for password changes."""

    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)
