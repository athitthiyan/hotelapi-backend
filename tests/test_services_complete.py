"""
100% branch-coverage tests for all service-layer modules:
  - services/rate_limit_service.py
  - services/search_service.py
  - services/inventory_service.py
  - services/notification_service.py
  - services/audit_service.py
  - services/worker_service.py
"""

from __future__ import annotations

import json
from collections import deque
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base


# ─────────────────────────────────────────────────────────────────────────────
# Helpers / Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


@pytest.fixture()
def sqlite_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path}/svc.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def sample_room(sqlite_session):
    room = models.Room(
        hotel_name="Svc Test Hotel",
        room_type=models.RoomType.DELUXE,
        description="desc",
        price=100.0,
        availability=True,
        city="Chennai",
        country="India",
        max_guests=2,
        rating=4.5,
        review_count=50,
        is_featured=False,
    )
    sqlite_session.add(room)
    sqlite_session.commit()
    sqlite_session.refresh(room)
    return room


@pytest.fixture()
def sample_booking(sqlite_session, sample_room):
    booking = models.Booking(
        booking_ref="BK-SVC001",
        user_name="Athit",
        email="athit@example.com",
        phone="9876543210",
        room_id=sample_room.id,
        check_in=datetime(2030, 6, 1, tzinfo=timezone.utc),
        check_out=datetime(2030, 6, 3, tzinfo=timezone.utc),
        hold_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
        guests=2,
        nights=2,
        room_rate=100.0,
        taxes=24.0,
        service_fee=10.0,
        total_amount=134.0,
        status=models.BookingStatus.PENDING,
        payment_status=models.PaymentStatus.PENDING,
    )
    sqlite_session.add(booking)
    sqlite_session.commit()
    sqlite_session.refresh(booking)
    return booking


# ─────────────────────────────────────────────────────────────────────────────
# rate_limit_service
# ─────────────────────────────────────────────────────────────────────────────


class TestRateLimitService:
    def setup_method(self):
        from services.rate_limit_service import reset_rate_limits
        reset_rate_limits()

    def teardown_method(self):
        from services.rate_limit_service import reset_rate_limits
        reset_rate_limits()

    def _make_request(self, host="127.0.0.1"):
        req = MagicMock()
        req.client = MagicMock()
        req.client.host = host
        return req

    def _make_request_no_client(self):
        req = MagicMock()
        req.client = None
        return req

    def test_build_key_with_subject(self):
        from services.rate_limit_service import build_rate_limit_key
        req = self._make_request("10.0.0.1")
        key = build_rate_limit_key("auth:login", req, subject="user@example.com")
        assert "user@example.com" in key
        assert "10.0.0.1" in key
        assert "auth:login" in key

    def test_build_key_without_subject_uses_host(self):
        from services.rate_limit_service import build_rate_limit_key
        req = self._make_request("10.0.0.2")
        key = build_rate_limit_key("auth:login", req, subject=None)
        # suffix falls back to client_host
        assert key.endswith("10.0.0.2")

    def test_build_key_no_client_uses_unknown(self):
        from services.rate_limit_service import build_rate_limit_key
        req = self._make_request_no_client()
        key = build_rate_limit_key("auth:login", req, subject=None)
        assert "unknown" in key

    def test_enforce_allows_up_to_limit(self):
        from services.rate_limit_service import enforce_rate_limit, RATE_LIMITS
        req = self._make_request()
        limit, _ = RATE_LIMITS["auth:signup"]
        # Should not raise for (limit - 1) calls
        for _ in range(limit - 1):
            enforce_rate_limit("auth:signup", req, subject="ok@example.com")

    def test_enforce_blocks_over_limit(self):
        from fastapi import HTTPException
        from services.rate_limit_service import enforce_rate_limit, RATE_LIMITS
        req = self._make_request()
        limit, _ = RATE_LIMITS["auth:signup"]
        for _ in range(limit):
            try:
                enforce_rate_limit("auth:signup", req, subject="block@example.com")
            except Exception:
                pass
        with pytest.raises(HTTPException) as exc_info:
            enforce_rate_limit("auth:signup", req, subject="block@example.com")
        assert exc_info.value.status_code == 429

    def test_expired_entries_are_pruned(self):
        """Old timestamps outside the window should be removed."""
        from services.rate_limit_service import (
            _REQUEST_LOG,
            enforce_rate_limit,
            RATE_LIMITS,
            build_rate_limit_key,
        )
        req = self._make_request()
        scope = "auth:login"
        limit, window = RATE_LIMITS[scope]
        key = build_rate_limit_key(scope, req, subject="prune@example.com")

        # Pre-fill log with stale timestamps
        stale_ts = datetime.now(timezone.utc) - window - timedelta(seconds=1)
        _REQUEST_LOG[key] = deque([stale_ts] * limit)

        # Should NOT raise because stale entries are pruned
        enforce_rate_limit(scope, req, subject="prune@example.com")

    def test_reset_clears_all(self):
        from services.rate_limit_service import _REQUEST_LOG, enforce_rate_limit, reset_rate_limits
        req = self._make_request()
        enforce_rate_limit("auth:login", req, subject="clear@example.com")
        reset_rate_limits()
        assert len(_REQUEST_LOG) == 0


# ─────────────────────────────────────────────────────────────────────────────
# search_service
# ─────────────────────────────────────────────────────────────────────────────


