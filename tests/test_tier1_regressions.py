from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier, Thread
from unittest.mock import MagicMock, patch

import models
from main import run_expired_hold_release
from routers.auth import hash_password
from services.inventory_service import lock_inventory_for_booking


def auth_header(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def signup_and_login(client, email: str, password: str = "StrongPass123", phone: str = "+91 98765 43210") -> str:
    # Request OTP for email
    otp_email_resp = client.post("/auth/otp/request", json={
        "flow": "signup",
        "channel": "email",
        "recipient": email,
    })
    assert otp_email_resp.status_code == 200
    email_challenge_id = otp_email_resp.json()["challenge_id"]
    email_dev_code = otp_email_resp.json()["dev_code"]

    # Request OTP for phone
    otp_phone_resp = client.post("/auth/otp/request", json={
        "flow": "signup",
        "channel": "phone",
        "recipient": phone,
    })
    assert otp_phone_resp.status_code == 200
    phone_challenge_id = otp_phone_resp.json()["challenge_id"]
    phone_dev_code = otp_phone_resp.json()["dev_code"]

    # Verify email OTP
    verify_email_resp = client.post("/auth/otp/verify", json={
        "challenge_id": email_challenge_id,
        "otp": email_dev_code,
    })
    assert verify_email_resp.status_code == 200

    # Verify phone OTP
    verify_phone_resp = client.post("/auth/otp/verify", json={
        "challenge_id": phone_challenge_id,
        "otp": phone_dev_code,
    })
    assert verify_phone_resp.status_code == 200

    # Sign up with verified challenges
    signup = client.post(
        "/auth/signup",
        json={
            "email": email,
            "phone": phone,
            "full_name": "Tier One User",
            "password": password,
            "email_challenge_id": email_challenge_id,
            "phone_challenge_id": phone_challenge_id,
        },
    )
    assert signup.status_code == 201

    # Login
    login = client.post("/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200
    return login.json()["access_token"]


def booking_payload(room_id: int, email: str, check_in: datetime, check_out: datetime) -> dict:
    return {
        "room_id": room_id,
        "user_name": "Tier User",
        "email": email,
        "phone": "9876543210",
        "check_in": check_in.isoformat(),
        "check_out": check_out.isoformat(),
        "guests": 2,
        "special_requests": "",
    }


def stripe_intent(intent_id: str, client_secret: str):
    return MagicMock(id=intent_id, client_secret=client_secret)


def test_inventory_lock_allows_only_one_concurrent_booking(app):
    session_factory = app.state.testing_session_local
    setup_db = session_factory()
    room = models.Room(
        hotel_name="Race Safe Hotel",
        room_type=models.RoomType.DELUXE,
        description="Only one room left",
        price=250.0,
        availability=True,
        city="Chennai",
        country="India",
    )
    setup_db.add(room)
    setup_db.commit()
    setup_db.refresh(room)
    room_id = room.id

    check_in = datetime.now(timezone.utc) + timedelta(days=15)
    check_out = check_in + timedelta(days=2)
    stay_dates = [check_in.date(), (check_in + timedelta(days=1)).date()]
    for stay_date in stay_dates:
        setup_db.add(
            models.RoomInventory(
                room_id=room.id,
                inventory_date=stay_date,
                total_units=1,
                available_units=1,
                locked_units=0,
                status=models.InventoryStatus.AVAILABLE,
            )
        )
    setup_db.commit()
    setup_db.close()

    barrier = Barrier(2)
    results: list[str] = []

    def worker(email: str):
        db = session_factory()
        booking = models.Booking(
            id=1 if email.startswith("race-a") else 2,
            booking_ref=f"BK-{email.split('@')[0].upper()}",
            user_name="Race Tester",
            email=email,
            room_id=room_id,
            check_in=check_in,
            check_out=check_out,
            hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
            guests=2,
            nights=2,
            room_rate=500.0,
            taxes=60.0,
            service_fee=25.0,
            total_amount=585.0,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        barrier.wait()
        try:
            lock_inventory_for_booking(db, booking=booking, lock_expires_at=booking.hold_expires_at)
            db.commit()
            results.append("locked")
        except ValueError:
            db.rollback()
            results.append("conflict")
        finally:
            db.close()

    first = Thread(target=worker, args=("race-a@example.com",))
    second = Thread(target=worker, args=("race-b@example.com",))
    first.start()
    second.start()
    first.join()
    second.join()

    assert sorted(results) == ["conflict", "locked"]


def test_hold_expiry_scheduler_releases_inventory_and_restores_searchability(client, app, db_session):
    room = models.Room(
        hotel_name="Scheduler Hotel",
        room_type=models.RoomType.SUITE,
        description="Scheduler room",
        price=300.0,
        availability=True,
        city="Chennai",
        country="India",
    )
    db_session.add(room)
    db_session.commit()
    db_session.refresh(room)

    check_in = datetime.now(timezone.utc) + timedelta(days=30)
    check_out = check_in + timedelta(days=2)
    booking = client.post(
        "/bookings",
        json=booking_payload(room.id, "hold@example.com", check_in, check_out),
    )
    assert booking.status_code == 201
    booking_id = booking.json()["id"]

    booking_row = db_session.query(models.Booking).filter_by(id=booking_id).first()
    booking_row.hold_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    inventory_rows = db_session.query(models.RoomInventory).filter_by(room_id=room.id).all()
    for row in inventory_rows:
        row.lock_expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    db_session.commit()

    released = run_expired_hold_release(app.state.testing_session_local)

    db_session.refresh(booking_row)
    for row in inventory_rows:
        db_session.refresh(row)

    search = client.get(
        "/rooms",
        params={
            "city": "Chennai",
            "check_in": check_in.date().isoformat(),
            "check_out": check_out.date().isoformat(),
        },
    )

    assert released >= 1
    assert booking_row.status == models.BookingStatus.EXPIRED
    assert booking_row.payment_status == models.PaymentStatus.EXPIRED
    assert all(row.locked_units == 0 for row in inventory_rows)
    assert search.status_code == 200
    assert search.json()["total"] == 1


def test_mixed_case_login_and_bookings_history_preserve_user_link(client, db_session, room_id):
    token = signup_and_login(client, "MixedCase@Example.com")
    headers = auth_header(token)
    check_in = datetime.now(timezone.utc) + timedelta(days=20)
    check_out = check_in + timedelta(days=2)

    created = client.post(
        "/bookings",
        json=booking_payload(room_id, "mixedcase@example.com", check_in, check_out),
    )
    assert created.status_code == 201

    relogin = client.post(
        "/auth/login",
        json={"email": "MIXEDCASE@example.com", "password": "StrongPass123"},
    )
    my_bookings = client.get("/auth/me/bookings", headers=auth_header(relogin.json()["access_token"]))
    booking_row = db_session.query(models.Booking).filter_by(id=created.json()["id"]).first()

    assert relogin.status_code == 200
    assert my_bookings.status_code == 200
    assert my_bookings.json()["total"] == 1
    assert booking_row.user_id is not None
    assert booking_row.email == "mixedcase@example.com"
    assert client.get("/bookings/history", params={"email": "MixedCase@Example.com"}).json()["total"] == 1
    assert headers


def test_guest_booking_preserved_without_user_id(client, db_session, room_id):
    check_in = datetime.now(timezone.utc) + timedelta(days=25)
    check_out = check_in + timedelta(days=1)
    created = client.post(
        "/bookings",
        json=booking_payload(room_id, "guest-only@example.com", check_in, check_out),
    )
    booking_row = db_session.query(models.Booking).filter_by(id=created.json()["id"]).first()

    assert created.status_code == 201
    assert booking_row.user_id is None
    history = client.get("/bookings/history", params={"email": "Guest-Only@Example.com"})
    assert history.status_code == 200
    assert history.json()["total"] == 1


def test_transactions_endpoint_requires_admin(client, db_session):
    guest_token = signup_and_login(client, "payments-user@example.com")
    admin = models.User(
        email="admin-transactions@example.com",
        full_name="Admin Transactions",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    admin_login = client.post(
        "/auth/login",
        json={"email": "admin-transactions@example.com", "password": "AdminPass123"},
    )

    unauthenticated = client.get("/payments/transactions")
    guest_access = client.get("/payments/transactions", headers=auth_header(guest_token))
    admin_access = client.get(
        "/payments/transactions",
        headers=auth_header(admin_login.json()["access_token"]),
    )

    assert unauthenticated.status_code == 401
    assert guest_access.status_code == 403
    assert admin_access.status_code == 200


def test_card_client_success_stays_processing_until_webhook(client, create_booking, db_session):
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_processing_only", "secret_processing_only"),
    ), patch(
        "routers.payments.stripe.PaymentIntent.retrieve",
        return_value={
            "id": "pi_processing_only",
            "status": "processing",
            "charges": {"data": []},
        },
    ):
        intent = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "process-001"},
        )

    acknowledged = client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking["id"],
            "transaction_ref": intent.json()["transaction_ref"],
            "payment_method": "card",
            "payment_intent_id": intent.json()["payment_intent_id"],
            "card_last4": "4242",
            "card_brand": "visa",
        },
    )
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()

    assert acknowledged.status_code == 200
    assert acknowledged.json()["status"] == "processing"
    assert booking_row.status == models.BookingStatus.PROCESSING
    assert booking_row.payment_status == models.PaymentStatus.PROCESSING


