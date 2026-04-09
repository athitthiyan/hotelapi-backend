"""
Security hardening tests for Stayvora backend.

Tests cover:
1. Security Headers Middleware - Verify all responses include proper security headers
2. CORS Configuration - Verify origins and HTTP methods restrictions
3. Seed Endpoint Protection - Verify endpoints are protected in production
4. OTP Validation - Verify phone OTP format and length validation
5. API Documentation - Verify docs are disabled in production
"""

import importlib
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import models
from database import Base, get_db, settings


def load_main_for_env(app_env: str):
    import main as main_module

    with patch("database.settings.app_env", app_env):
        return importlib.reload(main_module)


class TestSecurityHeaders:
    """Test security headers middleware adds required headers to all responses."""

    def test_health_endpoint_has_security_headers(self, client):
        """Verify /health endpoint includes all security headers."""
        response = client.get("/health")

        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_root_endpoint_has_security_headers(self, client):
        """Verify / endpoint includes all security headers."""
        response = client.get("/")

        assert response.status_code == 200
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_hsts_header_present(self, client):
        """Verify Strict-Transport-Security header is present."""
        response = client.get("/health")

        assert response.status_code == 200
        hsts = response.headers.get("Strict-Transport-Security")
        assert hsts is not None
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    def test_security_headers_on_error_response(self, client):
        """Verify security headers are included even on 404 responses."""
        response = client.get("/nonexistent-endpoint")

        assert response.status_code == 404
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


class TestCORSConfiguration:
    """Test CORS configuration allows only approved origins and methods."""

    def test_localhost_dev_ports_allowed(self, client):
        """Verify localhost development ports are in CORS origins."""
        # This test verifies configuration by checking that valid origins
        # would be accepted. We test with a preflight request.
        response = client.options(
            "/health",
            headers={
                "Origin": "http://localhost:4200",
                "Access-Control-Request-Method": "GET",
            }
        )

        # If CORS is configured, the origin should be in the response
        # or the middleware should allow it
        assert response.status_code in [200, 404]  # 404 is ok for OPTIONS, CORS still works

    def test_wildcard_origin_not_allowed(self, client):
        """Verify wildcard '*' is not in CORS origins (use explicit origins)."""
        from main import origins

        # Wildcard origin should never be in the allowed origins list
        assert "*" not in origins
        # But specific localhost ports should be there
        assert "http://localhost:4200" in origins

    def test_only_allowed_http_methods(self, client):
        """Verify CORS preflight accepts configured HTTP methods."""
        allowed_methods = ["GET", "POST", "PUT", "DELETE", "PATCH"]

        for method in allowed_methods:
            response = client.options(
                "/health",
                headers={
                    "Origin": "http://localhost:4200",
                    "Access-Control-Request-Method": method,
                },
            )

            assert response.status_code == 200
            allow_methods = response.headers.get("access-control-allow-methods", "")
            assert method in allow_methods

    def test_stayvora_production_origins_included(self, client):
        """Verify production Stayvora origins are hardcoded."""
        from main import _HARDCODED_ORIGINS

        # Verify critical production domains are hardcoded
        assert "https://stayvora.co.in" in _HARDCODED_ORIGINS
        assert "https://www.stayvora.co.in" in _HARDCODED_ORIGINS
        assert "https://pay.stayvora.co.in" in _HARDCODED_ORIGINS
        assert "https://admin.stayvora.co.in" in _HARDCODED_ORIGINS


