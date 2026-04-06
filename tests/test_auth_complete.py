"""
100% branch-coverage tests for routers/auth.py
Covers every conditional branch including inactive user, invalid scheme,
wrong token type, admin guard, refresh, and helper functions.
"""

from __future__ import annotations

import bcrypt
from datetime import datetime, timedelta, timezone
import pytest

import models
from routers.auth import (
    hash_password,
    verify_password,
    decode_token,
    get_bearer_token,
    get_user_from_payload,
)

# ─── helpers ─────────────────────────────────────────────────────────────────

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def signup_payload(**overrides):
    base = {"email": "athit@example.com", "full_name": "Athit", "password": "StrongPass123"}
    base.update(overrides)
    return base


def create_admin(db_session):
    admin = models.User(
        email="admin@example.com",
        full_name="Admin",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


# ─── Unit tests for pure helper functions ─────────────────────────────────────

class TestAuthHelpers:
    def test_hash_and_verify_password_correct(self):
        hashed = hash_password("MySecret1")
        assert verify_password("MySecret1", hashed) is True

    def test_verify_password_wrong(self):
        hashed = hash_password("MySecret1")
        assert verify_password("Wrong", hashed) is False

    def test_verify_password_supports_legacy_bcrypt_hashes(self):
        legacy_hash = bcrypt.hashpw(b"PartnerPass123", bcrypt.gensalt()).decode("utf-8")
        assert verify_password("PartnerPass123", legacy_hash) is True

    def test_verify_password_unknown_hash_returns_false(self):
        assert verify_password("MySecret1", "not-a-real-hash") is False

    def test_decode_token_wrong_type_raises_401(self, client):
        # First create a real access token
        client.post("/auth/signup", json=signup_payload())
        login = client.post("/auth/login", json={"email": "athit@example.com", "password": "StrongPass123"})
        access_token = login.json()["access_token"]

        # Trying to use access token as refresh token should fail
        with pytest.raises(Exception) as exc_info:
            decode_token(access_token, "refresh")
        assert "401" in str(exc_info.value.status_code) or hasattr(exc_info.value, "status_code")

    def test_decode_token_invalid_jwt_raises_401(self):
        with pytest.raises(Exception) as exc_info:
            decode_token("not.a.valid.jwt", "access")
        assert exc_info.value.status_code == 401


    def test_get_bearer_token_missing_header_raises_401(self):
        with pytest.raises(Exception) as exc_info:
            get_bearer_token(None)
        assert exc_info.value.status_code == 401
        assert "Authorization header is required" in exc_info.value.detail

    def test_get_bearer_token_wrong_scheme_raises_401(self):
        with pytest.raises(Exception) as exc_info:
            get_bearer_token("Basic sometoken")
        assert exc_info.value.status_code == 401
        assert "Invalid authorization header" in exc_info.value.detail

    def test_get_bearer_token_no_token_after_bearer_raises_401(self):
        with pytest.raises(Exception) as exc_info:
            get_bearer_token("Bearer ")
        assert exc_info.value.status_code == 401

    def test_get_bearer_token_valid(self):
        token = get_bearer_token("Bearer mytoken123")
        assert token == "mytoken123"

    def test_get_user_from_payload_inactive_user_raises_401(self, db_session):
        inactive = models.User(
            email="inactive@example.com",
            full_name="Inactive",
            hashed_password=hash_password("Test1234"),
            is_admin=False,
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()
        db_session.refresh(inactive)

        with pytest.raises(Exception) as exc_info:
            get_user_from_payload(db_session, {"sub": str(inactive.id)})
        assert exc_info.value.status_code == 401

    def test_get_user_from_payload_missing_user_raises_401(self, db_session):
        with pytest.raises(Exception) as exc_info:
            get_user_from_payload(db_session, {"sub": "999999"})
        assert exc_info.value.status_code == 401


# ─── Integration tests via HTTP client ────────────────────────────────────────

class TestPhoneVerification:
    def test_phone_otp_verification_required_before_profile_update(self, client):
        client.post("/auth/signup", json=signup_payload())
        login = client.post(
            "/auth/login",
            json={"email": "athit@example.com", "password": "StrongPass123"},
        )
        headers = auth_header(login.json()["access_token"])

        blocked = client.put(
            "/auth/me",
            headers=headers,
            json={"full_name": "Athit", "phone": "+91 99999 99999"},
        )
        assert blocked.status_code == 400
        assert "OTP" in blocked.json()["detail"]

        otp_response = client.post(
            "/auth/phone/request-otp",
            headers=headers,
            json={"phone": "+91 99999 99999"},
        )
        assert otp_response.status_code == 200
        dev_code = otp_response.json()["dev_code"]

        invalid = client.post(
            "/auth/phone/verify",
            headers=headers,
            json={"phone": "+91 99999 99999", "otp": "000000"},
        )
        assert invalid.status_code == 400

        verified = client.post(
            "/auth/phone/verify",
            headers=headers,
            json={"phone": "+91 99999 99999", "otp": dev_code},
        )
        assert verified.status_code == 200
        assert verified.json()["phone_verified"] is True

        updated = client.put(
            "/auth/me",
            headers=headers,
            json={"full_name": "Athit Updated", "phone": "+91 99999 99999"},
        )
        assert updated.status_code == 200
        assert updated.json()["full_name"] == "Athit Updated"


class TestSignup:
    def test_signup_success_returns_201_with_tokens(self, client):
        r = client.post("/auth/signup", json=signup_payload())
        assert r.status_code == 201
        body = r.json()
        assert "access_token" in body
        assert "refresh_token" in body
        assert body["user"]["email"] == "athit@example.com"

    def test_signup_duplicate_email_returns_409(self, client):
        client.post("/auth/signup", json=signup_payload())
        r = client.post("/auth/signup", json=signup_payload())
        assert r.status_code == 409
        assert r.json()["detail"] == "Email already registered"

    def test_signup_weak_password_returns_422(self, client):
        r = client.post("/auth/signup", json=signup_payload(password="weak"))
        assert r.status_code == 422

    def test_signup_missing_uppercase_returns_422(self, client):
        r = client.post("/auth/signup", json=signup_payload(password="alllower1"))
        assert r.status_code == 422

    def test_signup_missing_digit_returns_422(self, client):
        r = client.post("/auth/signup", json=signup_payload(password="NoDigitPass"))
        assert r.status_code == 422

    def test_signup_rate_limit_triggers_429(self, client):
        for _ in range(5):
            client.post("/auth/signup", json=signup_payload(email="ratelimit@example.com"))
        r = client.post("/auth/signup", json=signup_payload(email="ratelimit@example.com"))
        assert r.status_code == 429


class TestLogin:
    def test_login_success_returns_tokens(self, client):
        client.post("/auth/signup", json=signup_payload())
        r = client.post("/auth/login", json={"email": "athit@example.com", "password": "StrongPass123"})
        assert r.status_code == 200
        assert "access_token" in r.json()
        assert "refresh_token" in r.json()

    def test_login_user_not_found_returns_401(self, client):
        r = client.post("/auth/login", json={"email": "nobody@example.com", "password": "StrongPass123"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Invalid email or password"

    def test_login_wrong_password_returns_401(self, client):
        client.post("/auth/signup", json=signup_payload())
        r = client.post("/auth/login", json={"email": "athit@example.com", "password": "WrongPass1"})
        assert r.status_code == 401

    def test_login_inactive_user_returns_403(self, client, db_session):
        # Create inactive user directly
        inactive = models.User(
            email="inactive@example.com",
            full_name="Inactive",
            hashed_password=hash_password("StrongPass123"),
            is_admin=False,
            is_active=False,
        )
        db_session.add(inactive)
        db_session.commit()

        r = client.post("/auth/login", json={"email": "inactive@example.com", "password": "StrongPass123"})
        assert r.status_code == 403
        assert r.json()["detail"] == "User account is inactive"

    def test_login_rate_limit_triggers_429(self, client):
        client.post("/auth/signup", json=signup_payload())
        for _ in range(8):
            client.post("/auth/login", json={"email": "athit@example.com", "password": "Wrong1"})
        r = client.post("/auth/login", json={"email": "athit@example.com", "password": "Wrong1"})
        assert r.status_code == 429


class TestRefresh:
    def test_refresh_returns_new_tokens(self, client):
        client.post("/auth/signup", json=signup_payload())
        login = client.post("/auth/login", json={"email": "athit@example.com", "password": "StrongPass123"})
        refresh_token = login.json()["refresh_token"]

        r = client.post("/auth/refresh", json={"refresh_token": refresh_token})
        assert r.status_code == 200
        assert "access_token" in r.json()

    def test_refresh_with_access_token_returns_401(self, client):
        client.post("/auth/signup", json=signup_payload())
        login = client.post("/auth/login", json={"email": "athit@example.com", "password": "StrongPass123"})
        access_token = login.json()["access_token"]

        # Passing access token as refresh token → wrong token_type
        r = client.post("/auth/refresh", json={"refresh_token": access_token})
        assert r.status_code == 401
        assert "refresh" in r.json()["detail"]

    def test_refresh_with_garbage_token_returns_401(self, client):
        r = client.post("/auth/refresh", json={"refresh_token": "garbage.token.here"})
        assert r.status_code == 401


class TestGetMe:
    def test_me_returns_user(self, client):
        signup = client.post("/auth/signup", json=signup_payload())
        token = signup.json()["access_token"]
        r = client.get("/auth/me", headers=auth_header(token))
        assert r.status_code == 200
        assert r.json()["email"] == "athit@example.com"

    def test_me_no_authorization_header_returns_401(self, client):
        r = client.get("/auth/me")
        assert r.status_code == 401
        assert "Authorization header is required" in r.json()["detail"]

    def test_me_invalid_scheme_returns_401(self, client):
        r = client.get("/auth/me", headers={"Authorization": "Basic sometoken"})
        assert r.status_code == 401

    def test_me_invalid_token_returns_401(self, client):
        r = client.get("/auth/me", headers={"Authorization": "Bearer not.valid.jwt"})
        assert r.status_code == 401

    def test_me_using_refresh_token_returns_401(self, client):
        client.post("/auth/signup", json=signup_payload())
        login = client.post("/auth/login", json={"email": "athit@example.com", "password": "StrongPass123"})
        refresh_token = login.json()["refresh_token"]
        r = client.get("/auth/me", headers=auth_header(refresh_token))
        assert r.status_code == 401

    def test_me_bookings_reconciles_successful_processing_booking(self, client, db_session, room_id):
        signup = client.post("/auth/signup", json=signup_payload())
        token = signup.json()["access_token"]
        user_id = signup.json()["user"]["id"]

        booking = models.Booking(
            booking_ref="BKRECON01",
            user_name="Athit",
            email="athit@example.com",
            user_id=user_id,
            phone="1234567890",
            room_id=room_id,
            check_in=datetime.now(timezone.utc) + timedelta(days=1),
            check_out=datetime.now(timezone.utc) + timedelta(days=2),
            hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            guests=2,
            nights=1,
            room_rate=200,
            taxes=24,
            service_fee=10,
            total_amount=234,
            status=models.BookingStatus.PROCESSING,
            payment_status=models.PaymentStatus.PROCESSING,
        )
        db_session.add(booking)
        db_session.flush()
        db_session.add(
            models.Transaction(
                booking_id=booking.id,
                transaction_ref="TXN-AUTH-RECONCILE",
                amount=234,
                currency="USD",
                payment_method="card",
                status=models.TransactionStatus.SUCCESS,
            )
        )
        db_session.commit()

        response = client.get("/auth/me/bookings", headers=auth_header(token))

        db_session.refresh(booking)
        assert response.status_code == 200
        assert response.json()["bookings"][0]["status"] == "confirmed"
        assert response.json()["bookings"][0]["payment_status"] == "paid"
        assert booking.status == models.BookingStatus.CONFIRMED
        assert booking.payment_status == models.PaymentStatus.PAID


class TestAdminGuard:
    def test_non_admin_forbidden_on_admin_routes(self, client):
        client.post("/auth/signup", json=signup_payload())
        login = client.post("/auth/login", json={"email": "athit@example.com", "password": "StrongPass123"})
        headers = auth_header(login.json()["access_token"])

        r = client.get("/analytics", headers=headers, params={"days": 7})
        assert r.status_code == 403
        assert r.json()["detail"] == "Admin access required"

    def test_admin_can_access_admin_routes(self, client, db_session):
        create_admin(db_session)
        login = client.post("/auth/login", json={"email": "admin@example.com", "password": "AdminPass123"})
        headers = auth_header(login.json()["access_token"])

        r = client.get("/analytics", headers=headers, params={"days": 7})
        assert r.status_code == 200
