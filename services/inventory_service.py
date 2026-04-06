from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from threading import Lock
from typing import Iterable

import models
from sqlalchemy.orm import Query


_ROOM_LOCK_GUARD = Lock()
_ROOM_LOCKS: dict[int, Lock] = {}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iter_stay_dates(check_in: datetime, check_out: datetime) -> Iterable[date]:
    current = check_in.date()
    checkout_date = check_out.date()
    while current < checkout_date:
        yield current
        current += timedelta(days=1)


def normalize_datetime_for_compare(value: datetime, reference: datetime) -> datetime:
    if value.tzinfo is None and reference.tzinfo is not None:
        return value.replace(tzinfo=reference.tzinfo)
    if value.tzinfo is not None and reference.tzinfo is None:
        return value.replace(tzinfo=None)
    return value


def get_or_create_inventory_row(
    db,
    *,
    room_id: int,
    inventory_date: date,
    default_total_units: int | None = None,
) -> models.RoomInventory:
    if default_total_units is None:
        room = db.query(models.Room).filter(models.Room.id == room_id).first()
        default_total_units = room.total_room_count if room else 1
    row = (
        db.query(models.RoomInventory)
        .filter(
            models.RoomInventory.room_id == room_id,
            models.RoomInventory.inventory_date == inventory_date,
        )
        .first()
    )
    if row:
        return row

    row = models.RoomInventory(
        room_id=room_id,
        inventory_date=inventory_date,
        total_units=default_total_units,
        available_units=default_total_units,
        locked_units=0,
        booked_units=0,
        blocked_units=0,
        status=models.InventoryStatus.AVAILABLE,
    )
    db.add(row)
    db.flush()
    return row


def apply_inventory_row_lock(db, query: Query) -> Query:
    bind = getattr(db, "bind", None)
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name == "sqlite":
        return query
    return query.with_for_update()


def uses_sqlite(db) -> bool:
    bind = getattr(db, "bind", None)
    return getattr(getattr(bind, "dialect", None), "name", "") == "sqlite"


@contextmanager
def inventory_lock_scope(db, room_id: int):
    if not uses_sqlite(db):
        yield
        return

    with _ROOM_LOCK_GUARD:
        room_lock = _ROOM_LOCKS.setdefault(room_id, Lock())
    with room_lock:
        yield


def get_or_create_inventory_rows_for_stay(
    db,
    *,
    room_id: int,
    check_in: datetime,
    check_out: datetime,
    default_total_units: int | None = None,
    for_update: bool = False,
) -> list[models.RoomInventory]:
    stay_dates = list(iter_stay_dates(check_in, check_out))
    if not stay_dates:
        return []

    query = db.query(models.RoomInventory).filter(
        models.RoomInventory.room_id == room_id,
        models.RoomInventory.inventory_date.in_(stay_dates),
    )
    if for_update:
        query = apply_inventory_row_lock(db, query)
    existing_rows = query.all()
    rows_by_date = {row.inventory_date: row for row in existing_rows}

    missing_dates = [stay_date for stay_date in stay_dates if stay_date not in rows_by_date]
    for inventory_date in missing_dates:
        db.add(
            models.RoomInventory(
                room_id=room_id,
                inventory_date=inventory_date,
                total_units=default_total_units,
                available_units=default_total_units,
                locked_units=0,
                booked_units=0,
                blocked_units=0,
                status=models.InventoryStatus.AVAILABLE,
            )
        )

    if missing_dates:
        db.flush()
        query = db.query(models.RoomInventory).filter(
            models.RoomInventory.room_id == room_id,
            models.RoomInventory.inventory_date.in_(stay_dates),
        )
        if for_update:
            query = apply_inventory_row_lock(db, query)
        existing_rows = query.all()
        rows_by_date = {row.inventory_date: row for row in existing_rows}

    return [rows_by_date[stay_date] for stay_date in stay_dates]


def derive_inventory_status(row: models.RoomInventory) -> models.InventoryStatus:
    if row.available_units <= 0:
        return models.InventoryStatus.BLOCKED
    if row.locked_units > 0:
        return models.InventoryStatus.LOCKED
    return models.InventoryStatus.AVAILABLE


def calculate_effective_price(
    room: models.Room,
    *,
    inventory_date: date,
    inventory_row: models.RoomInventory | None = None,
) -> float:
    if inventory_row and inventory_row.price_override is not None:
        return inventory_row.price_override
    if inventory_date.weekday() >= 5 and room.weekend_price is not None:
        return room.weekend_price
    return room.price


