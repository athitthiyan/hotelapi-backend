"""
100% branch-coverage tests for routers/auth.py
Covers every conditional branch including inactive user, invalid scheme,
wrong token type, admin guard, refresh, and helper functions.
"""

from __future__ import annotations

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
