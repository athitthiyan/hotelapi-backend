from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request


RATE_LIMITS = {
    "auth:signup": (5, timedelta(minutes=10)),
    "auth:login": (8, timedelta(minutes=10)),
    "payments:create-intent": (10, timedelta(minutes=10)),
    "payments:failure": (12, timedelta(minutes=10)),
}

_REQUEST_LOG: dict[str, deque[datetime]] = defaultdict(deque)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_rate_limit_key(scope: str, request: Request, subject: str | None = None) -> str:
    client_host = request.client.host if request.client else "unknown"
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
    _REQUEST_LOG.clear()
