from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import models
from services import inventory_service


def _room(db_session):
    room = models.Room(
        hotel_name="Inventory Test Hotel",
        room_type=models.RoomType.SUITE,
        description="Inventory room",
        price=200.0,
        availability=True,
        city="Chennai",
        country="India",
    )
    db_session.add(room)
    db_session.commit()
    db_session.refresh(room)
    return room


def _booking(room_id: int, **overrides):
    check_in = overrides.pop("check_in", datetime.now(timezone.utc) + timedelta(days=10))
    check_out = overrides.pop("check_out", check_in + timedelta(days=2))
    hold_expires_at = overrides.pop(
        "hold_expires_at", datetime.now(timezone.utc) + timedelta(minutes=10)
    )
    booking = models.Booking(
        booking_ref="BKINVENT1",
        user_name="Inventory User",
        email="inventory@example.com",
        room_id=room_id,
        check_in=check_in,
        check_out=check_out,
        hold_expires_at=hold_expires_at,
        guests=2,
        nights=(check_out - check_in).days,
        room_rate=400.0,
        taxes=48.0,
        service_fee=20.0,
        total_amount=468.0,
        status=models.BookingStatus.PENDING,
        payment_status=models.PaymentStatus.PENDING,
        **overrides,
    )
    return booking


def test_normalize_datetime_for_compare_handles_naive_and_aware_values():
    aware = datetime.now(timezone.utc)
    naive = aware.replace(tzinfo=None)

    assert inventory_service.normalize_datetime_for_compare(naive, aware).tzinfo == timezone.utc
    assert inventory_service.normalize_datetime_for_compare(aware, naive).tzinfo is None
    assert inventory_service.normalize_datetime_for_compare(aware, aware) == aware


def test_apply_inventory_row_lock_skips_sqlite_and_locks_other_dialects():
    sqlite_db = MagicMock()
    sqlite_db.bind.dialect.name = "sqlite"
    sqlite_query = MagicMock()
    assert inventory_service.apply_inventory_row_lock(sqlite_db, sqlite_query) is sqlite_query

    postgres_db = MagicMock()
    postgres_db.bind.dialect.name = "postgresql"
    postgres_query = MagicMock()
    postgres_query.with_for_update.return_value = "locked-query"
    assert inventory_service.apply_inventory_row_lock(postgres_db, postgres_query) == "locked-query"
    postgres_query.with_for_update.assert_called_once()


def test_uses_sqlite_detects_dialect():
    sqlite_db = MagicMock()
    sqlite_db.bind.dialect.name = "sqlite"
    assert inventory_service.uses_sqlite(sqlite_db) is True

    postgres_db = MagicMock()
    postgres_db.bind.dialect.name = "postgresql"
    assert inventory_service.uses_sqlite(postgres_db) is False


def test_get_or_create_inventory_row_creates_missing_row(db_session):
    room = _room(db_session)
    inventory_date = date.today() + timedelta(days=7)

    row = inventory_service.get_or_create_inventory_row(
        db_session,
        room_id=room.id,
        inventory_date=inventory_date,
        default_total_units=3,
    )

    assert row.total_units == 3
    assert row.available_units == 3
    assert row.inventory_date == inventory_date

    existing = inventory_service.get_or_create_inventory_row(
        db_session,
        room_id=room.id,
        inventory_date=inventory_date,
        default_total_units=9,
    )
    assert existing.id == row.id
    assert existing.total_units == 3


def test_get_or_create_inventory_rows_for_stay_returns_empty_for_zero_nights(db_session):
    room = _room(db_session)
    check_in = datetime.now(timezone.utc) + timedelta(days=3)
    rows = inventory_service.get_or_create_inventory_rows_for_stay(
        db_session,
        room_id=room.id,
        check_in=check_in,
        check_out=check_in,
    )
    assert rows == []


def test_get_or_create_inventory_rows_for_stay_backfills_missing_dates(db_session):
    room = _room(db_session)
    check_in = datetime.now(timezone.utc) + timedelta(days=5)
    check_out = check_in + timedelta(days=3)

    rows = inventory_service.get_or_create_inventory_rows_for_stay(
        db_session,
        room_id=room.id,
        check_in=check_in,
        check_out=check_out,
        default_total_units=2,
        for_update=True,
    )

    assert len(rows) == 3
    assert all(row.total_units == 2 for row in rows)
    assert [row.inventory_date for row in rows] == list(
        inventory_service.iter_stay_dates(check_in, check_out)
    )