def release_expired_inventory_locks(
    db,
    *,
    room_id: int | None = None,
    booking_id: int | None = None,
    now: datetime | None = None,
) -> int:
    now = now or utc_now()
    query = db.query(models.RoomInventory).filter(
        models.RoomInventory.lock_expires_at.is_not(None),
        models.RoomInventory.locked_units > 0,
    )
    if room_id is not None:
        query = query.filter(models.RoomInventory.room_id == room_id)
    if booking_id is not None:
        query = query.filter(models.RoomInventory.locked_by_booking_id == booking_id)

    rows = query.all()
    released = 0
    for row in rows:
        lock_expires_at = row.lock_expires_at
        if lock_expires_at is None:
            continue
        lock_expires_at = normalize_datetime_for_compare(lock_expires_at, now)
        if lock_expires_at > now:
            continue
        row.available_units = min(row.total_units, row.available_units + row.locked_units)
        row.locked_units = 0
        row.locked_by_booking_id = None
        row.lock_expires_at = None
        row.status = derive_inventory_status(row)
        released += 1

    if released:
        db.commit()
    return released


def is_inventory_available(db, *, room_id: int, check_in: datetime, check_out: datetime) -> bool:
    release_expired_inventory_locks(db, room_id=room_id)
    for row in get_or_create_inventory_rows_for_stay(
        db,
        room_id=room_id,
        check_in=check_in,
        check_out=check_out,
    ):
        if row.status == models.InventoryStatus.BLOCKED or row.available_units <= 0:
            return False
    return True


def lock_inventory_for_booking(
    db,
    *,
    booking: models.Booking,
    lock_expires_at: datetime,
) -> None:
    with inventory_lock_scope(db, booking.room_id):
        release_expired_inventory_locks(db, room_id=booking.room_id, booking_id=booking.id)
        rows = get_or_create_inventory_rows_for_stay(
            db,
            room_id=booking.room_id,
            check_in=booking.check_in,
            check_out=booking.check_out,
            for_update=True,
        )
        for row in rows:
            if row.status == models.InventoryStatus.BLOCKED or row.available_units <= 0:
                raise ValueError("Inventory is not available for the selected dates")
            row.available_units -= 1
            row.locked_units += 1
            row.locked_by_booking_id = booking.id
            row.lock_expires_at = lock_expires_at
            row.status = derive_inventory_status(row)


def confirm_inventory_for_booking(db, *, booking: models.Booking) -> None:
    rows = (
        db.query(models.RoomInventory)
        .filter(models.RoomInventory.locked_by_booking_id == booking.id)
        .all()
    )
    for row in rows:
        row.locked_units = max(0, row.locked_units - 1)
        row.booked_units += 1
        row.locked_by_booking_id = None
        row.lock_expires_at = None
        row.status = derive_inventory_status(row)


def release_inventory_for_booking(db, *, booking: models.Booking) -> int:
    rows = (
        db.query(models.RoomInventory)
        .filter(models.RoomInventory.locked_by_booking_id == booking.id)
        .all()
    )
    for row in rows:
        row.available_units = min(row.total_units, row.available_units + row.locked_units)
        row.locked_units = 0
        row.locked_by_booking_id = None
        row.lock_expires_at = None
        row.status = derive_inventory_status(row)
    return len(rows)


def is_booking_inventory_locked(db, *, booking: models.Booking) -> bool:
    """Return True if every stay date for this booking has a non-expired lock
    specifically assigned to it.  Unlike is_inventory_available, this function
    checks for the *existing* lock rather than whether a brand-new lock could be
    acquired, so it is safe to call during payment processing."""
    now = utc_now()
    for stay_date in iter_stay_dates(booking.check_in, booking.check_out):
        row = (
            db.query(models.RoomInventory)
            .filter(
                models.RoomInventory.room_id == booking.room_id,
                models.RoomInventory.inventory_date == stay_date,
            )
            .first()
        )
        if not row or row.locked_by_booking_id != booking.id:
            return False
        lock_exp = row.lock_expires_at
        if lock_exp is None:
            return False
        lock_exp = normalize_datetime_for_compare(lock_exp, now)
        if lock_exp <= now:
            return False
    return True


def upsert_inventory_range(
    db,
    *,
    room_id: int,
    start_date: date,
    end_date: date,
    total_units: int,
    available_units: int | None,
    blocked_units: int | None = None,
    block_reason: str | None = None,
    price_override: float | None = None,
    price_override_label: str | None = None,
    status: models.InventoryStatus,
) -> list[models.RoomInventory]:
    current = start_date
    rows: list[models.RoomInventory] = []
    while current <= end_date:
        row = get_or_create_inventory_row(
            db,
            room_id=room_id,
            inventory_date=current,
            default_total_units=total_units,
        )
        row.total_units = total_units
        if available_units is not None:
            row.available_units = available_units
        else:
            row.available_units = total_units
        row.locked_units = 0
        row.booked_units = 0
        row.blocked_units = blocked_units or 0
        row.block_reason = block_reason
        row.price_override = price_override
        row.price_override_label = price_override_label
        row.locked_by_booking_id = None
        row.lock_expires_at = None
        row.status = status
        rows.append(row)
        current += timedelta(days=1)
    db.commit()
    return rows
