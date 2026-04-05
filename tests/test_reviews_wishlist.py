"""Exhaustive tests for reviews and wishlist routers."""
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from routers import auth, bookings, payments, reviews, rooms, wishlist
from services.rate_limit_service import reset_rate_limits


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "rw_test.db"
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
    _app.include_router(reviews.router)
    _app.include_router(wishlist.router)

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


# ─── helpers ─────────────────────────────────────────────────────────────────

def _register_and_login(client: TestClient, email: str = "reviewer@test.com", password: str = "TestPass123") -> str:
    reset_rate_limits()
    client.post("/auth/signup", json={
        "email": email,
        "full_name": "Test User",
        "password": password,
    })
    resp = client.post("/auth/login", json={"email": email, "password": password})
    return resp.json()["access_token"]


def _create_room(app) -> int:
    db = app.state.testing_session_local()
    room = models.Room(
        hotel_name="Test Hotel",
        room_type="suite",
        price=200.0,
        max_guests=2,
        beds=1,
        bathrooms=1,
        city="Tokyo",
        country="Japan",
        is_featured=True,
    )
    db.add(room)
    db.commit()
    db.refresh(room)
    rid = room.id
    db.close()
    return rid


def _create_confirmed_booking(app, room_id: int, email: str) -> int:
    db = app.state.testing_session_local()
    now = datetime.now(timezone.utc)
    booking = models.Booking(
        booking_ref=f"TESTREF{uuid.uuid4().hex[:6].upper()}",
        user_name="Test User",
        email=email,
        room_id=room_id,
        check_in=now - timedelta(days=3),
        check_out=now - timedelta(days=1),
        guests=2,
        nights=2,
        room_rate=200.0,
        taxes=24.0,
        service_fee=10.0,
        total_amount=234.0,
        status=models.BookingStatus.CONFIRMED,
        payment_status=models.PaymentStatus.PAID,
    )
    db.add(booking)
    db.commit()
    db.refresh(booking)
    bid = booking.id
    db.close()
    return bid


# ─── REVIEWS ─────────────────────────────────────────────────────────────────

class TestGetRoomReviews:
    def test_empty_reviews(self, client, app):
        room_id = _create_room(app)
        r = client.get(f"/reviews/rooms/{room_id}")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["reviews"] == []
        assert body["average_rating"] == 0.0

    def test_pagination_params(self, client, app):
        room_id = _create_room(app)
        r = client.get(f"/reviews/rooms/{room_id}?page=1&per_page=5")
        assert r.status_code == 200

    def test_room_not_found(self, client):
        r = client.get("/reviews/rooms/99999")
        assert r.status_code == 404

    def test_invalid_page_below_one(self, client, app):
        room_id = _create_room(app)
        r = client.get(f"/reviews/rooms/{room_id}?page=0")
        assert r.status_code == 422


