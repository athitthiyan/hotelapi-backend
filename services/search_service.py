from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import models


SEARCH_CACHE_TTL_SECONDS = 60

# TODO: Migrate in-memory search cache to Redis for distributed caching and production scalability
_search_cache: dict[str, tuple[datetime, dict[str, Any]]] = {}
_cache_lock = Lock()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def make_search_cache_key(**kwargs: Any) -> str:
    parts = [f"{key}={kwargs[key]}" for key in sorted(kwargs)]
    return "|".join(parts)


def get_cached_search(cache_key: str) -> dict[str, Any] | None:
    now = utc_now()
    with _cache_lock:
        cached = _search_cache.get(cache_key)
        if not cached:
            return None
        expires_at, payload = cached
        if expires_at <= now:
            _search_cache.pop(cache_key, None)
            return None
        return payload


def set_cached_search(cache_key: str, payload: dict[str, Any]) -> None:
    with _cache_lock:
        _search_cache[cache_key] = (
            utc_now() + timedelta(seconds=SEARCH_CACHE_TTL_SECONDS),
            payload,
        )


def clear_search_cache() -> None:
    with _cache_lock:
        _search_cache.clear()


def score_room(room: models.Room) -> float:
    featured_bonus = 20 if room.is_featured else 0
    guest_fit_bonus = room.max_guests * 2
    price_efficiency = max(0, 300 - room.price) / 20
    review_strength = min(room.review_count, 1000) / 50
    return round(
        featured_bonus + (room.rating * 10) + guest_fit_bonus + price_efficiency + review_strength,
        2,
    )


def sort_rooms(rooms: list[models.Room], sort_by: str) -> list[models.Room]:
    normalized_sort = {
        "price_low_to_high": "price_asc",
        "price_high_to_low": "price_desc",
        "top_rated": "rating_desc",
        "most_popular": "featured",
    }.get(sort_by, sort_by)

    if normalized_sort == "price_asc":
        return sorted(rooms, key=lambda room: (room.price, -room.rating, room.id))
    if normalized_sort == "price_desc":
        return sorted(rooms, key=lambda room: (-room.price, -room.rating, room.id))
    if normalized_sort == "rating_desc":
        return sorted(
            rooms,
            key=lambda room: (-room.rating, -room.review_count, room.price, room.id),
        )
    if normalized_sort == "featured":
        return sorted(
            rooms,
            key=lambda room: (
                not room.is_featured,
                -room.rating,
                room.price,
                room.id,
            ),
        )
    return sorted(
        rooms,
        key=lambda room: (
            -score_room(room),
            room.price,
            room.id,
        ),
    )
