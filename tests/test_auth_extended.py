"""Tests for extended auth endpoints: forgot/reset password, social login, profile, my bookings."""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_bootstrap.db")

import models
from database import Base, get_db
from routers import auth, bookings, payments, rooms
from services.rate_limit_service import reset_rate_limits


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


def _signup_login(client: TestClient, email: str = "user@test.com", password: str = "TestPass123") -> str:
    reset_rate_limits()
    client.post("/auth/signup", json={"email": email, "full_name": "Test User", "password": password})
    resp = client.post("/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


# ─── Forgot Password ──────────────────────────────────────────────────────────

class TestForgotPassword:
    def test_always_returns_200_for_unknown_email(self, client):
        reset_rate_limits()
        r = client.post("/auth/forgot-password", json={"email": "ghost@test.com"})
        assert r.status_code == 200
        assert "reset link" in r.json()["message"].lower()

    def test_creates_reset_token_for_known_email(self, client, app):
        reset_rate_limits()
        email = "resetme@test.com"
        client.post("/auth/signup", json={"email": email, "full_name": "User", "password": "TestPass123"})
        r = client.post("/auth/forgot-password", json={"email": email})
        assert r.status_code == 200

        db = app.state.testing_session_local()
        user = db.query(models.User).filter(models.User.email == email).first()
        tokens = db.query(models.PasswordResetToken).filter(
            models.PasswordResetToken.user_id == user.id
        ).all()
        db.close()
        assert len(tokens) == 1

    def test_invalidates_previous_token_on_new_request(self, client, app):
        reset_rate_limits()
        email = "double@test.com"
        client.post("/auth/signup", json={"email": email, "full_name": "User", "password": "TestPass123"})
        reset_rate_limits()
        client.post("/auth/forgot-password", json={"email": email})
        reset_rate_limits()
        client.post("/auth/forgot-password", json={"email": email})

        db = app.state.testing_session_local()
        user = db.query(models.User).filter(models.User.email == email).first()
        tokens = db.query(models.PasswordResetToken).filter(
            models.PasswordResetToken.user_id == user.id,
            models.PasswordResetToken.used_at == None,  # noqa: E711
        ).count()
        db.close()
        assert tokens == 1  # only 1 active token


# ─── Reset Password ───────────────────────────────────────────────────────────

class TestResetPassword:
    def _get_raw_token(self, app, email: str) -> str:
        """Insert a fresh token and return the raw value."""
        import hashlib, secrets
        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        db = app.state.testing_session_local()
        user = db.query(models.User).filter(models.User.email == email).first()
        record = models.PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        db.add(record)
        db.commit()
        db.close()
        return raw

    def test_reset_password_success(self, client, app):
        email = "resetpw@test.com"
        _signup_login(client, email)
        raw_token = self._get_raw_token(app, email)

        r = client.post(
            "/auth/reset-password",
            json={"token": raw_token, "new_password": "NewPass999"},
        )
        assert r.status_code == 200

        # Should be able to login with new password
        reset_rate_limits()
        login_r = client.post("/auth/login", json={"email": email, "password": "NewPass999"})
        assert login_r.status_code == 200

    def test_reset_with_invalid_token(self, client):
        r = client.post(
            "/auth/reset-password",
            json={"token": "totally-fake-token", "new_password": "NewPass999"},
        )
        assert r.status_code == 400

    def test_reset_with_expired_token(self, client, app):
        import hashlib, secrets
        email = "expired@test.com"
        _signup_login(client, email)

        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        db = app.state.testing_session_local()
        user = db.query(models.User).filter(models.User.email == email).first()
        record = models.PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # already expired
        )
        db.add(record)
        db.commit()
        db.close()

        r = client.post(
            "/auth/reset-password",
            json={"token": raw, "new_password": "NewPass999"},
        )
        assert r.status_code == 400

    def test_reset_with_already_used_token(self, client, app):
        import hashlib, secrets
        email = "used@test.com"
        _signup_login(client, email)

        raw = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        db = app.state.testing_session_local()
        user = db.query(models.User).filter(models.User.email == email).first()
        record = models.PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            used_at=datetime.now(timezone.utc),  # already used
        )
        db.add(record)
        db.commit()
        db.close()

        r = client.post(
            "/auth/reset-password",
            json={"token": raw, "new_password": "NewPass999"},
        )
        assert r.status_code == 400


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

    def test_update_phone(self, client, app):
        token = _signup_login(client, "phone@test.com")
        r = client.put(
            "/auth/me",
            json={"phone": "+1-555-0000"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["phone"] == "+1-555-0000"

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
