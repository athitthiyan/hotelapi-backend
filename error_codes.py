"""Structured API error codes for Stayvora booking flow."""

from typing import Optional
from fastapi import HTTPException

# ─── Error Code Constants ─────────────────────────────────────────────────────

ROOM_NOT_FOUND = "ROOM_NOT_FOUND"
ROOM_UNAVAILABLE = "ROOM_UNAVAILABLE"
ROOM_BLOCKED = "ROOM_BLOCKED"
INVALID_DATE_RANGE = "INVALID_DATE_RANGE"
CHECK_IN_PAST = "CHECK_IN_PAST"
MINIMUM_STAY = "MINIMUM_STAY"
GUEST_CAPACITY_EXCEEDED = "GUEST_CAPACITY_EXCEEDED"
BOOKING_CONFLICT = "BOOKING_CONFLICT"
HOLD_EXISTS = "HOLD_EXISTS"
HOLD_EXPIRED = "HOLD_EXPIRED"
HOLD_NOT_FOUND = "HOLD_NOT_FOUND"
AUTH_REQUIRED = "AUTH_REQUIRED"
DUPLICATE_BOOKING = "DUPLICATE_BOOKING"
PAYMENT_FAILED = "PAYMENT_FAILED"
INVALID_GUEST_COUNT = "INVALID_GUEST_COUNT"


# ─── Helper Function ─────────────────────────────────────────────────────────

def booking_error(
    status_code: int,
    code: str,
    message: str,
    field: Optional[str] = None,
) -> HTTPException:
    """Return an HTTPException whose detail is a structured dict for frontend error mapping."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "field": field},
    )
