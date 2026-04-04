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
    /payments/transactions is intentionally public so the PayFlow frontend
    (which has no user login system) can display transaction history.
    """
    signup = client.post("/auth/signup", json=signup_payload(email="nonadmin@example.com"))
    headers = auth_header(signup.json()["access_token"])

    reconciliation = client.get("/payments/admin/reconciliation", headers=headers)
    transactions = client.get("/payments/transactions", headers=headers)

    assert reconciliation.status_code == 403
    assert transactions.status_code == 200   # public — no auth required


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