class TestSearchService:
    def setup_method(self):
        from services.search_service import clear_search_cache
        clear_search_cache()

    def teardown_method(self):
        from services.search_service import clear_search_cache
        clear_search_cache()

    def _make_room(self, **kwargs) -> models.Room:
        defaults = dict(
            id=1,
            hotel_name="Hotel",
            room_type=models.RoomType.STANDARD,
            description="desc",
            price=200.0,
            max_guests=2,
            rating=4.0,
            review_count=100,
            is_featured=False,
            availability=True,
            city="Chennai",
            country="India",
        )
        defaults.update(kwargs)
        room = MagicMock(spec=models.Room)
        for k, v in defaults.items():
            setattr(room, k, v)
        return room

    def test_make_search_cache_key_sorts_params(self):
        from services.search_service import make_search_cache_key
        k1 = make_search_cache_key(b=2, a=1)
        k2 = make_search_cache_key(a=1, b=2)
        assert k1 == k2

    def test_cache_miss_returns_none(self):
        from services.search_service import get_cached_search
        assert get_cached_search("no-such-key") is None

    def test_cache_hit_returns_payload(self):
        from services.search_service import get_cached_search, set_cached_search
        set_cached_search("k1", {"rooms": [], "total": 0})
        result = get_cached_search("k1")
        assert result is not None
        assert result["total"] == 0

    def test_cache_expired_entry_returns_none(self):
        from services.search_service import _search_cache, get_cached_search, _cache_lock
        # manually insert already-expired entry
        with _cache_lock:
            _search_cache["expired-key"] = (
                datetime.now(timezone.utc) - timedelta(seconds=1),
                {"rooms": [], "total": 999},
            )
        result = get_cached_search("expired-key")
        assert result is None
        # Expired key must be evicted
        with _cache_lock:
            assert "expired-key" not in _search_cache

    def test_clear_cache_empties_all(self):
        from services.search_service import set_cached_search, clear_search_cache, _search_cache
        set_cached_search("x", {"total": 1})
        clear_search_cache()
        assert len(_search_cache) == 0

    def test_score_room_featured_bonus(self):
        from services.search_service import score_room
        featured = self._make_room(is_featured=True, price=100.0, rating=4.0, review_count=50, max_guests=2)
        not_featured = self._make_room(is_featured=False, price=100.0, rating=4.0, review_count=50, max_guests=2)
        assert score_room(featured) > score_room(not_featured)
        # Difference is exactly 20
        assert score_room(featured) - score_room(not_featured) == 20.0

    def test_score_room_price_efficiency_capped_at_zero(self):
        from services.search_service import score_room
        # price > 300 → efficiency = max(0, 300 - price) / 20 = 0
        expensive = self._make_room(price=500.0, rating=4.0, review_count=0, max_guests=1, is_featured=False)
        score = score_room(expensive)
        assert score >= 0

    def test_score_room_review_capped_at_1000(self):
        from services.search_service import score_room
        many_reviews = self._make_room(review_count=2000, price=100.0, rating=4.0, max_guests=2, is_featured=False)
        capped_reviews = self._make_room(review_count=1000, price=100.0, rating=4.0, max_guests=2, is_featured=False)
        assert score_room(many_reviews) == score_room(capped_reviews)

    def test_sort_price_asc(self):
        from services.search_service import sort_rooms
        rooms = [
            self._make_room(id=1, price=300.0, rating=4.0),
            self._make_room(id=2, price=100.0, rating=4.0),
            self._make_room(id=3, price=200.0, rating=4.0),
        ]
        sorted_rooms = sort_rooms(rooms, "price_asc")
        prices = [r.price for r in sorted_rooms]
        assert prices == sorted(prices)

    def test_sort_price_desc(self):
        from services.search_service import sort_rooms
        rooms = [
            self._make_room(id=1, price=100.0, rating=4.0),
            self._make_room(id=2, price=300.0, rating=4.0),
            self._make_room(id=3, price=200.0, rating=4.0),
        ]
        sorted_rooms = sort_rooms(rooms, "price_desc")
        prices = [r.price for r in sorted_rooms]
        assert prices == sorted(prices, reverse=True)

    def test_sort_rating_desc(self):
        from services.search_service import sort_rooms
        rooms = [
            self._make_room(id=1, price=100.0, rating=3.0, review_count=10),
            self._make_room(id=2, price=150.0, rating=4.8, review_count=500),
            self._make_room(id=3, price=200.0, rating=4.5, review_count=200),
        ]
        sorted_rooms = sort_rooms(rooms, "rating_desc")
        ratings = [r.rating for r in sorted_rooms]
        assert ratings[0] == max(ratings)

    def test_sort_featured(self):
        from services.search_service import sort_rooms
        rooms = [
            self._make_room(id=1, is_featured=False, price=100.0, rating=4.0),
            self._make_room(id=2, is_featured=True, price=150.0, rating=4.5),
            self._make_room(id=3, is_featured=False, price=80.0, rating=3.5),
        ]
        sorted_rooms = sort_rooms(rooms, "featured")
        # Featured room should be first
        assert sorted_rooms[0].is_featured is True

    def test_sort_recommended_default(self):
        from services.search_service import sort_rooms
        rooms = [
            self._make_room(id=1, price=100.0, rating=3.0, review_count=10, is_featured=False, max_guests=2),
            self._make_room(id=2, price=200.0, rating=4.8, review_count=1000, is_featured=True, max_guests=4),
        ]
        # No exception; result is sorted list
        result = sort_rooms(rooms, "recommended")
        assert len(result) == 2

    def test_sort_unknown_fallback_is_recommended(self):
        from services.search_service import sort_rooms
        rooms = [
            self._make_room(id=1, price=100.0, rating=3.0, review_count=10, is_featured=False, max_guests=2),
        ]
        result = sort_rooms(rooms, "unknown_sort")
        assert len(result) == 1


# ─────────────────────────────────────────────────────────────────────────────
# inventory_service
# ─────────────────────────────────────────────────────────────────────────────