class TestSeedEndpointProtection:
    """Test that seed endpoints are protected in production."""

    def test_seed_endpoint_blocked_in_production(self, tmp_path, app):
        """Verify POST /seed returns 403 in production environment."""
        # Patch settings to simulate production
        with patch("main.settings.app_env", "production"):
            db_path = tmp_path / "test_prod.db"
            engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
            )
            TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Base.metadata.create_all(bind=engine)

            # Override the app's get_db dependency
            def override_get_db():
                db = TestingSessionLocal()
                try:
                    yield db
                finally:
                    db.close()

            app.dependency_overrides[get_db] = override_get_db

            client = TestClient(app)

            response = client.post("/seed")

            assert response.status_code == 403
            assert "disabled in production" in response.json()["detail"].lower()

    def test_seed_endpoint_accessible_in_development(self, tmp_path):
        """Verify POST /seed is accessible when not in production."""
        development_main = load_main_for_env("development")
        try:
            db_path = tmp_path / "test_dev_seed.db"
            engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
            )
            TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Base.metadata.create_all(bind=engine)
            original_session_local = development_main.SessionLocal

            def override_get_db():
                db = TestingSessionLocal()
                try:
                    yield db
                finally:
                    db.close()

            development_main.app.dependency_overrides[get_db] = override_get_db
            development_main.SessionLocal = TestingSessionLocal
            client = TestClient(development_main.app)
            response = client.post("/seed")

            # Should either succeed or fail with data errors, not 403
            assert response.status_code != 403
        finally:
            if "original_session_local" in locals():
                development_main.SessionLocal = original_session_local
            load_main_for_env(settings.app_env)

    def test_backfill_coordinates_blocked_in_production(self, tmp_path, app):
        """Verify POST /seed/backfill-coordinates returns 403 in production."""
        with patch("main.settings.app_env", "production"):
            db_path = tmp_path / "test_prod_backfill.db"
            engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
            )
            TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Base.metadata.create_all(bind=engine)

            def override_get_db():
                db = TestingSessionLocal()
                try:
                    yield db
                finally:
                    db.close()

            app.dependency_overrides[get_db] = override_get_db

            client = TestClient(app)

            response = client.post("/seed/backfill-coordinates")

            assert response.status_code == 403
            assert "disabled in production" in response.json()["detail"].lower()

    def test_backfill_coordinates_accessible_in_development(self, tmp_path):
        """Verify POST /seed/backfill-coordinates is accessible in development."""
        development_main = load_main_for_env("development")
        try:
            db_path = tmp_path / "test_dev_backfill.db"
            engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False},
            )
            TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
            Base.metadata.create_all(bind=engine)
            original_session_local = development_main.SessionLocal

            def override_get_db():
                db = TestingSessionLocal()
                try:
                    yield db
                finally:
                    db.close()

            development_main.app.dependency_overrides[get_db] = override_get_db
            development_main.SessionLocal = TestingSessionLocal
            client = TestClient(development_main.app)
            response = client.post("/seed/backfill-coordinates")

            # Should either succeed or fail with data errors, not 403
            assert response.status_code != 403
        finally:
            if "original_session_local" in locals():
                development_main.SessionLocal = original_session_local
            load_main_for_env(settings.app_env)