class TestCreateReview:
    def test_create_review_success(self, client, app):
        email = "reviewer2@test.com"
        token = _register_and_login(client, email)
        room_id = _create_room(app)
        booking_id = _create_confirmed_booking(app, room_id, email)

        r = client.post(
            "/reviews",
            json={
                "room_id": room_id,
                "booking_id": booking_id,
                "rating": 5,
                "title": "Excellent",
                "body": "Really enjoyed the stay",
                "cleanliness_rating": 5,
                "service_rating": 4,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 201
        body = r.json()
        db = app.state.testing_session_local()
        room = db.query(models.Room).filter(models.Room.id == room_id).first()
        db.close()
        assert body["rating"] == 5
        assert body["is_verified"] is True
        assert body["reviewer_name"] == "Test User"
        assert room.review_count == 1
        assert room.rating == 5.0

    def test_create_review_requires_auth(self, client, app):
        room_id = _create_room(app)
        r = client.post(
            "/reviews",
            json={"room_id": room_id, "booking_id": 1, "rating": 4},
        )
        assert r.status_code == 401

    def test_create_review_wrong_user_booking(self, client, app):
        email_a = "a@test.com"
        email_b = "b@test.com"
        token_b = _register_and_login(client, email_b, "TestPass123")
        room_id = _create_room(app)
        booking_id = _create_confirmed_booking(app, room_id, email_a)

        r = client.post(
            "/reviews",
            json={"room_id": room_id, "booking_id": booking_id, "rating": 3},
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert r.status_code == 404

    def test_duplicate_review_rejected(self, client, app):
        email = "dup@test.com"
        token = _register_and_login(client, email)
        room_id = _create_room(app)
        booking_id = _create_confirmed_booking(app, room_id, email)

        payload = {"room_id": room_id, "booking_id": booking_id, "rating": 4}
        headers = {"Authorization": f"Bearer {token}"}

        first = client.post("/reviews", json=payload, headers=headers)
        assert first.status_code == 201

        second = client.post("/reviews", json=payload, headers=headers)
        assert second.status_code == 409

    def test_review_invalid_rating(self, client, app):
        email = "rate@test.com"
        token = _register_and_login(client, email)
        r = client.post(
            "/reviews",
            json={"room_id": 1, "booking_id": 1, "rating": 10},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 422

    def test_multiple_reviews_keep_exact_room_count(self, client, app):
        room_id = _create_room(app)

        first_email = "count1@test.com"
        second_email = "count2@test.com"
        first_token = _register_and_login(client, first_email)
        second_token = _register_and_login(client, second_email)
        first_booking = _create_confirmed_booking(app, room_id, first_email)
        second_booking = _create_confirmed_booking(app, room_id, second_email)

        client.post(
            "/reviews",
            json={"room_id": room_id, "booking_id": first_booking, "rating": 5},
            headers={"Authorization": f"Bearer {first_token}"},
        )
        client.post(
            "/reviews",
            json={"room_id": room_id, "booking_id": second_booking, "rating": 3},
            headers={"Authorization": f"Bearer {second_token}"},
        )

        db = app.state.testing_session_local()
        room = db.query(models.Room).filter(models.Room.id == room_id).first()
        db.close()

        assert room.review_count == 2
        assert room.rating == 4.0


class TestHostReply:
    def test_host_reply_requires_admin(self, client, app):
        email = "user@test.com"
        token = _register_and_login(client, email)
        r = client.post(
            "/reviews/1/host-reply",
            json={"reply": "Thank you"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 403

    def test_host_reply_review_not_found(self, client, app):
        # Create an admin
        db = app.state.testing_session_local()
        from routers.auth import hash_password
        admin = models.User(
            email="admin@hotel.com",
            full_name="Admin",
            hashed_password=hash_password("AdminPass1"),
            is_admin=True,
            is_active=True,
        )
        db.add(admin)
        db.commit()
        db.close()

        reset_rate_limits()
        login_resp = client.post(
            "/auth/login",
            json={"email": "admin@hotel.com", "password": "AdminPass1"},
        )
        token = login_resp.json()["access_token"]

        r = client.post(
            "/reviews/99999/host-reply",
            json={"reply": "Thank you"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404


class TestDeleteReview:
    def test_delete_own_review(self, client, app):
        email = "del@test.com"
        token = _register_and_login(client, email)
        room_id = _create_room(app)
        booking_id = _create_confirmed_booking(app, room_id, email)

        create_r = client.post(
            "/reviews",
            json={"room_id": room_id, "booking_id": booking_id, "rating": 3},
            headers={"Authorization": f"Bearer {token}"},
        )
        review_id = create_r.json()["id"]

        del_r = client.delete(
            f"/reviews/{review_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        db = app.state.testing_session_local()
        room = db.query(models.Room).filter(models.Room.id == room_id).first()
        db.close()
        assert del_r.status_code == 204
        assert room.review_count == 0
        assert room.rating == 0.0

    def test_delete_another_user_review_forbidden(self, client, app):
        email_a = "owner@test.com"
        email_b = "thief@test.com"
        token_a = _register_and_login(client, email_a)
        token_b = _register_and_login(client, email_b, "TestPass123")

        room_id = _create_room(app)
        booking_id = _create_confirmed_booking(app, room_id, email_a)

        create_r = client.post(
            "/reviews",
            json={"room_id": room_id, "booking_id": booking_id, "rating": 4},
            headers={"Authorization": f"Bearer {token_a}"},
        )
        review_id = create_r.json()["id"]

        del_r = client.delete(
            f"/reviews/{review_id}",
            headers={"Authorization": f"Bearer {token_b}"},
        )
        assert del_r.status_code == 403

    def test_delete_nonexistent_review(self, client, app):
        email = "nd@test.com"
        token = _register_and_login(client, email)
        r = client.delete(
            "/reviews/99999",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404


# ─── WISHLIST ─────────────────────────────────────────────────────────────────

class TestWishlistToggle:
    def test_toggle_add_to_wishlist(self, client, app):
        token = _register_and_login(client, "wl1@test.com")
        room_id = _create_room(app)

        r = client.post(
            f"/wishlist/{room_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["saved"] is True
        assert "Added" in body["message"]

    def test_toggle_remove_from_wishlist(self, client, app):
        token = _register_and_login(client, "wl2@test.com")
        room_id = _create_room(app)

        # Add
        client.post(f"/wishlist/{room_id}", headers={"Authorization": f"Bearer {token}"})
        # Remove (toggle again)
        r = client.post(
            f"/wishlist/{room_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200
        assert r.json()["saved"] is False

    def test_toggle_room_not_found(self, client, app):
        token = _register_and_login(client, "wl3@test.com")
        r = client.post(
            "/wishlist/99999",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404

    def test_toggle_requires_auth(self, client, app):
        room_id = _create_room(app)
        r = client.post(f"/wishlist/{room_id}")
        assert r.status_code == 401


class TestGetWishlist:
    def test_empty_wishlist(self, client, app):
        token = _register_and_login(client, "empty_wl@test.com")
        r = client.get("/wishlist", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["total"] == 0
        assert r.json()["items"] == []

    def test_wishlist_contains_added_room(self, client, app):
        token = _register_and_login(client, "has_wl@test.com")
        room_id = _create_room(app)
        client.post(f"/wishlist/{room_id}", headers={"Authorization": f"Bearer {token}"})

        r = client.get("/wishlist", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        assert body["items"][0]["room_id"] == room_id

    def test_get_wishlist_requires_auth(self, client):
        r = client.get("/wishlist")
        assert r.status_code == 401


class TestWishlistStatus:
    def test_status_returns_saved_room_ids(self, client, app):
        token = _register_and_login(client, "status_wl@test.com")
        room_id = _create_room(app)
        client.post(f"/wishlist/{room_id}", headers={"Authorization": f"Bearer {token}"})

        r = client.get("/wishlist/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert room_id in r.json()["room_ids"]

    def test_status_empty_when_nothing_saved(self, client, app):
        token = _register_and_login(client, "nostatus@test.com")
        r = client.get("/wishlist/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["room_ids"] == []


class TestRemoveFromWishlist:
    def test_explicit_remove(self, client, app):
        token = _register_and_login(client, "rm_wl@test.com")
        room_id = _create_room(app)
        client.post(f"/wishlist/{room_id}", headers={"Authorization": f"Bearer {token}"})

        r = client.delete(
            f"/wishlist/{room_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 204

    def test_remove_not_in_wishlist(self, client, app):
        token = _register_and_login(client, "rm_none@test.com")
        room_id = _create_room(app)
        r = client.delete(
            f"/wishlist/{room_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 404

    def test_remove_requires_auth(self, client, app):
        room_id = _create_room(app)
        r = client.delete(f"/wishlist/{room_id}")
        assert r.status_code == 401