class TestInventoryService:
    def test_iter_stay_dates_single_night(self):
        from services.inventory_service import iter_stay_dates
        check_in = datetime(2030, 6, 1, tzinfo=timezone.utc)
        check_out = datetime(2030, 6, 2, tzinfo=timezone.utc)
        dates = list(iter_stay_dates(check_in, check_out))
        assert dates == [date(2030, 6, 1)]

    def test_iter_stay_dates_multi_night(self):
        from services.inventory_service import iter_stay_dates
        check_in = datetime(2030, 6, 1, tzinfo=timezone.utc)
        check_out = datetime(2030, 6, 4, tzinfo=timezone.utc)
        dates = list(iter_stay_dates(check_in, check_out))
        assert dates == [date(2030, 6, 1), date(2030, 6, 2), date(2030, 6, 3)]

    def test_normalize_aware_to_naive(self):
        from services.inventory_service import normalize_datetime_for_compare
        aware = datetime(2030, 1, 1, tzinfo=timezone.utc)
        naive = datetime(2030, 1, 1)
        result = normalize_datetime_for_compare(aware, naive)
        assert result.tzinfo is None

    def test_normalize_naive_to_aware(self):
        from services.inventory_service import normalize_datetime_for_compare
        naive = datetime(2030, 1, 1)
        aware = datetime(2030, 1, 1, tzinfo=timezone.utc)
        result = normalize_datetime_for_compare(naive, aware)
        assert result.tzinfo is not None

    def test_normalize_both_same_awareness(self):
        from services.inventory_service import normalize_datetime_for_compare
        aware1 = datetime(2030, 1, 1, tzinfo=timezone.utc)
        aware2 = datetime(2030, 1, 2, tzinfo=timezone.utc)
        result = normalize_datetime_for_compare(aware1, aware2)
        assert result == aware1

    def test_get_or_create_creates_new_row(self, sqlite_session, sample_room):
        from services.inventory_service import get_or_create_inventory_row
        row = get_or_create_inventory_row(
            sqlite_session,
            room_id=sample_room.id,
            inventory_date=date(2030, 7, 1),
        )
        assert row.room_id == sample_room.id
        assert row.available_units == 1

    def test_get_or_create_returns_existing(self, sqlite_session, sample_room):
        from services.inventory_service import get_or_create_inventory_row
        row1 = get_or_create_inventory_row(
            sqlite_session, room_id=sample_room.id, inventory_date=date(2030, 7, 2)
        )
        sqlite_session.commit()
        row2 = get_or_create_inventory_row(
            sqlite_session, room_id=sample_room.id, inventory_date=date(2030, 7, 2)
        )
        assert row1.id == row2.id

    def test_release_expired_locks_releases_expired(self, sqlite_session, sample_room):
        from services.inventory_service import (
            release_expired_inventory_locks,
            get_or_create_inventory_row,
        )
        inv_date = date(2030, 8, 1)
        row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=inv_date)
        row.locked_units = 1
        row.available_units = 0
        row.locked_by_booking_id = 999
        row.lock_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)  # already expired
        row.status = models.InventoryStatus.BLOCKED
        sqlite_session.commit()

        released = release_expired_inventory_locks(sqlite_session, room_id=sample_room.id)
        assert released >= 1

    def test_release_expired_locks_skips_not_yet_expired(self, sqlite_session, sample_room):
        from services.inventory_service import (
            release_expired_inventory_locks,
            get_or_create_inventory_row,
        )
        inv_date = date(2030, 9, 1)
        row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=inv_date)
        row.locked_units = 1
        row.available_units = 0
        row.locked_by_booking_id = 999
        row.lock_expires_at = datetime(2099, 1, 1, tzinfo=timezone.utc)  # far future
        row.status = models.InventoryStatus.BLOCKED
        sqlite_session.commit()

        released = release_expired_inventory_locks(sqlite_session, room_id=sample_room.id)
        assert released == 0

    def test_release_expired_locks_filters_by_booking_id(self, sqlite_session, sample_room):
        from services.inventory_service import (
            release_expired_inventory_locks,
            get_or_create_inventory_row,
        )
        inv_date = date(2030, 10, 1)
        row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=inv_date)
        row.locked_units = 1
        row.available_units = 0
        row.locked_by_booking_id = 888
        row.lock_expires_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
        sqlite_session.commit()

        # Filter for booking 777 → nothing released
        released = release_expired_inventory_locks(sqlite_session, booking_id=777)
        assert released == 0

    def test_is_inventory_available_true(self, sqlite_session, sample_room):
        from services.inventory_service import is_inventory_available
        result = is_inventory_available(
            sqlite_session,
            room_id=sample_room.id,
            check_in=datetime(2031, 1, 1, tzinfo=timezone.utc),
            check_out=datetime(2031, 1, 3, tzinfo=timezone.utc),
        )
        assert result is True

    def test_is_inventory_available_false_when_blocked(self, sqlite_session, sample_room):
        from services.inventory_service import is_inventory_available, get_or_create_inventory_row
        check_in = datetime(2031, 2, 1, tzinfo=timezone.utc)
        check_out = datetime(2031, 2, 3, tzinfo=timezone.utc)

        # Block the first date
        row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=date(2031, 2, 1))
        row.status = models.InventoryStatus.BLOCKED
        row.available_units = 0
        sqlite_session.commit()

        result = is_inventory_available(
            sqlite_session,
            room_id=sample_room.id,
            check_in=check_in,
            check_out=check_out,
        )
        assert result is False

    def test_lock_inventory_raises_when_blocked(self, sqlite_session, sample_room, sample_booking):
        from services.inventory_service import lock_inventory_for_booking, get_or_create_inventory_row
        row = get_or_create_inventory_row(
            sqlite_session,
            room_id=sample_room.id,
            inventory_date=date(2030, 6, 1),
        )
        row.status = models.InventoryStatus.BLOCKED
        row.available_units = 0
        sqlite_session.commit()

        with pytest.raises(ValueError, match="Inventory is not available"):
            lock_inventory_for_booking(
                sqlite_session,
                booking=sample_booking,
                lock_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
            )

    def test_lock_inventory_sets_blocked_when_last_unit(self, sqlite_session, sample_room, sample_booking):
        from services.inventory_service import lock_inventory_for_booking, get_or_create_inventory_row
        for d in [date(2030, 6, 1), date(2030, 6, 2)]:
            row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=d)
            row.available_units = 1
            row.status = models.InventoryStatus.AVAILABLE
        sqlite_session.commit()

        lock_inventory_for_booking(
            sqlite_session,
            booking=sample_booking,
            lock_expires_at=datetime(2099, 6, 1, tzinfo=timezone.utc),
        )
        row = sqlite_session.query(models.RoomInventory).filter_by(
            room_id=sample_room.id,
            inventory_date=date(2030, 6, 1),
        ).first()
        # After locking 1 unit of 1 total, available=0 → BLOCKED
        assert row.status == models.InventoryStatus.BLOCKED

    def test_confirm_inventory_sets_available_or_blocked(self, sqlite_session, sample_room, sample_booking):
        from services.inventory_service import (
            lock_inventory_for_booking,
            confirm_inventory_for_booking,
            get_or_create_inventory_row,
        )
        for d in [date(2030, 6, 1), date(2030, 6, 2)]:
            row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=d)
            row.available_units = 2
            row.total_units = 2
            row.status = models.InventoryStatus.AVAILABLE
        sqlite_session.commit()

        lock_inventory_for_booking(
            sqlite_session,
            booking=sample_booking,
            lock_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        sqlite_session.commit()

        confirm_inventory_for_booking(sqlite_session, booking=sample_booking)
        sqlite_session.commit()

        row = sqlite_session.query(models.RoomInventory).filter_by(
            room_id=sample_room.id,
            inventory_date=date(2030, 6, 1),
        ).first()
        assert row.locked_by_booking_id is None
        assert row.locked_units == 0

    def test_confirm_inventory_blocked_when_available_zero(self, sqlite_session, sample_room, sample_booking):
        from services.inventory_service import confirm_inventory_for_booking, get_or_create_inventory_row
        row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=date(2030, 6, 1))
        row.available_units = 0
        row.locked_units = 1
        row.locked_by_booking_id = sample_booking.id
        sqlite_session.commit()

        confirm_inventory_for_booking(sqlite_session, booking=sample_booking)
        sqlite_session.commit()

        sqlite_session.refresh(row)
        assert row.status == models.InventoryStatus.BLOCKED

    def test_release_inventory_no_rows_returns_zero(self, sqlite_session, sample_booking):
        from services.inventory_service import release_inventory_for_booking
        count = release_inventory_for_booking(sqlite_session, booking=sample_booking)
        assert count == 0

    def test_release_inventory_updates_rows(self, sqlite_session, sample_room, sample_booking):
        from services.inventory_service import (
            release_inventory_for_booking,
            lock_inventory_for_booking,
            get_or_create_inventory_row,
        )
        for d in [date(2030, 6, 1), date(2030, 6, 2)]:
            row = get_or_create_inventory_row(sqlite_session, room_id=sample_room.id, inventory_date=d)
            row.available_units = 1
            row.total_units = 1
            row.status = models.InventoryStatus.AVAILABLE
        sqlite_session.commit()

        lock_inventory_for_booking(
            sqlite_session, booking=sample_booking,
            lock_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        )
        sqlite_session.commit()

        count = release_inventory_for_booking(sqlite_session, booking=sample_booking)
        assert count == 2  # 2 nights

    def test_upsert_inventory_range_with_explicit_available_units(self, sqlite_session, sample_room):
        from services.inventory_service import upsert_inventory_range
        rows = upsert_inventory_range(
            sqlite_session,
            room_id=sample_room.id,
            start_date=date(2031, 3, 1),
            end_date=date(2031, 3, 2),
            total_units=5,
            available_units=3,
            status=models.InventoryStatus.AVAILABLE,
        )
        assert len(rows) == 2
        assert all(r.available_units == 3 for r in rows)

    def test_upsert_inventory_range_none_available_uses_total(self, sqlite_session, sample_room):
        from services.inventory_service import upsert_inventory_range
        rows = upsert_inventory_range(
            sqlite_session,
            room_id=sample_room.id,
            start_date=date(2031, 4, 1),
            end_date=date(2031, 4, 1),
            total_units=3,
            available_units=None,  # should default to total_units
            status=models.InventoryStatus.AVAILABLE,
        )
        assert rows[0].available_units == 3