class TestOTPValidation:
    """Test OTP validation enforces strict format and length requirements."""

    def test_phone_otp_with_non_numeric_characters_rejected(self, client, db_session):
        """Verify OTP with non-numeric characters is rejected."""
        # Create a test user
        from routers import auth as auth_module
        user = models.User(
            email="otp-test@example.com",
            full_name="OTP Test",
            hashed_password=auth_module.hash_password("password123"),
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        # Get access token
        token = auth_module.create_token(
            user, "access", __import__("datetime").timedelta(hours=1)
        )

        # Attempt to verify with non-numeric OTP
        response = client.post(
            "/auth/phone/verify",
            json={
                "phone": "9876543210",
                "otp": "12a4",  # Contains 'a' - non-numeric
            },
            headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 400
        assert "numeric digits" in response.json()["detail"].lower()

    def test_phone_otp_shorter_than_4_digits_rejected(self, client, db_session):
        """Verify OTP shorter than 4 digits is rejected."""
        from routers import auth as auth_module
        user = models.User(
            email="otp-short@example.com",
            full_name="OTP Short Test",
            hashed_password=auth_module.hash_password("password123"),
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        token = auth_module.create_token(
            user, "access", __import__("datetime").timedelta(hours=1)
        )

        # Attempt with 3-digit OTP
        response = client.post(
            "/auth/phone/verify",
            json={
                "phone": "9876543210",
                "otp": "123",  # Only 3 digits
            },
            headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 400
        assert "4-6" in response.json()["detail"]

    def test_phone_otp_longer_than_6_digits_rejected(self, client, db_session):
        """Verify OTP longer than 6 digits is rejected."""
        from routers import auth as auth_module
        user = models.User(
            email="otp-long@example.com",
            full_name="OTP Long Test",
            hashed_password=auth_module.hash_password("password123"),
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        token = auth_module.create_token(
            user, "access", __import__("datetime").timedelta(hours=1)
        )

        # Attempt with 7-digit OTP
        response = client.post(
            "/auth/phone/verify",
            json={
                "phone": "9876543210",
                "otp": "1234567",  # 7 digits
            },
            headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 400
        assert "4-6" in response.json()["detail"]

    def test_phone_otp_empty_string_rejected(self, client, db_session):
        """Verify empty OTP string is rejected."""
        from routers import auth as auth_module
        user = models.User(
            email="otp-empty@example.com",
            full_name="OTP Empty Test",
            hashed_password=auth_module.hash_password("password123"),
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        token = auth_module.create_token(
            user, "access", __import__("datetime").timedelta(hours=1)
        )

        response = client.post(
            "/auth/phone/verify",
            json={
                "phone": "9876543210",
                "otp": "",  # Empty
            },
            headers={"Authorization": f"Bearer {token}"}
        )

        assert response.status_code == 400
        assert "numeric digits" in response.json()["detail"].lower()

    def test_phone_otp_valid_lengths_accepted(self, client, db_session):
        """Verify OTPs of 4-6 digits are accepted for validation (even if incorrect)."""
        from routers import auth as auth_module
        from datetime import timedelta

        user = models.User(
            email="otp-valid@example.com",
            full_name="OTP Valid Test",
            hashed_password=auth_module.hash_password("password123"),
            is_active=True,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)

        token = auth_module.create_token(
            user, "access", timedelta(hours=1)
        )

        # Valid length OTPs should pass format validation (will fail on incorrect code)
        for otp in ["1234", "12345", "123456"]:
            response = client.post(
                "/auth/phone/verify",
                json={
                    "phone": "9876543210",
                    "otp": otp,
                },
                headers={"Authorization": f"Bearer {token}"}
            )

            # Should not fail due to format (status 400 with "numeric digits")
            # May fail with "expired" or "invalid code", but not format error
            if response.status_code == 400:
                assert "numeric digits" not in response.json()["detail"].lower()


class TestAPIDocumentationSecurity:
    """Test that API documentation is disabled in production."""

    def test_docs_disabled_in_production_config(self):
        """Verify docs_url is None when APP_ENV=production."""
        # Test app configuration
        from main import app

        if settings.app_env.lower() == "production":
            assert app.docs_url is None
            assert app.redoc_url is None
        else:
            # In non-production, docs should be available
            assert app.docs_url == "/docs"
            assert app.redoc_url == "/redoc"

    def test_docs_endpoint_not_accessible_in_production(self):
        """Verify /docs endpoint returns 404 in production."""
        production_main = load_main_for_env("production")
        try:
            client = TestClient(production_main.app)
            response = client.get("/docs")

            # In production with docs_url=None, endpoint should not exist
            assert response.status_code == 404
        finally:
            load_main_for_env(settings.app_env)

    def test_redoc_endpoint_not_accessible_in_production(self):
        """Verify /redoc endpoint returns 404 in production."""
        production_main = load_main_for_env("production")
        try:
            client = TestClient(production_main.app)
            response = client.get("/redoc")

            # In production with redoc_url=None, endpoint should not exist
            assert response.status_code == 404
        finally:
            load_main_for_env(settings.app_env)


class TestSecurityHeadersOnDifferentMethods:
    """Test security headers are applied consistently across HTTP methods."""

    def test_security_headers_on_post_request(self, client, db_session):
        """Verify security headers on POST requests."""
        # Create a test room for POST request
        room = models.Room(
            hotel_name="Test Hotel",
            room_type=models.RoomType.DELUXE,
            description="Test room",
            price=200.0,
            availability=True,
            city="Test City",
            country="Test Country",
        )
        db_session.add(room)
        db_session.commit()

        # Make a POST request (will fail due to auth, but headers still applied)
        response = client.post(
            "/bookings",
            json={
                "user_name": "Test",
                "email": "test@example.com",
                "phone": "1234567890",
                "room_id": room.id,
                "check_in": "2026-04-10T00:00:00Z",
                "check_out": "2026-04-11T00:00:00Z",
                "guests": 1,
            }
        )

        # Headers should be present regardless of response status
        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_security_headers_on_delete_request(self, client):
        """Verify security headers on DELETE requests."""
        # DELETE to non-existent resource
        response = client.delete("/bookings/99999")

        assert response.headers.get("X-Content-Type-Options") == "nosniff"
        assert response.headers.get("X-Frame-Options") == "DENY"
        assert response.headers.get("X-XSS-Protection") == "1; mode=block"
        assert response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
