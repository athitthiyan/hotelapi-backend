from __future__ import annotations

from datetime import datetime, timedelta, timezone

import bcrypt
import models
from routers.auth import hash_password


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def partner_payload(**overrides):
    base = {
        "email": "partner@example.com",
        "full_name": "Partner Owner",
        "password": "PartnerPass123",
        "legal_name": "Stayvora Chennai Pvt Ltd",
        "display_name": "Stayvora Marina Suites",
        "support_email": "support@marina.example.com",
        "support_phone": "9876543210",
        "address_line": "12 Marina Road",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "country": "India",
        "postal_code": "600001",
        "gst_number": "33ABCDE1234F1Z5",
        "bank_account_name": "Stayvora Marina Suites",
        "bank_account_number": "123456789012",
        "bank_ifsc": "HDFC0001234",
        "bank_upi_id": "marina@upi",
    }
    base.update(overrides)
    return base


def create_partner_and_token(client):
    register = client.post("/partner/register", json=partner_payload())
    assert register.status_code == 201
    return register.json()["access_token"]


def create_room_for_partner(client, token: str):
    response = client.post(
        "/partner/rooms",
        headers=auth_header(token),
        json={
            "room_type": "suite",
            "room_type_name": "Suite",
            "description": "Ocean-view room",
            "price": 4500,
            "original_price": 5200,
            "total_room_count": 10,
            "weekend_price": 5100,
            "holiday_price": 6200,
            "extra_guest_charge": 750,
            "is_active": True,
            "image_url": "https://example.com/room.jpg",
            "gallery_urls": ["https://example.com/room-1.jpg", "https://example.com/room-2.jpg"],
            "amenities": ["WiFi", "Breakfast included", "Pool access"],
            "location": "Near Marina Beach",
            "city": "Chennai",
            "country": "India",
            "max_guests": 3,
            "beds": 2,
            "bathrooms": 1,
            "size_sqft": 420,
            "floor": 4,
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


class TestPartnerAuth:
    def test_partner_register_creates_partner_user_and_hotel(self, client, db_session):
        response = client.post("/partner/register", json=partner_payload())

        assert response.status_code == 201
        body = response.json()
        assert body["user"]["is_partner"] is True
        assert body["user"]["is_admin"] is False

        user = db_session.query(models.User).filter(models.User.email == "partner@example.com").first()
        assert user is not None
        assert user.is_partner is True
        hotel = (
            db_session.query(models.PartnerHotel)
            .filter(models.PartnerHotel.owner_user_id == user.id)
            .first()
        )
        assert hotel is not None
        assert hotel.display_name == "Stayvora Marina Suites"
        assert hotel.bank_account_number_masked == "********9012"

    def test_partner_login_blocks_non_partner_user(self, client, db_session):
        user = models.User(
            email="guest@example.com",
            full_name="Guest User",
            hashed_password=hash_password("GuestPass123"),
            is_active=True,
            is_admin=False,
            is_partner=False,
        )
        db_session.add(user)
        db_session.commit()

        response = client.post(
            "/partner/login",
            json={"email": "guest@example.com", "password": "GuestPass123"},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Partner access required"

    def test_partner_route_blocks_guest_token(self, client):
        signup = client.post(
            "/auth/signup",
            json={"email": "guest2@example.com", "full_name": "Guest Two", "password": "GuestPass123"},
        )

        response = client.get("/partner/hotel", headers=auth_header(signup.json()["access_token"]))
        assert response.status_code == 403
        assert response.json()["detail"] == "Partner access required"

    def test_partner_login_accepts_legacy_bcrypt_password_hash(self, client, db_session):
        user = models.User(
            email="legacy-partner@example.com",
            full_name="Legacy Partner",
            hashed_password=bcrypt.hashpw(b"PartnerPass123", bcrypt.gensalt()).decode("utf-8"),
            is_active=True,
            is_admin=False,
            is_partner=True,
        )
        db_session.add(user)
        db_session.commit()

        response = client.post(
            "/partner/login",
            json={"email": "legacy-partner@example.com", "password": "PartnerPass123"},
        )

        assert response.status_code == 200
        assert response.json()["user"]["email"] == "legacy-partner@example.com"


class TestPartnerOperations:
    def test_partner_can_manage_hotel_rooms_calendar_and_revenue(self, client, db_session):
        token = create_partner_and_token(client)
        room_id = create_room_for_partner(client, token)

        now = datetime.now(timezone.utc)
        booking = models.Booking(
            booking_ref="BKPARTNER1",
            user_name="Guest User",
            email="guest@example.com",
            phone="9999999999",
            room_id=room_id,
            check_in=now + timedelta(days=3),
            check_out=now + timedelta(days=5),
            guests=2,
            nights=2,
            room_rate=4500,
            taxes=500,
            service_fee=100,
            total_amount=9100,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        db_session.add(booking)
        db_session.commit()
        db_session.refresh(booking)

        update_hotel = client.put(
            "/partner/hotel",
            headers=auth_header(token),
            json={"description": "A trusted Marina Beach hotel", "verified_badge": True},
        )
        assert update_hotel.status_code == 200
        assert update_hotel.json()["verified_badge"] is True

        rooms = client.get("/partner/room-types", headers=auth_header(token))
        assert rooms.status_code == 200
        assert rooms.json()["total"] == 1
        assert rooms.json()["rooms"][0]["amenities"] == ["WiFi", "Breakfast included", "Pool access"]
        assert rooms.json()["rooms"][0]["room_type_name"] == "Suite"
        assert rooms.json()["rooms"][0]["total_room_count"] == 10

        room_update = client.put(
            f"/partner/room-types/{room_id}",
            headers=auth_header(token),
            json={
                "price": 4700,
                "amenities": ["WiFi", "Parking", "Pool access"],
                "weekend_price": 5300,
                "total_room_count": 12,
            },
        )
        assert room_update.status_code == 200
        assert room_update.json()["price"] == 4700
        assert room_update.json()["amenities"] == ["WiFi", "Parking", "Pool access"]
        assert room_update.json()["weekend_price"] == 5300
        assert room_update.json()["total_room_count"] == 12

        calendar_update = client.put(
            "/partner/calendar",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": (datetime.now().date() + timedelta(days=1)).isoformat(),
                "end_date": (datetime.now().date() + timedelta(days=3)).isoformat(),
                "total_units": 12,
                "available_units": 2,
                "blocked_units": 4,
                "block_reason": "maintenance",
                "status": "available",
            },
        )
        assert calendar_update.status_code == 200
        assert calendar_update.json()["room_id"] == room_id
        target_day = next(
            day
            for day in calendar_update.json()["days"]
            if day["date"] == (datetime.now().date() + timedelta(days=1)).isoformat()
        )
        assert target_day["total_units"] == 12
        assert target_day["blocked_units"] == 4
        assert target_day["available_units"] == 2
        assert target_day["block_reason"] == "maintenance"

        pricing = client.post(
            "/partner/pricing",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": (datetime.now().date() + timedelta(days=1)).isoformat(),
                "end_date": (datetime.now().date() + timedelta(days=2)).isoformat(),
                "price": 6100,
                "label": "festival",
            },
        )
        assert pricing.status_code == 200
        assert pricing.json()["days"][0]["override_price"] == 6100

        bookings = client.get("/partner/bookings", headers=auth_header(token))
        assert bookings.status_code == 200
        assert bookings.json()["total"] == 1
        assert bookings.json()["bookings"][0]["booking_ref"] == "BKPARTNER1"

        revenue = client.get("/partner/revenue", headers=auth_header(token))
        assert revenue.status_code == 200
        assert revenue.json()["gross_revenue"] == 9100
        assert revenue.json()["commission_amount"] == 1365
        assert revenue.json()["net_revenue"] == 7735

        payouts = client.get("/partner/payouts", headers=auth_header(token))
        assert payouts.status_code == 200
        assert payouts.json()["total"] == 1
        assert payouts.json()["payouts"][0]["status"] == "pending"

        statement = client.get("/partner/payouts/statement", headers=auth_header(token))
        assert statement.status_code == 200
        assert statement.headers["content-type"].startswith("text/csv")
        assert "payout_id,booking_id,status,gross_amount,commission_amount,net_amount,currency,payout_reference,payout_date,created_at" in statement.text

        unblock = client.post(
            "/partner/inventory/unblock",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": (datetime.now().date() + timedelta(days=1)).isoformat(),
                "end_date": (datetime.now().date() + timedelta(days=1)).isoformat(),
                "blocked_units": 2,
                "status": "available",
            },
        )
        assert unblock.status_code == 200
        assert unblock.json()["days"][0]["blocked_units"] == 2

    def test_partner_cannot_delete_room_with_active_booking(self, client, db_session):
        token = create_partner_and_token(client)
        room_id = create_room_for_partner(client, token)
        now = datetime.now(timezone.utc)
        db_session.add(
            models.Booking(
                booking_ref="BKACTIVE1",
                user_name="Guest User",
                email="guest@example.com",
                room_id=room_id,
                check_in=now + timedelta(days=1),
                check_out=now + timedelta(days=2),
                guests=2,
                nights=1,
                room_rate=4500,
                taxes=100,
                service_fee=0,
                total_amount=4600,
                status=models.BookingStatus.CONFIRMED,
                payment_status=models.PaymentStatus.PAID,
            )
        )
        db_session.commit()

        response = client.delete(f"/partner/rooms/{room_id}", headers=auth_header(token))
        assert response.status_code == 409
        assert response.json()["detail"] == "Room has active bookings and cannot be deleted"

    def test_partner_calendar_validates_date_range(self, client):
        token = create_partner_and_token(client)
        room_id = create_room_for_partner(client, token)

        response = client.put(
            "/partner/calendar",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": "2026-05-10",
                "end_date": "2026-05-01",
                "total_units": 2,
                "available_units": 1,
                "status": "available",
            },
        )

        assert response.status_code == 422
        assert response.json()["detail"] == "end_date must be on or after start_date"

    def test_partner_cannot_reduce_inventory_below_confirmed_or_held_rooms(self, client, db_session):
        token = create_partner_and_token(client)
        room_id = create_room_for_partner(client, token)
        room = db_session.query(models.Room).filter(models.Room.id == room_id).first()
        room.total_room_count = 10
        db_session.commit()

        current_date = datetime.now(timezone.utc).date() + timedelta(days=5)
        db_session.add(
            models.RoomInventory(
                room_id=room_id,
                inventory_date=current_date,
                total_units=10,
                available_units=4,
                locked_units=2,
                blocked_units=0,
                booked_units=4,
                status=models.InventoryStatus.LOCKED,
            )
        )
        db_session.commit()

        response = client.put(
            "/partner/calendar",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": current_date.isoformat(),
                "end_date": current_date.isoformat(),
                "total_units": 5,
                "available_units": 1,
                "blocked_units": 0,
                "status": "available",
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "INVENTORY_CONFLICT"

    def test_partner_revenue_distinguishes_pending_and_settled_payouts(self, client, db_session):
        token = create_partner_and_token(client)
        room_id = create_room_for_partner(client, token)
        now = datetime.now(timezone.utc)
        booking = models.Booking(
            booking_ref="BKPAYOUT1",
            user_name="Guest User",
            email="guest@example.com",
            room_id=room_id,
            check_in=now + timedelta(days=1),
            check_out=now + timedelta(days=2),
            guests=2,
            nights=1,
            room_rate=4500,
            taxes=100,
            service_fee=0,
            total_amount=4600,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        db_session.add(booking)
        db_session.commit()

        partner_user = db_session.query(models.User).filter(models.User.email == "partner@example.com").first()
        hotel = db_session.query(models.PartnerHotel).filter(models.PartnerHotel.owner_user_id == partner_user.id).first()
        db_session.add_all(
            [
                models.PartnerPayout(
                    hotel_id=hotel.id,
                    booking_id=booking.id,
                    gross_amount=4600,
                    commission_amount=690,
                    net_amount=3910,
                    currency="INR",
                    status=models.PayoutStatus.PENDING,
                    payout_reference="payout_pending_001",
                ),
                models.PartnerPayout(
                    hotel_id=hotel.id,
                    booking_id=None,
                    gross_amount=9200,
                    commission_amount=1380,
                    net_amount=7820,
                    currency="INR",
                    status=models.PayoutStatus.SETTLED,
                    payout_reference="payout_settled_001",
                ),
            ]
        )
        db_session.commit()

        revenue = client.get("/partner/revenue", headers=auth_header(token))
        assert revenue.status_code == 200
        assert revenue.json()["pending_payouts"] == 3910
        assert revenue.json()["paid_out"] == 7820

    def test_customer_search_reflects_partner_inventory_and_pricing(self, client):
        token = create_partner_and_token(client)
        room_id = create_room_for_partner(client, token)
        target_date = datetime.now().date() + timedelta(days=7)

        partial_block = client.put(
            "/partner/calendar",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "total_units": 10,
                "available_units": 7,
                "blocked_units": 3,
                "block_reason": "maintenance",
                "status": "available",
            },
        )
        assert partial_block.status_code == 200

        pricing = client.post(
            "/partner/pricing",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "price": 6400,
                "label": "festival",
            },
        )
        assert pricing.status_code == 200

        search = client.get(
            "/rooms",
            params={
                "check_in": target_date.isoformat(),
                "check_out": (target_date + timedelta(days=1)).isoformat(),
            },
        )
        assert search.status_code == 200
        matching_room = next(room for room in search.json()["rooms"] if room["id"] == room_id)
        assert matching_room["price"] == 6400

        unavailable = client.get(
            f"/rooms/{room_id}/unavailable-dates",
            params={"from_date": target_date.isoformat(), "to_date": target_date.isoformat()},
        )
        assert unavailable.status_code == 200
        assert target_date.isoformat() not in unavailable.json()["unavailable_dates"]

        full_block = client.post(
            "/partner/inventory/block",
            headers=auth_header(token),
            json={
                "room_type_id": room_id,
                "start_date": target_date.isoformat(),
                "end_date": target_date.isoformat(),
                "status": "blocked",
            },
        )
        assert full_block.status_code == 200

        unavailable_after_full_block = client.get(
            f"/rooms/{room_id}/unavailable-dates",
            params={"from_date": target_date.isoformat(), "to_date": target_date.isoformat()},
        )
        assert unavailable_after_full_block.status_code == 200
        assert target_date.isoformat() in unavailable_after_full_block.json()["unavailable_dates"]
