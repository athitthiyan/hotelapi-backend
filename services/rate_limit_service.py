from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request


RATE_LIMITS = {
    "auth:signup": (5, timedelta(minutes=10)),
    "auth:login": (8, timedelta(minutes=10)),
    "auth:social-login": (10, timedelta(minutes=10)),
    "auth:forgot-password": (5, timedelta(minutes=15)),
    "auth:phone-otp": (5, timedelta(minutes=10)),
    "auth:otp-request-15m": (5, timedelta(minutes=15)),
    "auth:otp-request-1h": (10, timedelta(hours=1)),
    "partner:register": (5, timedelta(minutes=10)),
    "partner:login": (8, timedelta(minutes=10)),
    "payments:create-intent": (10, timedelta(minutes=10)),
    "payments:failure": (12, timedelta(minutes=10)),
}

_REQUEST_LOG: dict[str, deque[datetime]] = defaultdict(deque)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _get_client_ip(request: Request) -> str:
    """Extract real client IP, supporting reverse proxies (X-Forwarded-For)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def build_rate_limit_key(scope: str, request: Request, subject: str | None = None) -> str:
    client_host = _get_client_ip(request)
    suffix = subject or client_host
    return f"{scope}:{client_host}:{suffix}"


def enforce_rate_limit(
    scope: str,
    request: Request,
    subject: str | None = None,
) -> None:
    limit, window = RATE_LIMITS[scope]
    key = build_rate_limit_key(scope, request, subject=subject)
    now = utc_now()
    cutoff = now - window
    attempts = _REQUEST_LOG[key]

    while attempts and attempts[0] <= cutoff:
        attempts.popleft()

    if len(attempts) >= limit:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please try again later.",
        )

    attempts.append(now)


def reset_rate_limits() -> None:
    """Clear all in-memory rate limit logs. Used in tests."""
    _REQUEST_LOG.clear()
