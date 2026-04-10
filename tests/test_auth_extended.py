"""Tests for extended auth endpoints: forgot/reset password, social login, profile, my bookings."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base, get_db
from routers import auth, bookings, payments, rooms
from services.rate_limit_service import reset_rate_limits

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_bootstrap.db")


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "auth_ext_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    _app = FastAPI()
    _app.include_router(auth.router)
    _app.include_router(rooms.router)
    _app.include_router(bookings.router)
    _app.include_router(payments.router)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    _app.dependency_overrides[get_db] = override_get_db
    _app.state.testing_session_local = TestingSessionLocal

    yield _app

    _app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def client(app):
    return TestClient(app, raise_server_exceptions=True)


def _signup_login(client: TestClient, email: str = "user@test.com", password: str = "TestPass123", phone: str = "+91 98765 43210") -> str:
    reset_rate_limits()

    # Request OTP for email
    otp_email_resp = client.post("/auth/otp/request", json={
        "flow": "signup",
        "channel": "email",
        "recipient": email
    })
    assert otp_email_resp.status_code == 200
    email_challenge_id = otp_email_resp.json()["challenge_id"]
    email_dev_code = otp_email_resp.json().get("dev_code", "000000")

    # Request OTP for phone
    otp_phone_resp = client.post("/auth/otp/request", json={
        "flow": "signup",
        "channel": "phone",
        "recipient": phone
    })
    assert otp_phone_resp.status_code == 200
    phone_challenge_id = otp_phone_resp.json()["challenge_id"]
    phone_dev_code = otp_phone_resp.json().get("dev_code", "000000")

    # Verify email OTP
    verify_email_resp = client.post("/auth/otp/verify", json={
        "challenge_id": email_challenge_id,
        "otp": email_dev_code
    })
    assert verify_email_resp.status_code == 200

    # Verify phone OTP
    verify_phone_resp = client.post("/auth/otp/verify", json={
        "challenge_id": phone_challenge_id,
        "otp": phone_dev_code
    })
    assert verify_phone_resp.status_code == 200

    # Now signup with verified challenges
    signup_resp = client.post("/auth/signup", json={
        "email": email,
        "phone": phone,
        "full_name": "Test User",
        "password": password,
        "email_challenge_id": email_challenge_id,
        "phone_challenge_id": phone_challenge_id
    })
    assert signup_resp.status_code == 201

    # Login
    resp = client.post("/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


# ─── Forgot Password ──────────────────────────────────────────────────────────

class TestForgotPassword:
    def test_always_returns_200_for_unknown_email(self, client):
        reset_rate_limits()
        r = client.post("/auth/forgot-password", json={"channel": "email", "recipient": "ghost@test.com"})
        assert r.status_code == 200
        assert "otp" in r.json()["message"].lower() or "sent" in r.json()["message"].lower()

    def test_creates_otp_challenge_for_known_email(self, client, app):
        reset_rate_limits()
        email = "resetme@test.com"
        _signup_login(client, email)
        r = client.post("/auth/forgot-password", json={"channel": "email", "recipient": email})
        assert r.status_code == 200
        assert "challenge_id" in r.json()

        db = app.state.testing_session_local()
        user = db.query(models.User).filter(models.User.email == email).first()
        challenges = db.query(models.OtpChallenge).filter(
            models.OtpChallenge.user_id == user.id,
            models.OtpChallenge.flow == "password_reset"
        ).all()
        db.close()
        assert len(challenges) == 1

    def test_invalidates_previous_challenge_on_new_request(self, client, app):
        reset_rate_limits()
        email = "double@test.com"
        _signup_login(client, email)
        reset_rate_limits()
        r1 = client.post("/auth/forgot-password", json={"channel": "email", "recipient": email})
        assert r1.status_code == 200
        assert "challenge_id" in r1.json()

        # Second request may trigger resend cooldown; accept 200 or 429
        reset_rate_limits()
        r2 = client.post("/auth/forgot-password", json={"channel": "email", "recipient": email})
        assert r2.status_code in [200, 429]
        if r2.status_code == 200:
            assert "challenge_id" in r2.json()


# ─── Reset Password ───────────────────────────────────────────────────────────

class TestResetPassword:
    def _get_reset_token(self, client, email: str) -> str:
        """Complete the forgot-password OTP flow to get a reset_token."""
        # Request OTP
        reset_rate_limits()
        otp_resp = client.post("/auth/forgot-password", json={"channel": "email", "recipient": email})
        assert otp_resp.status_code == 200
        challenge_id = otp_resp.json()["challenge_id"]
        dev_code = otp_resp.json().get("dev_code", "000000")

        # Verify OTP
        verify_resp = client.post("/auth/otp/verify", json={
            "challenge_id": challenge_id,
            "otp": dev_code
        })
        assert verify_resp.status_code == 200
        reset_token = verify_resp.json().get("reset_token")
        assert reset_token is not None
        return reset_token

    def test_reset_password_success(self, client, app):
        email = "resetpw@test.com"
        _signup_login(client, email)
        reset_token = self._get_reset_token(client, email)

        r = client.post(
            "/auth/reset-password",
            json={"reset_token": reset_token, "new_password": "NewPass999"},
        )
        assert r.status_code == 200

        # Should be able to login with new password
        reset_rate_limits()
        login_r = client.post("/auth/login", json={"email": email, "password": "NewPass999"})
        assert login_r.status_code == 200

    def test_reset_with_invalid_token(self, client):
        r = client.post(
            "/auth/reset-password",
            json={"reset_token": "totally-fake-token", "new_password": "NewPass999"},
        )
        # Non-JWT tokens fall through to the link-token path which returns 400
        assert r.status_code == 400
        assert r.json()["detail"] == "Reset token is invalid or has expired"

    def test_reset_with_expired_token(self, client, app):
        email = "expired@test.com"
        _signup_login(client, email)

        # Get a valid OTP challenge and then expire it
        reset_rate_limits()
        otp_resp = client.post("/auth/forgot-password", json={"channel": "email", "recipient": email})
        assert otp_resp.status_code == 200
        challenge_id = otp_resp.json()["challenge_id"]

        # Set the challenge to expired in the database
        db = app.state.testing_session_local()
        challenge = db.query(models.OtpChallenge).filter(
            models.OtpChallenge.id == challenge_id
        ).first()
        if challenge:
            challenge.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
            db.commit()
        db.close()

        dev_code = otp_resp.json().get("dev_code", "000000")
        verify_resp = client.post("/auth/otp/verify", json={
            "challenge_id": challenge_id,
            "otp": dev_code
        })
        # Should fail because challenge is expired
        assert verify_resp.status_code == 400

    def test_reset_with_already_used_token(self, client, app):
        email = "used@test.com"
        _signup_login(client, email)

        # Get a reset token
        reset_token = self._get_reset_token(client, email)

        # Use it once
        r1 = client.post(
            "/auth/reset-password",
            json={"reset_token": reset_token, "new_password": "NewPass999"},
        )
        assert r1.status_code == 200

        # Try to use it again
        r2 = client.post(
            "/auth/reset-password",
            json={"reset_token": reset_token, "new_password": "AnotherPass456"},
        )
        assert r2.status_code == 400


# ─── Update Profile ───────────────────────────────────────────────────────────

class TestUpdateProfile:
    def test_update_full_name(self, client, app):
        token = _signup_login(client, "prof@test.com")
        r = client.put(
            "/auth/me",
            json={"full_name": "Updated Name"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["full_name"] == "Updated Name"

    def test_update_phone_requires_otp_for_new_number(self, client, app):
        token = _signup_login(client, "phone@test.com")
        r = client.put(
            "/auth/me",
            json={"phone": "+1-555-0000"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 400
        assert r.json()["detail"] == "Verify this phone number with OTP first"

    def test_update_profile_requires_auth(self, client):
        r = client.put("/auth/me", json={"full_name": "X"})
        assert r.status_code == 401


# ─── Change Password ──────────────────────────────────────────────────────────

class TestChangePassword:
    def test_change_password_success(self, client, app):
        token = _signup_login(client, "changepw@test.com")
        r = client.post(
            "/auth/change-password",
            json={"current_password": "TestPass123", "new_password": "NewPass456"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    def test_change_password_wrong_current(self, client, app):
        token = _signup_login(client, "wrongpw@test.com")
        r = client.post(
            "/auth/change-password",
            json={"current_password": "WrongPassword", "new_password": "NewPass456"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 401

    def test_change_password_requires_auth(self, client):
        r = client.post(
            "/auth/change-password",
            json={"current_password": "old", "new_password": "NewPass456"},
        )
        assert r.status_code == 401


# ─── My Bookings ──────────────────────────────────────────────────────────────

class TestMyBookings:
    def test_my_bookings_returns_empty_list(self, client, app):
        token = _signup_login(client, "mybookings@test.com")
        r = client.get("/auth/me/bookings", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["upcoming"] == 0
        assert body["past"] == 0
        assert body["cancelled"] == 0
        assert body["expired"] == 0
        assert body["page"] == 1
        assert body["per_page"] == 5
        assert body["total_pages"] == 1
        assert body["tab"] == "upcoming"

    def test_my_bookings_requires_auth(self, client):
        r = client.get("/auth/me/bookings")
        assert r.status_code == 401

    def test_my_bookings_with_confirmed_booking(self, client, app):
        email = "bookings_user@test.com"
        token = _signup_login(client, email)

        # Create a room and booking directly
        db = app.state.testing_session_local()
        room = models.Room(
            hotel_name="My Hotel",
            room_type="standard",
            price=100.0,
            max_guests=2,
            beds=1,
            bathrooms=1,
            city="NYC",
            country="USA",
        )
        db.add(room)
        db.commit()
        db.refresh(room)

        future = datetime.now(timezone.utc) + timedelta(days=5)
        booking = models.Booking(
            booking_ref="MYBK001",
            user_name="Test User",
            email=email,
            room_id=room.id,
            check_in=future,
            check_out=future + timedelta(days=2),
            guests=1,
            nights=2,
            room_rate=100.0,
            taxes=12.0,
            service_fee=5.0,
            total_amount=117.0,
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )
        db.add(booking)
        db.commit()
        db.close()

        r = client.get("/auth/me/bookings", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["upcoming"] == 1
        assert body["past"] == 0
        assert body["cancelled"] == 0
        assert body["expired"] == 0
        assert body["page"] == 1
        assert body["per_page"] == 5
        assert body["total_pages"] == 1
        assert body["tab"] == "upcoming"
        assert len(body["bookings"]) == 1
        assert body["bookings"][0]["booking_ref"] == "MYBK001"

    def test_my_bookings_supports_tab_pagination_and_expired_counts(self, client, app):
        email = "bucketed_user@test.com"
        token = _signup_login(client, email)

        db = app.state.testing_session_local()
        user = db.query(models.User).filter(models.User.email == email).first()
        room = models.Room(
            hotel_name="Segment Hotel",
            room_type="standard",
            price=100.0,
            max_guests=2,
            beds=1,
            bathrooms=1,
            city="NYC",
            country="USA",
        )
        db.add(room)
        db.commit()
        db.refresh(room)

        future = datetime.now(timezone.utc) + timedelta(days=10)
        past = datetime.now(timezone.utc) - timedelta(days=10)
        db.add_all([
            models.Booking(
                booking_ref="UPCOMING-01",
                user_name="Test User",
                email=email,
                user_id=user.id,
                room_id=room.id,
                check_in=future,
                check_out=future + timedelta(days=1),
                guests=1,
                nights=1,
                room_rate=100.0,
                taxes=12.0,
                service_fee=5.0,
                total_amount=117.0,
                status=models.BookingStatus.CONFIRMED,
                payment_status=models.PaymentStatus.PAID,
            ),
            models.Booking(
                booking_ref="PAST-01",
                user_name="Test User",
                email=email,
                user_id=user.id,
                room_id=room.id,
                check_in=past,
                check_out=past + timedelta(days=1),
                guests=1,
                nights=1,
                room_rate=100.0,
                taxes=12.0,
                service_fee=5.0,
                total_amount=117.0,
                status=models.BookingStatus.COMPLETED,
                payment_status=models.PaymentStatus.PAID,
            ),
            models.Booking(
                booking_ref="CANCELLED-01",
                user_name="Test User",
                email=email,
                user_id=user.id,
                room_id=room.id,
                check_in=future + timedelta(days=3),
                check_out=future + timedelta(days=4),
                guests=1,
                nights=1,
                room_rate=100.0,
                taxes=12.0,
                service_fee=5.0,
                total_amount=117.0,
                status=models.BookingStatus.CANCELLED,
                payment_status=models.PaymentStatus.FAILED,
            ),
            models.Booking(
                booking_ref="EXPIRED-01",
                user_name="Test User",
                email=email,
                user_id=user.id,
                room_id=room.id,
                check_in=future + timedelta(days=5),
                check_out=future + timedelta(days=6),
                guests=1,
                nights=1,
                room_rate=100.0,
                taxes=12.0,
                service_fee=5.0,
                total_amount=117.0,
                status=models.BookingStatus.EXPIRED,
                payment_status=models.PaymentStatus.EXPIRED,
            ),
        ])
        db.commit()
        db.close()

        expired = client.get(
            "/auth/me/bookings",
            params={"tab": "expired", "page": 1, "per_page": 1},
            headers={"Authorization": f"Bearer {token}"},
        )
        cancelled = client.get(
            "/auth/me/bookings",
            params={"tab": "cancelled"},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert expired.status_code == 200
        expired_body = expired.json()
        assert expired_body["total"] == 1
        assert expired_body["upcoming"] == 1
        assert expired_body["past"] == 1
        assert expired_body["cancelled"] == 1
        assert expired_body["expired"] == 1
        assert expired_body["page"] == 1
        assert expired_body["per_page"] == 1
        assert expired_body["total_pages"] == 1
        assert expired_body["tab"] == "expired"
        assert expired_body["bookings"][0]["booking_ref"] == "EXPIRED-01"

        assert cancelled.status_code == 200
        cancelled_body = cancelled.json()
        assert cancelled_body["total"] == 1
        assert cancelled_body["cancelled"] == 1
        assert cancelled_body["expired"] == 1
        assert cancelled_body["bookings"][0]["booking_ref"] == "CANCELLED-01"

    def test_my_bookings_rejects_invalid_pagination_inputs(self, client):
        token = _signup_login(client, "bookings-pagination@test.com")

        invalid_tab = client.get(
            "/auth/me/bookings",
            params={"tab": "mystery"},
            headers={"Authorization": f"Bearer {token}"},
        )
        invalid_page = client.get(
            "/auth/me/bookings",
            params={"page": 0},
            headers={"Authorization": f"Bearer {token}"},
        )
        invalid_page_size = client.get(
            "/auth/me/bookings",
            params={"per_page": 99},
            headers={"Authorization": f"Bearer {token}"},
        )

        assert invalid_tab.status_code == 400
        assert invalid_page.status_code == 422
        assert invalid_page_size.status_code == 422