def test_out_of_order_webhook_before_client_ack_is_safe(client, create_booking, db_session):
    booking = create_booking()

    with patch(
        "routers.payments.stripe.PaymentIntent.create",
        return_value=stripe_intent("pi_out_of_order", "secret_out_of_order"),
    ):
        intent = client.post(
            "/payments/create-payment-intent",
            json={"booking_id": booking["id"], "payment_method": "card", "idempotency_key": "ooo-0001"},
        )

    event = {
        "type": "payment_intent.succeeded",
        "data": {
            "object": {
                "id": "pi_out_of_order",
                "metadata": {
                    "booking_id": str(booking["id"]),
                    "transaction_ref": intent.json()["transaction_ref"],
                },
                "charges": {
                    "data": [
                        {
                            "payment_method_details": {
                                "card": {"last4": "4242", "brand": "visa"}
                            }
                        }
                    ]
                },
            }
        },
    }

    with patch("routers.payments.stripe.Webhook.construct_event", return_value=event):
        webhook = client.post("/payments/webhook", content=b"{}", headers={"stripe-signature": "sig"})

    acknowledged = client.post(
        "/payments/payment-success",
        json={
            "booking_id": booking["id"],
            "transaction_ref": intent.json()["transaction_ref"],
            "payment_method": "card",
            "payment_intent_id": intent.json()["payment_intent_id"],
            "card_last4": "4242",
            "card_brand": "visa",
        },
    )
    booking_row = db_session.query(models.Booking).filter_by(id=booking["id"]).first()
    transactions = db_session.query(models.Transaction).filter_by(booking_id=booking["id"]).all()

    assert webhook.status_code == 200
    assert acknowledged.status_code == 200
    assert acknowledged.json()["status"] == "success"
    assert booking_row.status == models.BookingStatus.CONFIRMED
    assert booking_row.payment_status == models.PaymentStatus.PAID
    assert len(transactions) == 1
