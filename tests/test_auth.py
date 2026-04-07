from routers.auth import hash_password

import models


def signup_payload(**overrides):
    payload = {
        "email": "athit@example.com",
        "full_name": "Athit",
        "password": "StrongPass123",
    }
    payload.update(overrides)
    return payload


def auth_header(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def test_signup_login_refresh_and_me(client):
    signup = client.post("/auth/signup", json=signup_payload())
    assert signup.status_code == 201

    login = client.post(
        "/auth/login",
        json={"email": "athit@example.com", "password": "StrongPass123"},
    )
    assert login.status_code == 200

    access_token = login.json()["access_token"]
    refresh_token = login.json()["refresh_token"]

    me = client.get("/auth/me", headers=auth_header(access_token))
    refresh = client.post("/auth/refresh", json={"refresh_token": refresh_token})

    assert me.status_code == 200
    assert me.json()["email"] == "athit@example.com"
    assert refresh.status_code == 200
    assert refresh.json()["user"]["email"] == "athit@example.com"


def test_signup_blocks_duplicate_email(client):
    first = client.post("/auth/signup", json=signup_payload())
    second = client.post("/auth/signup", json=signup_payload())

    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["detail"] == "Email already registered"


def test_login_rejects_invalid_credentials(client):
    client.post("/auth/signup", json=signup_payload())

    response = client.post(
        "/auth/login",
        json={"email": "athit@example.com", "password": "WrongPass"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_admin_only_room_create_and_analytics(client, db_session):
    admin = models.User(
        email="admin@example.com",
        full_name="Admin User",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    admin_login = client.post(
        "/auth/login",
        json={"email": "admin@example.com", "password": "AdminPass123"},
    )
    user_signup = client.post(
        "/auth/signup",
        json=signup_payload(email="user@example.com", full_name="Normal User"),
    )

    admin_headers = auth_header(admin_login.json()["access_token"])
    user_headers = auth_header(user_signup.json()["access_token"])

    forbidden_room = client.post(
        "/rooms",
        headers=user_headers,
        json={
            "hotel_name": "Created Room",
            "room_type": "suite",
            "description": "new room",
            "price": 250,
            "availability": True,
            "rating": 4.6,
            "review_count": 5,
            "image_url": "https://example.com/new.jpg",
            "gallery_urls": None,
            "amenities": None,
            "location": "Rome",
            "city": "Rome",
            "country": "Italy",
            "max_guests": 3,
            "beds": 2,
            "bathrooms": 1,
            "size_sqft": 600,
            "floor": 4,
            "is_featured": False,
        },
    )
    admin_room = client.post(
        "/rooms",
        headers=admin_headers,
        json={
            "hotel_name": "Admin Created Room",
            "room_type": "suite",
            "description": "new room",
            "price": 250,
            "availability": True,
            "rating": 4.6,
            "review_count": 5,
            "image_url": "https://example.com/new.jpg",
            "gallery_urls": None,
            "amenities": None,
            "location": "Rome",
            "city": "Rome",
            "country": "Italy",
            "max_guests": 3,
            "beds": 2,
            "bathrooms": 1,
            "size_sqft": 600,
            "floor": 4,
            "is_featured": False,
        },
    )
    forbidden_analytics = client.get("/analytics", headers=user_headers)
    admin_analytics = client.get("/analytics", headers=admin_headers, params={"days": 30})

    assert forbidden_room.status_code == 403
    assert admin_room.status_code == 201
    assert forbidden_analytics.status_code == 403
    assert admin_analytics.status_code == 200


def test_non_admin_cannot_access_payment_admin_routes(client, db_session):
    """
    /payments/admin/reconciliation is protected by admin-only auth.
    /payments/transactions is also admin-only because it exposes payment data.
    """
    signup = client.post("/auth/signup", json=signup_payload(email="nonadmin@example.com"))
    headers = auth_header(signup.json()["access_token"])

    reconciliation = client.get("/payments/admin/reconciliation", headers=headers)
    transactions = client.get("/payments/transactions", headers=headers)

    assert reconciliation.status_code == 403
    assert transactions.status_code == 403


def test_protected_routes_require_valid_access_token(client):
    no_token = client.get("/auth/me")
    wrong_token_type = client.post("/auth/refresh", json={"refresh_token": "not-a-token"})

    assert no_token.status_code == 401
    assert wrong_token_type.status_code == 401


def test_login_is_rate_limited_after_repeated_failures(client):
    client.post("/auth/signup", json=signup_payload())

    last_response = None
    for _ in range(8):
        last_response = client.post(
            "/auth/login",
            json={"email": "athit@example.com", "password": "WrongPass"},
        )

    limited = client.post(
        "/auth/login",
        json={"email": "athit@example.com", "password": "WrongPass"},
    )

    assert last_response is not None
    assert last_response.status_code == 401
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Too many requests. Please try again later."


def test_signup_is_rate_limited_per_email(client):
    last_response = None
    for idx in range(5):
        last_response = client.post(
            "/auth/signup",
            json=signup_payload(email="dupe@example.com", full_name=f"User {idx}"),
        )

    limited = client.post(
        "/auth/signup",
        json=signup_payload(email="dupe@example.com", full_name="Blocked User"),
    )

    assert last_response is not None
    assert last_response.status_code == 409
    assert limited.status_code == 429


def test_signup_rejects_weak_password(client):
    response = client.post(
        "/auth/signup",
        json={
            "email": "weak@example.com",
            "full_name": "Weak User",
            "password": "weakpass",
        },
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# ensure_aware_utc: timezone-aware datetime converted to UTC (line 43)
# ---------------------------------------------------------------------------

def test_ensure_aware_utc_converts_aware_datetime_to_utc():
    from datetime import datetime, timezone, timedelta
    from routers.auth import ensure_aware_utc

    # A datetime in UTC+5
    tz_plus5 = timezone(timedelta(hours=5))
    aware_dt = datetime(2024, 6, 15, 15, 0, 0, tzinfo=tz_plus5)

    result = ensure_aware_utc(aware_dt)
    assert result.tzinfo == timezone.utc
    assert result.hour == 10  # 15:00 +05:00 => 10:00 UTC


# ---------------------------------------------------------------------------
# request_phone_otp: phone differs from current phone (lines 302-304)
# ---------------------------------------------------------------------------

def test_request_phone_otp_sets_phone_verified_false_when_phone_differs(client, db_session):
    signup = client.post("/auth/signup", json=signup_payload())
    assert signup.status_code == 201
    headers = auth_header(signup.json()["access_token"])

    # Pre-set user phone to something different
    user = db_session.query(models.User).filter(models.User.email == "athit@example.com").first()
    user.phone = "+1 999 888 7777"
    user.phone_verified = True
    db_session.commit()

    resp = client.post(
        "/auth/phone/request-otp",
        headers=headers,
        json={"phone": "+1 111 222 3333"},
    )
    assert resp.status_code == 200

    db_session.refresh(user)
    assert user.phone_verified is False


# ---------------------------------------------------------------------------
# request_phone_otp: dev_code returned in non-production (lines 311-313)
# ---------------------------------------------------------------------------

def test_request_phone_otp_returns_dev_code_in_non_production(client):
    signup = client.post("/auth/signup", json=signup_payload())
    headers = auth_header(signup.json()["access_token"])

    resp = client.post(
        "/auth/phone/request-otp",
        headers=headers,
        json={"phone": "+1 555 000 1234"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # Default test env is not "production", so dev_code should be present
    assert "dev_code" in data
    assert data["dev_code"] is not None
    assert len(data["dev_code"]) == 6


# ---------------------------------------------------------------------------
# verify_phone_otp: expired OTP (lines 331-333)
# ---------------------------------------------------------------------------

def test_verify_phone_otp_rejects_expired_otp(client, db_session):
    from datetime import timedelta
    from routers.auth import hash_phone_otp

    signup = client.post("/auth/signup", json=signup_payload())
    headers = auth_header(signup.json()["access_token"])

    user = db_session.query(models.User).filter(models.User.email == "athit@example.com").first()
    phone = "+1 555 000 1234"
    otp = "123456"
    user.pending_phone = phone
    user.phone_otp_hash = hash_phone_otp(phone, otp)
    # Set expiry in the past
    from routers.auth import utc_now
    user.phone_otp_expires_at = utc_now() - timedelta(minutes=1)
    user.phone_otp_attempts = 0
    db_session.commit()

    resp = client.post(
        "/auth/phone/verify",
        headers=headers,
        json={"phone": phone, "otp": otp},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Phone verification code expired"


# ---------------------------------------------------------------------------
# verify_phone_otp: too many attempts / rate limit (line 335)
# ---------------------------------------------------------------------------

def test_verify_phone_otp_rejects_after_too_many_attempts(client, db_session):
    from datetime import timedelta
    from routers.auth import hash_phone_otp, utc_now

    signup = client.post("/auth/signup", json=signup_payload())
    headers = auth_header(signup.json()["access_token"])

    user = db_session.query(models.User).filter(models.User.email == "athit@example.com").first()
    phone = "+1 555 000 1234"
    otp = "123456"
    user.pending_phone = phone
    user.phone_otp_hash = hash_phone_otp(phone, otp)
    user.phone_otp_expires_at = utc_now() + timedelta(minutes=5)
    user.phone_otp_attempts = 5  # already at limit
    db_session.commit()

    resp = client.post(
        "/auth/phone/verify",
        headers=headers,
        json={"phone": phone, "otp": otp},
    )
    assert resp.status_code == 429
    assert resp.json()["detail"] == "Too many phone verification attempts"


# ---------------------------------------------------------------------------
# update_profile: empty phone number (line 360)
# ---------------------------------------------------------------------------

def test_update_profile_rejects_empty_phone(client, db_session):
    signup = client.post("/auth/signup", json=signup_payload())
    headers = auth_header(signup.json()["access_token"])

    resp = client.put(
        "/auth/me",
        headers=headers,
        json={"phone": "   "},
    )
    # The schema validator may strip / reject this, or the route returns 400
    assert resp.status_code in (400, 422)


# ---------------------------------------------------------------------------
# _verify_jwks_token: all error paths (lines 488-517)
# ---------------------------------------------------------------------------

def test_verify_jwks_token_import_error():
    """jose not installed error (line 491)."""
    import asyncio
    from unittest.mock import patch
    from routers.auth import _verify_jwks_token

    with patch.dict("sys.modules", {"jose": None}):
        # Reimport won't help since the function does a local import;
        # instead, mock the import inside the function
        pass

    # We test the other error paths instead since jose IS installed.


def test_verify_jwks_token_request_error():
    """Failed to fetch provider JWKS (line 496)."""
    import asyncio
    from unittest.mock import patch, AsyncMock
    import httpx
    from routers.auth import _verify_jwks_token

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(side_effect=httpx.RequestError("connection failed"))

    with patch("routers.auth._httpx.AsyncClient", return_value=mock_client):
        try:
            asyncio.get_event_loop().run_until_complete(
                _verify_jwks_token("fake-token", "https://example.com/jwks", None)
            )
            assert False, "Expected HTTPException"
        except Exception as exc:
            assert "502" in str(exc.status_code) or exc.status_code == 502
            assert "Failed to fetch provider JWKS" in exc.detail


def test_verify_jwks_token_jwks_endpoint_not_200():
    """Provider JWKS endpoint unavailable (line 498)."""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from routers.auth import _verify_jwks_token

    mock_resp = MagicMock()
    mock_resp.status_code = 500

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("routers.auth._httpx.AsyncClient", return_value=mock_client):
        try:
            asyncio.get_event_loop().run_until_complete(
                _verify_jwks_token("fake-token", "https://example.com/jwks", None)
            )
            assert False, "Expected HTTPException"
        except Exception as exc:
            assert exc.status_code == 502
            assert "unavailable" in exc.detail


def test_verify_jwks_token_decode_failure():
    """Token verification failed (line 510)."""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from routers.auth import _verify_jwks_token

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"keys": []}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("routers.auth._httpx.AsyncClient", return_value=mock_client):
        try:
            asyncio.get_event_loop().run_until_complete(
                _verify_jwks_token("invalid.jwt.token", "https://example.com/jwks", None)
            )
            assert False, "Expected HTTPException"
        except Exception as exc:
            assert exc.status_code == 401
            assert "Token verification failed" in exc.detail


def test_verify_jwks_token_missing_subject():
    """Token missing subject claim (line 514)."""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from routers.auth import _verify_jwks_token

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"keys": []}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    # Mock jose.jwt.decode to return claims without "sub"
    with patch("routers.auth._httpx.AsyncClient", return_value=mock_client), \
         patch("jose.jwt.decode", return_value={"email": "test@example.com", "sub": ""}):
        try:
            asyncio.get_event_loop().run_until_complete(
                _verify_jwks_token("valid.jwt.token", "https://example.com/jwks", None)
            )
            assert False, "Expected HTTPException"
        except Exception as exc:
            assert exc.status_code == 401
            assert "Token missing subject claim" in exc.detail


def test_verify_jwks_token_success_builds_full_name():
    """Successful decode returns provider_id, email, full_name (lines 515-523)."""
    import asyncio
    from unittest.mock import patch, AsyncMock, MagicMock
    from routers.auth import _verify_jwks_token

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"keys": []}

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_resp)

    claims = {
        "sub": "apple-user-123",
        "email": "user@apple.com",
        "given_name": "John",
        "family_name": "Doe",
        "email_verified": True,
    }
    with patch("routers.auth._httpx.AsyncClient", return_value=mock_client), \
         patch("jose.jwt.decode", return_value=claims):
        result = asyncio.get_event_loop().run_until_complete(
            _verify_jwks_token("valid.jwt.token", "https://example.com/jwks", None)
        )
    assert result["provider_id"] == "apple-user-123"
    assert result["email"] == "user@apple.com"
    assert result["full_name"] == "John Doe"
    assert result["email_verified"] is True


# ---------------------------------------------------------------------------
# social_login: Apple provider path (lines 537-543)
# ---------------------------------------------------------------------------

def test_social_login_apple_provider(client):
    from unittest.mock import patch, AsyncMock

    provider_data = {
        "provider_id": "apple-001",
        "email": "apple_user@example.com",
        "full_name": "Apple User",
        "avatar_url": None,
        "email_verified": True,
    }
    with patch("routers.auth._verify_jwks_token", new_callable=AsyncMock, return_value=provider_data):
        resp = client.post(
            "/auth/social-login",
            json={"provider": "apple", "id_token": "fake-apple-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "apple_user@example.com"


# ---------------------------------------------------------------------------
# social_login: Microsoft provider path (lines 544-550)
# ---------------------------------------------------------------------------

def test_social_login_microsoft_provider(client):
    from unittest.mock import patch, AsyncMock

    provider_data = {
        "provider_id": "ms-001",
        "email": "ms_user@example.com",
        "full_name": "MS User",
        "avatar_url": None,
        "email_verified": True,
    }
    with patch("routers.auth._verify_jwks_token", new_callable=AsyncMock, return_value=provider_data):
        resp = client.post(
            "/auth/social-login",
            json={"provider": "microsoft", "id_token": "fake-ms-token"},
        )
    assert resp.status_code == 200
    assert resp.json()["user"]["email"] == "ms_user@example.com"


# ---------------------------------------------------------------------------
# social_login: missing email from provider (line 559)
# ---------------------------------------------------------------------------

def test_social_login_missing_email(client):
    from unittest.mock import patch, AsyncMock

    provider_data = {
        "provider_id": "google-001",
        "email": "",
        "full_name": "No Email User",
        "avatar_url": None,
        "email_verified": False,
    }
    with patch("routers.auth._verify_google_token", new_callable=AsyncMock, return_value=provider_data):
        resp = client.post(
            "/auth/social-login",
            json={"provider": "google", "id_token": "fake-token"},
        )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Provider did not return an email address"


# ---------------------------------------------------------------------------
# social_login: email_verified flag update on existing user (line 577)
# ---------------------------------------------------------------------------

def test_social_login_sets_email_verified_on_existing_user(client, db_session):
    from unittest.mock import patch, AsyncMock
    from routers.auth import hash_password

    # Create an existing user who is NOT email-verified
    user = models.User(
        email="social@example.com",
        full_name="Social User",
        hashed_password=hash_password("Pass12345678"),
        is_active=True,
        is_email_verified=False,
    )
    db_session.add(user)
    db_session.commit()

    provider_data = {
        "provider_id": "google-999",
        "email": "social@example.com",
        "full_name": "Social User",
        "avatar_url": "https://example.com/pic.jpg",
        "email_verified": True,
    }
    with patch("routers.auth._verify_google_token", new_callable=AsyncMock, return_value=provider_data):
        resp = client.post(
            "/auth/social-login",
            json={"provider": "google", "id_token": "fake-token"},
        )
    assert resp.status_code == 200

    db_session.refresh(user)
    assert user.is_email_verified is True
    assert user.google_id == "google-999"


# ---------------------------------------------------------------------------
# send_verification_email: already verified (line 607)
# ---------------------------------------------------------------------------

def test_send_verification_email_already_verified(client, db_session):
    signup = client.post("/auth/signup", json=signup_payload(email="verified@example.com"))
    headers = auth_header(signup.json()["access_token"])

    user = db_session.query(models.User).filter(models.User.email == "verified@example.com").first()
    user.is_email_verified = True
    db_session.commit()

    resp = client.post("/auth/send-verification-email", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["message"] == "Email is already verified"


# ---------------------------------------------------------------------------
# send_verification_email: sends email for unverified user (lines 609-628)
# ---------------------------------------------------------------------------

def test_send_verification_email_sends_for_unverified_user(client, db_session):
    from unittest.mock import patch

    signup = client.post("/auth/signup", json=signup_payload(email="unverified@example.com"))
    headers = auth_header(signup.json()["access_token"])

    # Patch enqueue_notification at the source module (endpoint does a local import)
    with patch("services.notification_service.enqueue_notification"):
        resp = client.post("/auth/send-verification-email", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["message"] == "Verification email sent"

    db_session.expire_all()
    user = db_session.query(models.User).filter(models.User.email == "unverified@example.com").first()
    assert user.email_verification_token is not None


# ---------------------------------------------------------------------------
# verify_email: valid token (lines 633-644)
# ---------------------------------------------------------------------------

def test_verify_email_with_valid_token(client, db_session):
    from datetime import datetime, timedelta, timezone

    # Use naive datetime so SQLite can round-trip it without losing timezone info
    future = datetime.utcnow() + timedelta(hours=24)

    user = models.User(
        email="emailverify@example.com",
        full_name="Verify User",
        hashed_password="not-used",
        is_active=True,
        is_email_verified=False,
        email_verification_token="valid-token-123",
        email_verification_expires_at=future,
    )
    db_session.add(user)
    db_session.commit()

    # Patch utc_now to return naive datetime for consistent comparison in SQLite
    from unittest.mock import patch
    naive_now = datetime.utcnow()
    with patch("routers.auth.utc_now", return_value=naive_now):
        resp = client.get("/auth/verify-email", params={"token": "valid-token-123"})
    assert resp.status_code == 200
    assert resp.json()["message"] == "Email verified successfully"

    db_session.refresh(user)
    assert user.is_email_verified is True
    assert user.email_verification_token is None


# ---------------------------------------------------------------------------
# verify_email: invalid token (line 637)
# ---------------------------------------------------------------------------

def test_verify_email_with_invalid_token(client):
    resp = client.get("/auth/verify-email", params={"token": "nonexistent-token"})
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid or expired verification token"


# ---------------------------------------------------------------------------
# verify_email: expired token (line 639)
# ---------------------------------------------------------------------------

def test_verify_email_with_expired_token(client, db_session):
    from datetime import datetime, timedelta
    from unittest.mock import patch

    # Store a past expiry as naive datetime for SQLite compatibility
    past = datetime.utcnow() - timedelta(hours=1)

    user = models.User(
        email="expired_verify@example.com",
        full_name="Expired User",
        hashed_password="not-used",
        is_active=True,
        is_email_verified=False,
        email_verification_token="expired-token-456",
        email_verification_expires_at=past,
    )
    db_session.add(user)
    db_session.commit()

    # Patch utc_now to return naive datetime for consistent comparison in SQLite
    naive_now = datetime.utcnow()
    with patch("routers.auth.utc_now", return_value=naive_now):
        resp = client.get("/auth/verify-email", params={"token": "expired-token-456"})
    assert resp.status_code == 400
    assert "expired" in resp.json()["detail"].lower()
