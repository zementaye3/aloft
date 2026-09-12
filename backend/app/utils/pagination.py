"""
Pagination utilities for list endpoints.

Implements cursor-based pagination for security and performance:
- Prevents ID guessing attacks
- Efficient for large datasets
- Consistent ordering
- No offset performance issues
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class PaginationParams(BaseModel):
    """Pagination parameters for list endpoints."""

    page: int = Field(default=1, ge=1, description="Page number (1-based)")
    page_size: int = Field(
        default=20, ge=1, le=100, description="Number of items per page (max 100)"
    )
    sort_by: str | None = Field(default=None, description="Field to sort by")
    sort_order: str = Field(
        default="desc", pattern="^(asc|desc)$", description="Sort order: asc or desc"
    )


class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated response model."""

    items: list[T]
    total: int
    page: int
    page_size: int
    total_pages: int
    has_next: bool
    has_previous: bool


def calculate_pagination(
    total: int,
    page: int,
    page_size: int,
) -> dict[str, Any]:
    """Calculate pagination metadata."""
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0
    has_next = page < total_pages
    has_previous = page > 1

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "has_next": has_next,
        "has_previous": has_previous,
    }


def get_skip_limit(page: int, page_size: int) -> tuple[int, int]:
    """Calculate skip and limit for MongoDB queries."""
    skip = (page - 1) * page_size
    limit = page_size
    return skip, limit


def build_sort_query(sort_by: str | None, sort_order: str) -> list[tuple[str, int]]:
    """Build MongoDB sort query from parameters."""
    if not sort_by:
        return [("_id", -1)]  # Default sort by _id descending

    sort_direction = 1 if sort_order == "asc" else -1
    return [(sort_by, sort_direction)]


class CursorPaginationParams(BaseModel):
    """Cursor-based pagination parameters (more secure than offset-based)."""

    cursor: str | None = Field(
        default=None, description="Cursor for next page (from previous response)"
    )
    limit: int = Field(default=20, ge=1, le=100, description="Number of items per page (max 100)")


class CursorPaginatedResponse(BaseModel, Generic[T]):
    """Cursor-based paginated response model."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool
    limit: int


def encode_cursor(cursor_value: Any) -> str:
    """Encode a cursor value to a string."""
    import base64
    import json

    if cursor_value is None:
        return ""

    cursor_data = {"value": cursor_value}
    json_str = json.dumps(cursor_data)
    encoded = base64.b64encode(json_str.encode()).decode()
    return encoded


def decode_cursor(cursor: str) -> Any:
    """Decode a cursor string to its value."""
    import base64
    import json

    if not cursor:
        return None

    try:
        decoded = base64.b64decode(cursor.encode()).decode()
        cursor_data = json.loads(decoded)
        return cursor_data.get("value")
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
