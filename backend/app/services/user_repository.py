"""
User repository — MongoDB CRUD for the `users` collection.

All functions accept a Motor `AsyncIOMotorDatabase` so they can be called
with a real DB in production and a `mongomock_motor` DB in tests without
any patching.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.models.role import Role
from app.models.user import User

logger = logging.getLogger("aloft.services.user_repository")


def _doc_to_user(doc: dict) -> User:
    """Convert a raw MongoDB document to a User model."""
    # Handle role conversion from string to enum
    role_value = doc.get("role", "USER")
    if isinstance(role_value, str):
        try:
            role = Role(role_value)
        except ValueError:
            role = Role.USER
    else:
        role = Role.USER

    return User(
        user_id=str(doc["_id"]),
        email=doc["email"],
        hashed_password=doc["hashed_password"],
        role=role,
        is_active=doc.get("is_active", True),
        is_verified=doc.get("is_verified", False),
        mfa_enabled=doc.get("mfa_enabled", False),
        mfa_secret=doc.get("mfa_secret"),
        failed_login_attempts=doc.get("failed_login_attempts", 0),
        locked_until=doc.get("locked_until"),
        last_login_at=doc.get("last_login_at"),
        last_login_ip=doc.get("last_login_ip"),
        password_changed_at=doc.get("password_changed_at"),
        created_at=doc.get("created_at", datetime.now(UTC)),
        updated_at=doc.get("updated_at", datetime.now(UTC)),
    )


async def get_user_by_email(db: AsyncIOMotorDatabase, email: str) -> User | None:
    """Return the user with this email address, or None if not found."""
    doc = await db.users.find_one({"email": email.lower()})
    return _doc_to_user(doc) if doc else None


async def get_user_by_id(db: AsyncIOMotorDatabase, user_id: str) -> User | None:
    """Return the user with this _id string, or None if not found."""
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(user_id)
    except InvalidId:
        return None
    doc = await db.users.find_one({"_id": oid})
    return _doc_to_user(doc) if doc else None


async def create_user(db: AsyncIOMotorDatabase, email: str, hashed_password: str) -> User:
    """Insert a new user and return the created User (with user_id populated).

    Raises ValueError if a user with this email already exists.

    Uses insert_one and catches DuplicateKeyError from the unique index on
    `email` rather than doing a check-then-insert. This avoids the TOCTOU
    race where two concurrent signups with the same email both pass the
    existence check before either has inserted, resulting in one of them
    hitting the unique index with an unhandled 500 instead of the intended 409.
    """
    from pymongo.errors import DuplicateKeyError

    email_lower = email.lower()
    doc = {
        "email": email_lower,
        "hashed_password": hashed_password,
        "is_active": True,
        "created_at": datetime.now(UTC),
    }
    try:
        result = await db.users.insert_one(doc)
    except DuplicateKeyError as err:
        raise ValueError(f"A user with email '{email_lower}' already exists.") from err

    logger.info("Created user %s", result.inserted_id)

    return User(
        user_id=str(result.inserted_id),
        email=email_lower,
        hashed_password=hashed_password,
        is_active=True,
        is_verified=False,
        created_at=doc["created_at"],
    )


async def update_user(db: AsyncIOMotorDatabase, user: User) -> None:
    """Update an existing user in the database.

    Updates all fields of the user document based on the User model.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(user.user_id)
    except InvalidId as err:
        raise ValueError(f"Invalid user_id: {user.user_id}") from err

    # Fields that are always written (booleans, counters, strings that are never
    # intentionally cleared).
    set_doc: dict = {
        "email": user.email,
        "hashed_password": user.hashed_password,
        "role": user.role.value if hasattr(user.role, "value") else str(user.role),
        "is_active": user.is_active,
        "is_verified": user.is_verified,
        "mfa_enabled": user.mfa_enabled,
        "failed_login_attempts": user.failed_login_attempts,
        "updated_at": datetime.now(UTC),
    }

    # Nullable fields that need explicit $unset when set to None so that
    # clearing them (e.g. unlocking an account by setting locked_until=None)
    # actually persists to MongoDB.
    _NULLABLE_FIELDS = {
        "mfa_secret": user.mfa_secret,
        "locked_until": user.locked_until,
        "last_login_at": user.last_login_at,
        "last_login_ip": user.last_login_ip,
        "password_changed_at": user.password_changed_at,
    }

    unset_doc: dict = {}
    for field, value in _NULLABLE_FIELDS.items():
        if value is None:
            unset_doc[field] = ""
        else:
            set_doc[field] = value

    mongo_update: dict = {"$set": set_doc}
    if unset_doc:
        mongo_update["$unset"] = unset_doc

    result = await db.users.update_one({"_id": oid}, mongo_update)

    if result.matched_count == 0:
        raise ValueError(f"User not found: {user.user_id}")

    logger.info("Updated user %s", user.user_id)


async def delete_user(db: AsyncIOMotorDatabase, user_id: str) -> bool:
    """Delete a user by ID.

    Returns True if the user was deleted, False if not found.
    Used by the GDPR deletion worker to purge accounts after the grace period.
    """
    from bson import ObjectId
    from bson.errors import InvalidId

    try:
        oid = ObjectId(user_id)
    except InvalidId:
        return False

    result = await db.users.delete_one({"_id": oid})
    deleted = result.deleted_count > 0
    if deleted:
        logger.warning("Deleted user %s", user_id)
    return deleted