# ─────────────────────────────────────────────────────────────────────────────
# notification_service
# ─────────────────────────────────────────────────────────────────────────────


class TestNotificationService:
    def test_enqueue_creates_pending_notification(self, sqlite_session, sample_booking):
        from services.notification_service import enqueue_notification
        notif = enqueue_notification(
            sqlite_session,
            event_type="booking_hold_created",
            recipient_email="athit@example.com",
            subject="Hold created",
            body="Your hold is active",
            booking_id=sample_booking.id,
        )
        sqlite_session.commit()
        assert notif.status == models.NotificationStatus.PENDING
        assert notif.event_type == "booking_hold_created"

    def test_queue_booking_hold_email(self, sqlite_session, sample_booking):
        from services.notification_service import queue_booking_hold_email
        notif = queue_booking_hold_email(sqlite_session, sample_booking)
        sqlite_session.commit()
        assert notif.event_type == "booking_hold_created"
        assert sample_booking.booking_ref in notif.subject

    def test_queue_booking_confirmation_email(self, sqlite_session, sample_booking):
        from services.notification_service import queue_booking_confirmation_email
        txn = models.Transaction(
            booking_id=sample_booking.id,
            transaction_ref="TXN-TEST001",
            amount=134.0,
            currency="USD",
            payment_method="mock",
            status=models.TransactionStatus.SUCCESS,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()
        sqlite_session.refresh(txn)

        notif = queue_booking_confirmation_email(sqlite_session, sample_booking, txn)
        sqlite_session.commit()
        assert notif.event_type == "booking_confirmed"
        assert notif.transaction_id == txn.id
        # Must contain invoice PDF attachment
        assert notif.attachment_pdf is not None
        assert len(notif.attachment_pdf) > 0
        assert notif.attachment_pdf[:5] == b"%PDF-"
        assert notif.attachment_filename is not None
        assert notif.attachment_filename.startswith("INV-")
        assert notif.attachment_filename.endswith(".pdf")

    def test_queue_payment_receipt_email(self, sqlite_session, sample_booking):
        from services.notification_service import queue_payment_receipt_email
        txn = models.Transaction(
            booking_id=sample_booking.id,
            transaction_ref="TXN-TEST002",
            amount=134.0,
            currency="USD",
            payment_method="mock",
            status=models.TransactionStatus.SUCCESS,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()
        sqlite_session.refresh(txn)

        notif = queue_payment_receipt_email(sqlite_session, sample_booking, txn)
        sqlite_session.commit()
        assert notif.event_type == "payment_receipt"

    def test_queue_payment_failure_email(self, sqlite_session, sample_booking):
        from services.notification_service import queue_payment_failure_email
        txn = models.Transaction(
            booking_id=sample_booking.id,
            transaction_ref="TXN-TEST003",
            amount=134.0,
            currency="USD",
            payment_method="mock",
            status=models.TransactionStatus.FAILED,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()
        sqlite_session.refresh(txn)

        notif = queue_payment_failure_email(sqlite_session, sample_booking, txn, "Card declined")
        sqlite_session.commit()
        assert notif.event_type == "payment_failed_retry"
        assert "Card declined" in notif.body

    def test_queue_booking_cancellation_email(self, sqlite_session, sample_booking):
        from services.notification_service import queue_booking_cancellation_email
        notif = queue_booking_cancellation_email(sqlite_session, sample_booking)
        sqlite_session.commit()
        assert notif.event_type == "booking_cancelled"
        assert sample_booking.booking_ref in notif.subject

    @patch("services.notification_service.get_settings")
    def test_deliver_notification_success(self, mock_get_settings):
        from services.notification_service import deliver_notification
        mock_settings = MagicMock()
        mock_settings.resend_api_key = ""
        mock_get_settings.return_value = mock_settings
        notif = MagicMock()
        notif.recipient_email = "valid@example.com"
        # Must not raise — skips delivery when no API key
        deliver_notification(notif)

    def test_deliver_notification_fails_for_test_domain(self):
        from services.notification_service import deliver_notification
        notif = MagicMock()
        notif.recipient_email = "fail-delivery+test@example.com"
        with pytest.raises(ValueError, match="rejected"):
            deliver_notification(notif)

    def test_process_pending_empty(self, sqlite_session):
        from services.notification_service import process_pending_notifications
        result = process_pending_notifications(sqlite_session, limit=10)
        assert result == {"sent": 0, "failed": 0, "total": 0}

    def test_process_pending_all_sent(self, sqlite_session, sample_booking):
        from services.notification_service import (
            process_pending_notifications,
            queue_booking_hold_email,
        )
        queue_booking_hold_email(sqlite_session, sample_booking)
        sqlite_session.commit()

        result = process_pending_notifications(sqlite_session, limit=10)
        assert result["sent"] == 1
        assert result["failed"] == 0

    def test_process_pending_some_failed(self, sqlite_session, sample_booking):
        from services.notification_service import process_pending_notifications

        # A notification that will fail delivery
        fail_notif = models.NotificationOutbox(
            booking_id=sample_booking.id,
            event_type="booking_hold_created",
            recipient_email="fail-delivery+test@example.com",
            subject="Hold",
            body="Body",
            status=models.NotificationStatus.PENDING,
        )
        sqlite_session.add(fail_notif)
        # A notification that will succeed
        ok_notif = models.NotificationOutbox(
            booking_id=sample_booking.id,
            event_type="booking_hold_created",
            recipient_email="ok@example.com",
            subject="Hold",
            body="Body",
            status=models.NotificationStatus.PENDING,
        )
        sqlite_session.add(ok_notif)
        sqlite_session.commit()

        result = process_pending_notifications(sqlite_session, limit=10)
        assert result["sent"] == 1
        assert result["failed"] == 1

    def test_process_pending_sets_failure_reason(self, sqlite_session):
        from services.notification_service import process_pending_notifications
        fail_notif = models.NotificationOutbox(
            event_type="test",
            recipient_email="fail-delivery+x@example.com",
            subject="Fail",
            body="Body",
            status=models.NotificationStatus.PENDING,
        )
        sqlite_session.add(fail_notif)
        sqlite_session.commit()

        process_pending_notifications(sqlite_session, limit=10)
        sqlite_session.refresh(fail_notif)
        assert fail_notif.failure_reason is not None
        assert fail_notif.status == models.NotificationStatus.FAILED


# ─────────────────────────────────────────────────────────────────────────────
# document_service
# ─────────────────────────────────────────────────────────────────────────────


class TestDocumentService:
    def test_invoice_number_format(self, sample_booking):
        from services.document_service import invoice_number_for_booking
        inv_no = invoice_number_for_booking(sample_booking)
        assert inv_no == f"INV-{sample_booking.booking_ref}"

    def test_build_invoice_pdf_returns_valid_pdf(self, sqlite_session, sample_booking):
        from services.document_service import build_invoice_pdf
        pdf = build_invoice_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert len(pdf) > 100
        assert pdf[:5] == b"%PDF-"

    def test_build_invoice_pdf_with_refund_status(self, sqlite_session, sample_booking):
        from services.document_service import build_invoice_pdf
        sample_booking.payment_status = models.PaymentStatus.REFUNDED
        sqlite_session.commit()
        pdf = build_invoice_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert pdf[:5] == b"%PDF-"

    def test_build_invoice_pdf_no_room(self, sqlite_session, sample_booking):
        from services.document_service import build_invoice_pdf
        sample_booking.room = None
        pdf = build_invoice_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert pdf[:5] == b"%PDF-"

    def test_build_invoice_pdf_with_partner_hotel(self, sqlite_session, sample_room, sample_booking):
        from services.document_service import build_invoice_pdf
        user = models.User(
            email="partner-doc@example.com",
            full_name="Doc Partner",
            hashed_password="x",
            is_partner=True,
            is_active=True,
        )
        sqlite_session.add(user)
        sqlite_session.commit()
        sqlite_session.refresh(user)

        hotel = models.PartnerHotel(
            owner_user_id=user.id,
            legal_name="Doc Hotel Pvt Ltd",
            display_name="Doc Hotel",
            support_email="doc@hotel.com",
            address_line="123 Test St",
            city="Mumbai",
            country="India",
            gst_number="22ABCDE1234F1Z5",
        )
        sqlite_session.add(hotel)
        sqlite_session.commit()
        sqlite_session.refresh(hotel)

        sample_room.partner_hotel_id = hotel.id
        sqlite_session.commit()
        sqlite_session.refresh(sample_booking)

        pdf = build_invoice_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert pdf[:5] == b"%PDF-"

    def test_build_voucher_pdf_returns_valid_pdf(self, sqlite_session, sample_booking):
        from services.document_service import build_voucher_pdf
        pdf = build_voucher_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert len(pdf) > 100
        assert pdf[:5] == b"%PDF-"

    def test_build_voucher_pdf_no_room(self, sqlite_session, sample_booking):
        from services.document_service import build_voucher_pdf
        sample_booking.room = None
        pdf = build_voucher_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert pdf[:5] == b"%PDF-"

    def test_build_voucher_pdf_with_special_requests(self, sqlite_session, sample_booking):
        from services.document_service import build_voucher_pdf
        sample_booking.special_requests = "Extra pillows please"
        sqlite_session.commit()
        pdf = build_voucher_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert pdf[:5] == b"%PDF-"

    def test_build_voucher_pdf_with_partner_hotel(self, sqlite_session, sample_room, sample_booking):
        from services.document_service import build_voucher_pdf
        user = models.User(
            email="partner-voucher@example.com",
            full_name="Voucher Partner",
            hashed_password="x",
            is_partner=True,
            is_active=True,
        )
        sqlite_session.add(user)
        sqlite_session.commit()
        sqlite_session.refresh(user)

        hotel = models.PartnerHotel(
            owner_user_id=user.id,
            legal_name="Voucher Hotel Pvt Ltd",
            display_name="Voucher Hotel",
            support_email="voucher@hotel.com",
            support_phone="+919876543210",
            address_line="456 Test Ave",
            city="Delhi",
            country="India",
            check_in_time="15:00",
            check_out_time="12:00",
        )
        sqlite_session.add(hotel)
        sqlite_session.commit()
        sqlite_session.refresh(hotel)

        sample_room.partner_hotel_id = hotel.id
        sqlite_session.commit()
        sqlite_session.refresh(sample_booking)

        pdf = build_voucher_pdf(sample_booking)
        assert isinstance(pdf, bytes)
        assert pdf[:5] == b"%PDF-"

    def test_safe_date_none(self):
        from services.document_service import _safe_date
        assert _safe_date(None) == "N/A"

    def test_safe_date_string(self):
        from services.document_service import _safe_date
        assert _safe_date("2030-06-01") == "2030-06-01"

    def test_safe_date_datetime(self):
        from services.document_service import _safe_date
        dt = datetime(2030, 6, 1, 14, 0, tzinfo=timezone.utc)
        assert _safe_date(dt) == "2030-06-01"

    def test_safe_currency_valid(self):
        from services.document_service import _safe_currency
        assert _safe_currency(1234.5) == "INR 1,234.50"

    def test_safe_currency_none(self):
        from services.document_service import _safe_currency
        assert _safe_currency(None) == "INR 0.00"

    def test_safe_currency_invalid(self):
        from services.document_service import _safe_currency
        assert _safe_currency("not-a-number") == "INR 0.00"

    def test_safe_currency_custom_symbol(self):
        from services.document_service import _safe_currency
        assert _safe_currency(100.0, symbol="USD") == "USD 100.00"


# ─────────────────────────────────────────────────────────────────────────────
# notification_service — PDF attachment tests
# ─────────────────────────────────────────────────────────────────────────────


class TestNotificationAttachment:
    def test_enqueue_with_attachment(self, sqlite_session, sample_booking):
        from services.notification_service import enqueue_notification
        pdf_data = b"%PDF-1.4 fake pdf content"
        notif = enqueue_notification(
            sqlite_session,
            event_type="test_attachment",
            recipient_email="test@example.com",
            subject="Test",
            body="Body",
            booking_id=sample_booking.id,
            attachment_pdf=pdf_data,
            attachment_filename="test-invoice.pdf",
        )
        sqlite_session.commit()
        sqlite_session.refresh(notif)
        assert notif.attachment_pdf == pdf_data
        assert notif.attachment_filename == "test-invoice.pdf"

    def test_enqueue_without_attachment(self, sqlite_session, sample_booking):
        from services.notification_service import enqueue_notification
        notif = enqueue_notification(
            sqlite_session,
            event_type="test_no_attachment",
            recipient_email="test@example.com",
            subject="Test",
            body="Body",
            booking_id=sample_booking.id,
        )
        sqlite_session.commit()
        assert notif.attachment_pdf is None
        assert notif.attachment_filename is None

    def test_confirmation_email_has_invoice_attached(self, sqlite_session, sample_booking):
        from services.notification_service import queue_booking_confirmation_email
        txn = models.Transaction(
            booking_id=sample_booking.id,
            transaction_ref="TXN-ATTACH001",
            amount=134.0,
            currency="USD",
            payment_method="mock",
            status=models.TransactionStatus.SUCCESS,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()
        sqlite_session.refresh(txn)

        notif = queue_booking_confirmation_email(sqlite_session, sample_booking, txn)
        sqlite_session.commit()

        assert notif.attachment_pdf is not None
        assert notif.attachment_pdf[:5] == b"%PDF-"
        assert notif.attachment_filename == f"INV-{sample_booking.booking_ref}.pdf"
        assert "invoice" in notif.body.lower() or "Invoice" in notif.body

    def test_other_emails_have_no_attachment(self, sqlite_session, sample_booking):
        from services.notification_service import (
            queue_booking_hold_email,
            queue_booking_cancellation_email,
            queue_refund_initiated_email,
            queue_refund_success_email,
            queue_refund_failure_email,
        )
        sample_booking.refund_amount = 100.0
        sample_booking.refund_failed_reason = "test"
        sqlite_session.commit()

        for fn in [
            lambda: queue_booking_hold_email(sqlite_session, sample_booking),
            lambda: queue_booking_cancellation_email(sqlite_session, sample_booking),
            lambda: queue_refund_initiated_email(sqlite_session, sample_booking),
            lambda: queue_refund_success_email(sqlite_session, sample_booking),
            lambda: queue_refund_failure_email(sqlite_session, sample_booking),
        ]:
            notif = fn()
            sqlite_session.commit()
            assert notif.attachment_pdf is None
            assert notif.attachment_filename is None

    def test_resend_params_include_attachment(self):
        """Verify _send_via_resend builds correct params with attachment."""
        import base64
        from unittest.mock import patch, MagicMock

        from services.notification_service import _send_via_resend

        notif = MagicMock()
        notif.recipient_email = "user@example.com"
        notif.subject = "Test"
        notif.body = "Body"
        notif.attachment_pdf = b"%PDF-1.4 test"
        notif.attachment_filename = "invoice.pdf"

        mock_resend = MagicMock()
        with patch.dict("sys.modules", {"resend": mock_resend}):
            _send_via_resend(notif, "test-api-key", "Stayvora <noreply@test.com>")

        call_args = mock_resend.Emails.send.call_args
        params = call_args[0][0]
        assert "attachments" in params
        assert len(params["attachments"]) == 1
        att = params["attachments"][0]
        assert att["filename"] == "invoice.pdf"
        assert att["content_type"] == "application/pdf"
        # Verify base64 content
        decoded = base64.b64decode(att["content"])
        assert decoded == b"%PDF-1.4 test"

    def test_resend_params_no_attachment(self):
        """Verify _send_via_resend does NOT include attachments key when none."""
        from unittest.mock import patch, MagicMock

        from services.notification_service import _send_via_resend

        notif = MagicMock()
        notif.recipient_email = "user@example.com"
        notif.subject = "Test"
        notif.body = "Body"
        notif.attachment_pdf = None
        notif.attachment_filename = None

        mock_resend = MagicMock()
        with patch.dict("sys.modules", {"resend": mock_resend}):
            _send_via_resend(notif, "test-api-key", "Stayvora <noreply@test.com>")

        call_args = mock_resend.Emails.send.call_args
        params = call_args[0][0]
        assert "attachments" not in params


# ─────────────────────────────────────────────────────────────────────────────
# audit_service
# ─────────────────────────────────────────────────────────────────────────────


class TestAuditService:
    def test_write_audit_log_with_metadata(self, sqlite_session):
        from services.audit_service import write_audit_log
        log = write_audit_log(
            sqlite_session,
            actor_user_id=1,
            action="payment.refund",
            entity_type="booking",
            entity_id=42,
            metadata={"reason": "Customer request", "amount": 134.0},
        )
        sqlite_session.commit()
        sqlite_session.refresh(log)
        assert log.action == "payment.refund"
        assert log.entity_id == "42"
        parsed = json.loads(log.metadata_json)
        assert parsed["reason"] == "Customer request"

    def test_write_audit_log_without_metadata(self, sqlite_session):
        from services.audit_service import write_audit_log
        log = write_audit_log(
            sqlite_session,
            actor_user_id=None,
            action="system.startup",
            entity_type="system",
            entity_id="boot",
        )
        sqlite_session.commit()
        sqlite_session.refresh(log)
        assert log.metadata_json == "{}"
        assert log.actor_user_id is None

    def test_write_audit_log_entity_id_string(self, sqlite_session):
        from services.audit_service import write_audit_log
        log = write_audit_log(
            sqlite_session,
            actor_user_id=5,
            action="room.delete",
            entity_type="room",
            entity_id="stuck-attempts",
        )
        sqlite_session.commit()
        assert log.entity_id == "stuck-attempts"


# ─────────────────────────────────────────────────────────────────────────────
# worker_service
# ─────────────────────────────────────────────────────────────────────────────


class TestWorkerService:
    def test_run_maintenance_cycle_no_work(self, sqlite_session):
        from services.worker_service import run_maintenance_cycle
        result = run_maintenance_cycle(sqlite_session, payment_timeout_minutes=30, notification_limit=10)
        assert result["reconciled_payments"] == 0
        assert result["processed_notifications"] == 0
        assert result["sent_notifications"] == 0
        assert result["failed_notifications"] == 0

    def test_run_maintenance_cycle_processes_stuck_payments_and_notifications(
        self, sqlite_session, sample_room
    ):
        from services.worker_service import run_maintenance_cycle

        # Create a PROCESSING booking
        booking = models.Booking(
            booking_ref="BK-WORKER01",
            user_name="Worker",
            email="worker@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2025, 1, 1, tzinfo=timezone.utc),
            check_out=datetime(2025, 1, 3, tzinfo=timezone.utc),
            hold_expires_at=datetime(2025, 1, 1, 0, 15, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        sqlite_session.add(booking)
        sqlite_session.commit()
        sqlite_session.refresh(booking)

        # Create a stuck PROCESSING transaction (created > 30 min ago)
        txn = models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-STUCK001",
            amount=117.0,
            currency="USD",
            payment_method="card",
            status=models.TransactionStatus.PROCESSING,
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        sqlite_session.add(txn)

        # A pending notification
        notif = models.NotificationOutbox(
            booking_id=booking.id,
            event_type="booking_hold_created",
            recipient_email="worker@example.com",
            subject="Hold",
            body="Body",
            status=models.NotificationStatus.PENDING,
        )
        sqlite_session.add(notif)
        sqlite_session.commit()

        result = run_maintenance_cycle(sqlite_session, payment_timeout_minutes=1, notification_limit=10)
        assert result["reconciled_payments"] >= 1
        assert result["processed_notifications"] >= 1

    def test_get_operational_counts_empty(self, sqlite_session):
        from services.worker_service import get_operational_counts
        counts = get_operational_counts(sqlite_session)
        assert counts["pending_notifications"] == 0
        assert counts["processing_payments"] == 0

    def test_get_operational_counts_with_data(self, sqlite_session, sample_room):
        from services.worker_service import get_operational_counts

        booking = models.Booking(
            booking_ref="BK-COUNT01",
            user_name="Count",
            email="count@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2025, 2, 1, tzinfo=timezone.utc),
            check_out=datetime(2025, 2, 2, tzinfo=timezone.utc),
            hold_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
            guests=1,
            nights=1,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        sqlite_session.add(booking)
        sqlite_session.commit()
        sqlite_session.refresh(booking)

        txn = models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-COUNT01",
            amount=117.0,
            currency="USD",
            payment_method="mock",
            status=models.TransactionStatus.PENDING,
        )
        notif = models.NotificationOutbox(
            booking_id=booking.id,
            event_type="test",
            recipient_email="count@example.com",
            subject="Test",
            body="Body",
            status=models.NotificationStatus.PENDING,
        )
        sqlite_session.add(txn)
        sqlite_session.add(notif)
        sqlite_session.commit()

        counts = get_operational_counts(sqlite_session)
        assert counts["pending_notifications"] == 1
        assert counts["processing_payments"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# payment_state_service
# ─────────────────────────────────────────────────────────────────────────────


class TestPaymentStateService:
    # -- _get_card_details: non-dict payment_intent (line 74) ----------------

    def test_get_card_details_non_dict_payment_intent(self):
        from services.payment_state_service import _get_card_details

        class FakePI:
            charges = {"data": [{"payment_method_details": {"card": {"last4": "4242", "brand": "visa"}}}]}

        last4, brand = _get_card_details(FakePI())
        assert last4 == "4242"
        assert brand == "visa"

    # -- _get_card_details: no charges (line 76) -----------------------------

    def test_get_card_details_no_charges(self):
        from services.payment_state_service import _get_card_details

        last4, brand = _get_card_details({"charges": {"data": []}})
        assert last4 is None
        assert brand is None

    def test_get_card_details_non_dict_no_charges(self):
        from services.payment_state_service import _get_card_details

        class FakePI:
            charges = {"data": []}

        last4, brand = _get_card_details(FakePI())
        assert last4 is None
        assert brand is None

    # -- verify_card_payment_intent_succeeded: early return (line 88) --------

    def test_verify_card_early_return_no_intent_id(self):
        from services.payment_state_service import verify_card_payment_intent_succeeded

        ok, last4, brand = verify_card_payment_intent_succeeded(None)
        assert ok is False

    def test_verify_card_early_return_no_stripe_key(self, monkeypatch):
        from services.payment_state_service import verify_card_payment_intent_succeeded
        from database import settings as db_settings

        monkeypatch.setattr(db_settings, "stripe_secret_key", "")
        ok, last4, brand = verify_card_payment_intent_succeeded("pi_test")
        assert ok is False

    # -- verify_card_payment_intent_succeeded: exception handling (lines 95-96)

    def test_verify_card_stripe_exception(self, monkeypatch):
        from services.payment_state_service import verify_card_payment_intent_succeeded
        from database import settings as db_settings
        import stripe

        monkeypatch.setattr(db_settings, "stripe_secret_key", "sk_test")
        monkeypatch.setattr(stripe.PaymentIntent, "retrieve", lambda *a, **kw: (_ for _ in ()).throw(Exception("boom")))
        ok, last4, brand = verify_card_payment_intent_succeeded("pi_test", attempts=1)
        assert ok is False
        assert last4 is None

    # -- verify_card_payment_intent_succeeded: retry delay (lines 108-111) ---

    def test_verify_card_retry_delay(self, monkeypatch):
        from services.payment_state_service import verify_card_payment_intent_succeeded
        from database import settings as db_settings
        import stripe
        import time

        monkeypatch.setattr(db_settings, "stripe_secret_key", "sk_test")
        call_count = {"n": 0}
        slept = {"total": 0.0}

        def fake_retrieve(*a, **kw):
            call_count["n"] += 1
            return {"status": "processing"}

        def fake_sleep(secs):
            slept["total"] += secs

        monkeypatch.setattr(stripe.PaymentIntent, "retrieve", fake_retrieve)
        monkeypatch.setattr(time, "sleep", fake_sleep)

        ok, _, _ = verify_card_payment_intent_succeeded("pi_test", attempts=3, delay_seconds=0.5)
        assert ok is False
        assert call_count["n"] == 3
        assert slept["total"] == pytest.approx(1.0)

    # -- reconcile_gateway_payment_state: None booking (line 122) ------------

    def test_reconcile_gateway_none_booking(self, sqlite_session):
        from services.payment_state_service import reconcile_gateway_payment_state

        assert reconcile_gateway_payment_state(sqlite_session, None) is False

    # -- reconcile_gateway_payment_state: not confirmed returns False (line 139)

    def test_reconcile_gateway_not_confirmed(self, sqlite_session, sample_room, monkeypatch):
        from services.payment_state_service import reconcile_gateway_payment_state

        booking = models.Booking(
            booking_ref="BK-PSGATE01",
            user_name="GateUser",
            email="gate@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2030, 6, 1, tzinfo=timezone.utc),
            check_out=datetime(2030, 6, 3, tzinfo=timezone.utc),
            hold_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        sqlite_session.add(booking)
        sqlite_session.commit()
        sqlite_session.refresh(booking)

        txn = models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-PSGATE01",
            stripe_payment_intent_id="pi_gate01",
            amount=117.0,
            currency="INR",
            payment_method="card",
            status=models.TransactionStatus.PROCESSING,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()

        # Stripe says not succeeded
        monkeypatch.setattr(
            "services.payment_state_service.verify_card_payment_intent_succeeded",
            lambda *a, **kw: (False, None, None),
        )
        result = reconcile_gateway_payment_state(sqlite_session, booking)
        assert result is False

    # -- reconcile_gateway_payment_state: full success path (lines 147-170) --

    def test_reconcile_gateway_full_success(self, sqlite_session, sample_room, monkeypatch):
        from services.payment_state_service import reconcile_gateway_payment_state

        booking = models.Booking(
            booking_ref="BK-PSGATE02",
            user_name="GateUser2",
            email="gate2@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2030, 6, 1, tzinfo=timezone.utc),
            check_out=datetime(2030, 6, 3, tzinfo=timezone.utc),
            hold_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        sqlite_session.add(booking)
        sqlite_session.commit()
        sqlite_session.refresh(booking)

        txn = models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-PSGATE02",
            stripe_payment_intent_id="pi_gate02",
            amount=117.0,
            currency="INR",
            payment_method="card",
            status=models.TransactionStatus.PROCESSING,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()
        sqlite_session.refresh(txn)

        monkeypatch.setattr(
            "services.payment_state_service.verify_card_payment_intent_succeeded",
            lambda *a, **kw: (True, "1234", "mastercard"),
        )
        monkeypatch.setattr(
            "services.payment_state_service.confirm_inventory_for_booking",
            lambda db, booking: None,
        )

        result = reconcile_gateway_payment_state(sqlite_session, booking)
        assert result is True
        assert booking.payment_status == models.PaymentStatus.PAID
        assert booking.status == models.BookingStatus.CONFIRMED
        assert txn.status == models.TransactionStatus.SUCCESS

    # -- reconcile_gateway: notification dedup (lines 155-170) ---------------

    def test_reconcile_gateway_skips_duplicate_notifications(self, sqlite_session, sample_room, monkeypatch):
        from services.payment_state_service import reconcile_gateway_payment_state

        booking = models.Booking(
            booking_ref="BK-PSGATE03",
            user_name="GateUser3",
            email="gate3@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2030, 6, 1, tzinfo=timezone.utc),
            check_out=datetime(2030, 6, 3, tzinfo=timezone.utc),
            hold_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        sqlite_session.add(booking)
        sqlite_session.commit()
        sqlite_session.refresh(booking)

        txn = models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-PSGATE03",
            stripe_payment_intent_id="pi_gate03",
            amount=117.0,
            currency="INR",
            payment_method="card",
            status=models.TransactionStatus.PROCESSING,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()
        sqlite_session.refresh(txn)

        # Pre-insert both notification outbox entries so dedup skips them
        for etype in ("booking_confirmed", "payment_receipt"):
            sqlite_session.add(models.NotificationOutbox(
                booking_id=booking.id,
                transaction_id=txn.id,
                event_type=etype,
                recipient_email="gate3@example.com",
                subject="Already sent",
                body="body",
            ))
        sqlite_session.commit()

        monkeypatch.setattr(
            "services.payment_state_service.verify_card_payment_intent_succeeded",
            lambda *a, **kw: (True, "5678", "amex"),
        )
        monkeypatch.setattr(
            "services.payment_state_service.confirm_inventory_for_booking",
            lambda db, booking: None,
        )
        queued_calls = {"confirm": 0, "receipt": 0}
        monkeypatch.setattr(
            "services.payment_state_service.queue_booking_confirmation_email",
            lambda *a, **kw: queued_calls.__setitem__("confirm", queued_calls["confirm"] + 1),
        )
        monkeypatch.setattr(
            "services.payment_state_service.queue_payment_receipt_email",
            lambda *a, **kw: queued_calls.__setitem__("receipt", queued_calls["receipt"] + 1),
        )

        result = reconcile_gateway_payment_state(sqlite_session, booking)
        assert result is True
        # Notifications should NOT have been queued again
        assert queued_calls["confirm"] == 0
        assert queued_calls["receipt"] == 0

    # -- reconcile_gateway: already PAID/CONFIRMED (lines 147-154 else) ------

    def test_reconcile_gateway_already_paid_confirmed(self, sqlite_session, sample_room, monkeypatch):
        from services.payment_state_service import reconcile_gateway_payment_state

        booking = models.Booking(
            booking_ref="BK-PSGATE04",
            user_name="GateUser4",
            email="gate4@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2030, 6, 1, tzinfo=timezone.utc),
            check_out=datetime(2030, 6, 3, tzinfo=timezone.utc),
            hold_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        sqlite_session.add(booking)
        sqlite_session.commit()
        sqlite_session.refresh(booking)

        txn = models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-PSGATE04",
            stripe_payment_intent_id="pi_gate04",
            amount=117.0,
            currency="INR",
            payment_method="card",
            status=models.TransactionStatus.PROCESSING,
        )
        sqlite_session.add(txn)
        sqlite_session.commit()
        sqlite_session.refresh(txn)

        monkeypatch.setattr(
            "services.payment_state_service.verify_card_payment_intent_succeeded",
            lambda *a, **kw: (True, None, None),
        )
        monkeypatch.setattr(
            "services.payment_state_service.confirm_inventory_for_booking",
            lambda db, booking: None,
        )
        monkeypatch.setattr(
            "services.payment_state_service.queue_booking_confirmation_email",
            lambda *a, **kw: None,
        )
        monkeypatch.setattr(
            "services.payment_state_service.queue_payment_receipt_email",
            lambda *a, **kw: None,
        )

        result = reconcile_gateway_payment_state(sqlite_session, booking)
        # changed is False but txn.status == SUCCESS so result is True
        assert result is True
        # Status was not changed
        assert booking.status == models.BookingStatus.CONFIRMED
        assert booking.payment_status == models.PaymentStatus.PAID

    # -- derive_booking_lifecycle_state: None booking (line 178) -------------

    def test_derive_lifecycle_none_booking(self):
        from services.payment_state_service import derive_booking_lifecycle_state

        assert derive_booking_lifecycle_state(None) is None

    # -- derive_booking_lifecycle_state: SUCCESS transaction (line 195) ------

    def test_derive_lifecycle_payment_success(self, sqlite_session, sample_room):
        from services.payment_state_service import derive_booking_lifecycle_state

        booking = models.Booking(
            booking_ref="BK-PSLC01",
            user_name="LCUser",
            email="lc@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2030, 6, 1, tzinfo=timezone.utc),
            check_out=datetime(2030, 6, 3, tzinfo=timezone.utc),
            hold_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        txn = models.Transaction(
            booking_id=1,
            transaction_ref="TXN-LC01",
            amount=117.0,
            currency="INR",
            payment_method="card",
            status=models.TransactionStatus.SUCCESS,
        )
        state = derive_booking_lifecycle_state(booking, txn)
        assert state == "PAYMENT_SUCCESS"

    # -- derive_booking_lifecycle_state: PENDING/PROCESSING txn (lines 206-211)

    def test_derive_lifecycle_payment_pending_via_transaction(self, sqlite_session, sample_room):
        from services.payment_state_service import derive_booking_lifecycle_state

        booking = models.Booking(
            booking_ref="BK-PSLC02",
            user_name="LCUser2",
            email="lc2@example.com",
            phone="1234567890",
            room_id=sample_room.id,
            check_in=datetime(2030, 6, 1, tzinfo=timezone.utc),
            check_out=datetime(2030, 6, 3, tzinfo=timezone.utc),
            hold_expires_at=datetime(2030, 6, 1, 0, 15, tzinfo=timezone.utc),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        txn = models.Transaction(
            booking_id=1,
            transaction_ref="TXN-LC02",
            amount=117.0,
            currency="INR",
            payment_method="card",
            status=models.TransactionStatus.PENDING,
        )
        state = derive_booking_lifecycle_state(booking, txn)
        assert state == "PAYMENT_PENDING"

    def test_derive_lifecycle_payment_pending_no_transaction(self):
        from services.payment_state_service import derive_booking_lifecycle_state

        booking = MagicMock()
        booking.status = models.BookingStatus.PROCESSING
        booking.payment_status = models.PaymentStatus.PROCESSING
        state = derive_booking_lifecycle_state(booking, None)
        assert state == "PAYMENT_PENDING"

    # -- attach_booking_lifecycle_state: None booking (line 220) -------------

    def test_attach_lifecycle_none_booking(self, sqlite_session):
        from services.payment_state_service import attach_booking_lifecycle_state

        assert attach_booking_lifecycle_state(sqlite_session, None) is None

    # -- reconcile_booking_payment_state: None booking (line 249) ------------

    def test_reconcile_booking_payment_none_booking(self, sqlite_session):
        from services.payment_state_service import reconcile_booking_payment_state

        assert reconcile_booking_payment_state(sqlite_session, None) is False
