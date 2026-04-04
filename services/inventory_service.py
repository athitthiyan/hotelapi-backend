from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import models


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
    default_total_units: int = 1,
) -> models.RoomInventory:
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
        status=models.InventoryStatus.AVAILABLE,
    )
    db.add(row)
    db.flush()
    return row


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
        row.status = (
            models.InventoryStatus.BLOCKED
            if row.available_units <= 0
            else models.InventoryStatus.AVAILABLE
        )
        released += 1

    if released:
        db.commit()
    return released


def is_inventory_available(db, *, room_id: int, check_in: datetime, check_out: datetime) -> bool:
    release_expired_inventory_locks(db, room_id=room_id)
    for stay_date in iter_stay_dates(check_in, check_out):
        row = get_or_create_inventory_row(db, room_id=room_id, inventory_date=stay_date)
        if row.status == models.InventoryStatus.BLOCKED or row.available_units <= 0:
            return False
    return True


def lock_inventory_for_booking(
    db,
    *,
    booking: models.Booking,
    lock_expires_at: datetime,
) -> None:
    release_expired_inventory_locks(db, room_id=booking.room_id, booking_id=booking.id)
    for stay_date in iter_stay_dates(booking.check_in, booking.check_out):
        row = get_or_create_inventory_row(db, room_id=booking.room_id, inventory_date=stay_date)
        if row.status == models.InventoryStatus.BLOCKED or row.available_units <= 0:
            raise ValueError("Inventory is not available for the selected dates")
        row.available_units -= 1
        row.locked_units += 1
        row.locked_by_booking_id = booking.id
        row.lock_expires_at = lock_expires_at
        row.status = (
            models.InventoryStatus.LOCKED
            if row.available_units > 0
            else models.InventoryStatus.BLOCKED
        )


def confirm_inventory_for_booking(db, *, booking: models.Booking) -> None:
    rows = (
        db.query(models.RoomInventory)
        .filter(models.RoomInventory.locked_by_booking_id == booking.id)
        .all()
    )
    for row in rows:
        row.locked_units = 0
        row.locked_by_booking_id = None
        row.lock_expires_at = None
        row.status = (
            models.InventoryStatus.BLOCKED
            if row.available_units <= 0
            else models.InventoryStatus.AVAILABLE
        )


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
        row.status = (
            models.InventoryStatus.BLOCKED
            if row.available_units <= 0
            else models.InventoryStatus.AVAILABLE
        )
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
        row.available_units = total_units if available_units is None else available_units
        row.locked_units = 0
        row.locked_by_booking_id = None
        row.lock_expires_at = None
        row.status = status
        rows.append(row)
        current += timedelta(days=1)
    db.commit()
    return rows
