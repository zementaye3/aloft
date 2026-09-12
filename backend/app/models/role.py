"""
Role-based access control (RBAC) models and utilities.

Implements a flexible RBAC system with:
- Predefined roles (admin, user, premium)
- Permission-based access control
- Role hierarchy and inheritance
- Granular permission checking
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """User roles with hierarchical permissions."""

    ADMIN = "admin"  # Full system access
    PREMIUM = "premium"  # Enhanced features and higher limits
    USER = "user"  # Standard user access
    GUEST = "guest"  # Limited access (for future use)


class Permission(str, Enum):
    """Granular permissions for fine-grained access control."""

    # User management
    CREATE_USER = "create_user"
    READ_USER = "read_user"
    UPDATE_USER = "update_user"
    DELETE_USER = "delete_user"
    LIST_USERS = "list_users"

    # Content management
    CREATE_CONTENT = "create_content"
    READ_CONTENT = "read_content"
    UPDATE_CONTENT = "update_content"
    DELETE_CONTENT = "delete_content"
    LIST_CONTENT = "list_content"

    # POI management
    CREATE_POI = "create_poi"
    READ_POI = "read_poi"
    UPDATE_POI = "update_poi"
    DELETE_POI = "delete_poi"
    LIST_POI = "list_poi"

    # Audio management
    CREATE_AUDIO = "create_audio"
    READ_AUDIO = "read_audio"
    DELETE_AUDIO = "delete_audio"

    # Session management
    CREATE_SESSION = "create_session"
    READ_SESSION = "read_session"
    UPDATE_SESSION = "update_session"
    DELETE_SESSION = "delete_session"

    # Route management
    CREATE_ROUTE = "create_route"
    READ_ROUTE = "read_route"
    DELETE_ROUTE = "delete_route"
    DOWNLOAD_ROUTE = "download_route"

    # Flight management
    LOOKUP_FLIGHT = "lookup_flight"

    # Admin-only permissions
    MANAGE_ROLES = "manage_roles"
    VIEW_AUDIT_LOGS = "view_audit_logs"
    MANAGE_RATE_LIMITS = "manage_rate_limits"
    VIEW_SYSTEM_METRICS = "view_system_metrics"
    MANAGE_API_KEYS = "manage_api_keys"


# Role-to-permission mapping
ROLE_PERMISSIONS: dict[Role, set[Permission]] = {
    Role.ADMIN: {
        # Full access to all permissions
        Permission.CREATE_USER,
        Permission.READ_USER,
        Permission.UPDATE_USER,
        Permission.DELETE_USER,
        Permission.LIST_USERS,
        Permission.CREATE_CONTENT,
        Permission.READ_CONTENT,
        Permission.UPDATE_CONTENT,
        Permission.DELETE_CONTENT,
        Permission.LIST_CONTENT,
        Permission.CREATE_POI,
        Permission.READ_POI,
        Permission.UPDATE_POI,
        Permission.DELETE_POI,
        Permission.LIST_POI,
        Permission.CREATE_AUDIO,
        Permission.READ_AUDIO,
        Permission.DELETE_AUDIO,
        Permission.CREATE_SESSION,
        Permission.READ_SESSION,
        Permission.UPDATE_SESSION,
        Permission.DELETE_SESSION,
        Permission.CREATE_ROUTE,
        Permission.READ_ROUTE,
        Permission.DELETE_ROUTE,
        Permission.DOWNLOAD_ROUTE,
        Permission.LOOKUP_FLIGHT,
        Permission.MANAGE_ROLES,
        Permission.VIEW_AUDIT_LOGS,
        Permission.MANAGE_RATE_LIMITS,
        Permission.VIEW_SYSTEM_METRICS,
        Permission.MANAGE_API_KEYS,
    },
    Role.PREMIUM: {
        # Enhanced user permissions
        Permission.CREATE_CONTENT,
        Permission.READ_CONTENT,
        Permission.DELETE_CONTENT,
        Permission.LIST_CONTENT,
        Permission.CREATE_POI,
        Permission.READ_POI,
        Permission.LIST_POI,
        Permission.CREATE_AUDIO,
        Permission.READ_AUDIO,
        Permission.DELETE_AUDIO,
        Permission.CREATE_SESSION,
        Permission.READ_SESSION,
        Permission.UPDATE_SESSION,
        Permission.DELETE_SESSION,
        Permission.CREATE_ROUTE,
        Permission.READ_ROUTE,
        Permission.DELETE_ROUTE,
        Permission.DOWNLOAD_ROUTE,
        Permission.LOOKUP_FLIGHT,
        # Higher rate limits (managed in config)
    },
    Role.USER: {
        # Standard user permissions
        Permission.CREATE_CONTENT,
        Permission.READ_CONTENT,
        Permission.LIST_CONTENT,
        Permission.CREATE_POI,
        Permission.READ_POI,
        Permission.LIST_POI,
        Permission.CREATE_AUDIO,
        Permission.READ_AUDIO,
        Permission.CREATE_SESSION,
        Permission.READ_SESSION,
        Permission.UPDATE_SESSION,
        Permission.DELETE_SESSION,
        Permission.CREATE_ROUTE,
        Permission.READ_ROUTE,
        Permission.DELETE_ROUTE,
        Permission.DOWNLOAD_ROUTE,
        Permission.LOOKUP_FLIGHT,
    },
    Role.GUEST: {
        # Limited guest permissions (for future use)
        Permission.READ_CONTENT,
        Permission.READ_POI,
        Permission.LIST_POI,
        Permission.READ_ROUTE,
    },
}


def has_permission(role: Role, permission: Permission) -> bool:
    """Check if a role has a specific permission."""
    return permission in ROLE_PERMISSIONS.get(role, set())


def has_any_permission(role: Role, permissions: set[Permission]) -> bool:
    """Check if a role has any of the specified permissions."""
    role_permissions = ROLE_PERMISSIONS.get(role, set())
    return bool(role_permissions & permissions)


def has_all_permissions(role: Role, permissions: set[Permission]) -> bool:
    """Check if a role has all of the specified permissions."""
    role_permissions = ROLE_PERMISSIONS.get(role, set())
    return permissions.issubset(role_permissions)


def get_role_permissions(role: Role) -> set[Permission]:
    """Get all permissions for a given role."""
    return ROLE_PERMISSIONS.get(role, set())


def is_admin(role: Role) -> bool:
    """Check if the role is admin."""
    return role == Role.ADMIN


def is_premium(role: Role) -> bool:
    """Check if the role is premium or admin."""
    return role in {Role.PREMIUM, Role.ADMIN}