def test_release_expired_inventory_locks_releases_only_expired_rows(db_session):
    room = _room(db_session)
    expired = models.RoomInventory(
        room_id=room.id,
        inventory_date=date.today() + timedelta(days=9),
        total_units=1,
        available_units=0,
        locked_units=1,
        locked_by_booking_id=11,
        lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        status=models.InventoryStatus.BLOCKED,
    )
    active = models.RoomInventory(
        room_id=room.id,
        inventory_date=date.today() + timedelta(days=10),
        total_units=1,
        available_units=0,
        locked_units=1,
        locked_by_booking_id=12,
        lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        status=models.InventoryStatus.BLOCKED,
    )
    db_session.add_all([expired, active])
    db_session.commit()

    released = inventory_service.release_expired_inventory_locks(db_session, room_id=room.id)
    db_session.refresh(expired)
    db_session.refresh(active)

    assert released == 1
    assert expired.available_units == 1
    assert expired.locked_units == 0
    assert expired.locked_by_booking_id is None
    assert active.locked_units == 1


def test_release_expired_inventory_locks_returns_zero_when_nothing_expires(db_session):
    room = _room(db_session)
    row = models.RoomInventory(
        room_id=room.id,
        inventory_date=date.today() + timedelta(days=12),
        total_units=1,
        available_units=1,
        locked_units=0,
        status=models.InventoryStatus.AVAILABLE,
    )
    db_session.add(row)
    db_session.commit()

    assert inventory_service.release_expired_inventory_locks(db_session, room_id=room.id) == 0


def test_lock_and_confirm_inventory_for_booking_transitions_rows(db_session):
    room = _room(db_session)
    booking = _booking(room.id)
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)

    inventory_service.lock_inventory_for_booking(
        db_session,
        booking=booking,
        lock_expires_at=booking.hold_expires_at,
    )
    db_session.commit()

    rows = inventory_service.get_or_create_inventory_rows_for_stay(
        db_session,
        room_id=room.id,
        check_in=booking.check_in,
        check_out=booking.check_out,
    )
    assert all(row.locked_by_booking_id == booking.id for row in rows)
    assert all(row.locked_units == 1 for row in rows)

    inventory_service.confirm_inventory_for_booking(db_session, booking=booking)
    db_session.commit()

    for row in rows:
        db_session.refresh(row)
    assert all(row.locked_units == 0 for row in rows)
    assert all(row.locked_by_booking_id is None for row in rows)


def test_lock_inventory_for_booking_raises_when_inventory_is_blocked(db_session):
    room = _room(db_session)
    booking = _booking(room.id)
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)

    for stay_date in inventory_service.iter_stay_dates(booking.check_in, booking.check_out):
        db_session.add(
            models.RoomInventory(
                room_id=room.id,
                inventory_date=stay_date,
                total_units=1,
                available_units=0,
                locked_units=0,
                status=models.InventoryStatus.BLOCKED,
            )
        )
    db_session.commit()

    try:
        inventory_service.lock_inventory_for_booking(
            db_session,
            booking=booking,
            lock_expires_at=booking.hold_expires_at,
        )
    except ValueError as exc:
        assert "not available" in str(exc)
    else:
        raise AssertionError("Expected lock_inventory_for_booking to raise ValueError")


def test_release_inventory_for_booking_and_lock_status_queries(db_session):
    room = _room(db_session)
    booking = _booking(room.id)
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)

    inventory_service.lock_inventory_for_booking(
        db_session,
        booking=booking,
        lock_expires_at=booking.hold_expires_at,
    )
    db_session.commit()

    assert inventory_service.is_booking_inventory_locked(db_session, booking=booking) is True

    released_rows = inventory_service.release_inventory_for_booking(db_session, booking=booking)
    db_session.commit()

    assert released_rows == 2
    assert inventory_service.is_booking_inventory_locked(db_session, booking=booking) is False


def test_is_booking_inventory_locked_handles_missing_or_expired_rows(db_session):
    room = _room(db_session)
    booking = _booking(room.id)
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)

    assert inventory_service.is_booking_inventory_locked(db_session, booking=booking) is False

    row = models.RoomInventory(
        room_id=room.id,
        inventory_date=booking.check_in.date(),
        total_units=1,
        available_units=0,
        locked_units=1,
        locked_by_booking_id=booking.id,
        lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        status=models.InventoryStatus.LOCKED,
    )
    db_session.add(row)
    db_session.commit()

    assert inventory_service.is_booking_inventory_locked(db_session, booking=booking) is False


def test_upsert_inventory_range_defaults_available_units_to_total(db_session):
    room = _room(db_session)
    rows = inventory_service.upsert_inventory_range(
        db_session,
        room_id=room.id,
        start_date=date.today() + timedelta(days=20),
        end_date=date.today() + timedelta(days=21),
        total_units=4,
        available_units=None,
        status=models.InventoryStatus.AVAILABLE,
    )

    assert len(rows) == 2
    assert all(row.available_units == 4 for row in rows)
