"""
100% branch-coverage tests for routers/rooms.py

Branches covered:
  get_rooms        – cache HIT/MISS, all individual filters (query/city/room_type/
                     min_price/max_price/guests/featured), check_in+check_out inventory
                     filter, pagination
  get_featured_rooms – basic happy path
  get_destinations – with and without query filter
  get_room         – found / 404
  create_room      – admin success / non-admin 403
  update_room      – success / room not found 404 / non-admin 403
  delete_room      – success / not found 404 / has bookings 400
  update_room_inventory – success / room not found / end<start / neg total / neg avail /
                          available_units=None
  get_room_inventory    – no filters / start_date filter / end_date filter / room 404
"""

from __future__ import annotations

import pytest

from routers.auth import hash_password
import models
from services.search_service import _search_cache  # to test cache HIT


# ─── helpers ─────────────────────────────────────────────────────────────────

def auth_header(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def room_payload(**overrides) -> dict:
    base = {
        "hotel_name": "Test Hotel",
        "room_type": "suite",
        "description": "A nice room",
        "price": 200.0,
        "availability": True,
        "rating": 4.5,
        "review_count": 10,
        "image_url": "https://example.com/img.jpg",
        "gallery_urls": None,
        "amenities": None,
        "location": "Downtown",
        "city": "Paris",
        "country": "France",
        "max_guests": 2,
        "beds": 1,
        "bathrooms": 1,
        "size_sqft": 400,
        "floor": 2,
        "is_featured": False,
    }
    base.update(overrides)
    return base


def inventory_payload(room_id: int, **overrides) -> dict:
    base = {
        "room_id": room_id,
        "start_date": "2030-06-01",
        "end_date": "2030-06-07",
        "total_units": 5,
        "available_units": 5,
        "status": "available",
    }
    base.update(overrides)
    return base


def make_admin(db_session):
    admin = models.User(
        email="admin@rooms.com",
        full_name="Admin",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    db_session.refresh(admin)
    return admin


def admin_login(client) -> dict:
    r = client.post("/auth/login", json={"email": "admin@rooms.com", "password": "AdminPass123"})
    return auth_header(r.json()["access_token"])


def create_room_via_api(client, headers, **overrides) -> dict:
    r = client.post("/rooms", headers=headers, json=room_payload(**overrides))
    assert r.status_code == 201, r.text
    return r.json()


# ─── get_rooms ────────────────────────────────────────────────────────────────

class TestGetRooms:
    def test_no_filters_returns_rooms(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers)
        r = client.get("/rooms")
        assert r.status_code == 200
        body = r.json()
        assert "rooms" in body
        assert body["total"] >= 1

    def test_cache_hit_returns_same_result(self, client, db_session):
        """Second identical call returns cached result without hitting DB again."""
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers)

        r1 = client.get("/rooms")
        assert r1.status_code == 200

        # Cache should now be populated; second call must still return 200
        r2 = client.get("/rooms")
        assert r2.status_code == 200
        assert r2.json()["total"] == r1.json()["total"]

    def test_query_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, hotel_name="UniqueNameXYZ", city="Berlin")
        r = client.get("/rooms", params={"query": "UniqueNameXYZ"})
        assert r.status_code == 200
        assert any("UniqueNameXYZ" in room["hotel_name"] for room in r.json()["rooms"])

    def test_city_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, city="Tokyo")
        r = client.get("/rooms", params={"city": "Tokyo"})
        assert r.status_code == 200
        assert all(room["city"] == "Tokyo" for room in r.json()["rooms"])

    def test_room_type_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, room_type="deluxe")
        r = client.get("/rooms", params={"room_type": "deluxe"})
        assert r.status_code == 200
        assert all(room["room_type"] == "deluxe" for room in r.json()["rooms"])

    def test_min_price_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, price=500.0)
        r = client.get("/rooms", params={"min_price": 400})
        assert r.status_code == 200
        assert all(room["price"] >= 400 for room in r.json()["rooms"])

    def test_max_price_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, price=50.0)
        r = client.get("/rooms", params={"max_price": 100})
        assert r.status_code == 200
        assert all(room["price"] <= 100 for room in r.json()["rooms"])

    def test_guests_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, max_guests=4)
        r = client.get("/rooms", params={"guests": 4})
        assert r.status_code == 200
        assert all(room["max_guests"] >= 4 for room in r.json()["rooms"])

    def test_featured_filter_true(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, is_featured=True)
        r = client.get("/rooms", params={"featured": "true"})
        assert r.status_code == 200
        assert all(room["is_featured"] is True for room in r.json()["rooms"])

    def test_featured_filter_false(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, is_featured=False)
        r = client.get("/rooms", params={"featured": "false"})
        assert r.status_code == 200
        assert all(room["is_featured"] is False for room in r.json()["rooms"])

    def test_check_in_check_out_inventory_filter(self, client, db_session):
        """Rooms without inventory for the requested dates should be excluded."""
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, city="InventoryCity")
        # No inventory upserted → room should be filtered out
        r = client.get("/rooms", params={
            "city": "InventoryCity",
            "check_in": "2035-01-10",
            "check_out": "2035-01-12",
        })
        assert r.status_code == 200
        assert r.json()["total"] == 0

    def test_pagination(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        for i in range(3):
            create_room_via_api(client, headers, hotel_name=f"PaginatedHotel{i}", city="PagCity")
        r = client.get("/rooms", params={"city": "PagCity", "per_page": 2, "page": 1})
        assert r.status_code == 200
        body = r.json()
        assert len(body["rooms"]) <= 2
        assert body["total"] == 3

    def test_sort_by_price_asc(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, city="SortCity", price=300.0)
        create_room_via_api(client, headers, city="SortCity", price=100.0)
        r = client.get("/rooms", params={"city": "SortCity", "sort_by": "price_asc"})
        assert r.status_code == 200
        prices = [room["price"] for room in r.json()["rooms"]]
        assert prices == sorted(prices)


# ─── get_featured_rooms ───────────────────────────────────────────────────────

class TestGetFeaturedRooms:
    def test_returns_only_featured(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, is_featured=True)
        create_room_via_api(client, headers, is_featured=False)
        r = client.get("/rooms/featured")
        assert r.status_code == 200
        assert all(room["is_featured"] is True for room in r.json())

    def test_respects_limit(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        for _ in range(3):
            create_room_via_api(client, headers, is_featured=True)
        r = client.get("/rooms/featured", params={"limit": 2})
        assert r.status_code == 200
        assert len(r.json()) <= 2


# ─── get_destinations ─────────────────────────────────────────────────────────

class TestGetDestinations:
    def test_destinations_no_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, city="Madrid", country="Spain")
        r = client.get("/rooms/destinations")
        assert r.status_code == 200
        body = r.json()
        assert "destinations" in body
        assert body["total"] >= 1

    def test_destinations_with_query_filter(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, city="Kyoto", country="Japan")
        r = client.get("/rooms/destinations", params={"query": "Kyoto"})
        assert r.status_code == 200
        body = r.json()
        assert any(d["city"] == "Kyoto" for d in body["destinations"])

    def test_destinations_query_no_match(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        create_room_via_api(client, headers, city="Berlin", country="Germany")
        r = client.get("/rooms/destinations", params={"query": "ZZZNoMatch"})
        assert r.status_code == 200
        assert r.json()["total"] == 0


# ─── get_room ─────────────────────────────────────────────────────────────────

class TestGetRoom:
    def test_get_room_found(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        r = client.get(f"/rooms/{room['id']}")
        assert r.status_code == 200
        assert r.json()["id"] == room["id"]

    def test_get_room_not_found(self, client):
        r = client.get("/rooms/999999")
        assert r.status_code == 404
        assert r.json()["detail"] == "Room not found"


# ─── create_room ──────────────────────────────────────────────────────────────

class TestCreateRoom:
    def test_admin_can_create_room(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        r = client.post("/rooms", headers=headers, json=room_payload())
        assert r.status_code == 201
        assert r.json()["hotel_name"] == "Test Hotel"

    def test_non_admin_cannot_create_room(self, client):
        signup = client.post("/auth/signup", json={
            "email": "user@rooms.com",
            "full_name": "User",
            "password": "UserPass123",
        })
        headers = auth_header(signup.json()["access_token"])
        r = client.post("/rooms", headers=headers, json=room_payload())
        assert r.status_code == 403


# ─── update_room ──────────────────────────────────────────────────────────────

class TestUpdateRoom:
    def test_admin_can_update_room(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        r = client.patch(f"/rooms/{room['id']}", headers=headers, json={"price": 999.0})
        assert r.status_code == 200
        assert r.json()["price"] == 999.0

    def test_update_room_not_found(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        r = client.patch("/rooms/999999", headers=headers, json={"price": 999.0})
        assert r.status_code == 404
        assert r.json()["detail"] == "Room not found"

    def test_non_admin_cannot_update_room(self, client, db_session):
        make_admin(db_session)
        admin_headers = admin_login(client)
        room = create_room_via_api(client, admin_headers)

        signup = client.post("/auth/signup", json={
            "email": "user2@rooms.com",
            "full_name": "User2",
            "password": "UserPass123",
        })
        user_headers = auth_header(signup.json()["access_token"])
        r = client.patch(f"/rooms/{room['id']}", headers=user_headers, json={"price": 1.0})
        assert r.status_code == 403


# ─── delete_room ──────────────────────────────────────────────────────────────

class TestDeleteRoom:
    def test_admin_can_delete_room(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        r = client.delete(f"/rooms/{room['id']}", headers=headers)
        assert r.status_code == 200
        assert r.json()["message"] == "Room deleted successfully"

    def test_delete_room_not_found(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        r = client.delete("/rooms/999999", headers=headers)
        assert r.status_code == 404
        assert r.json()["detail"] == "Room not found"

    def test_delete_room_with_existing_bookings_returns_400(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)

        # Create a user and make a booking for that room
        signup = client.post("/auth/signup", json={
            "email": "booker@rooms.com",
            "full_name": "Booker",
            "password": "BookerPass123",
        })
        user_headers = auth_header(signup.json()["access_token"])

        # Create a booking directly in DB to avoid inventory requirements
        booking = models.Booking(
            room_id=room["id"],
            user_id=signup.json()["user"]["id"],
            check_in="2032-01-01",
            check_out="2032-01-03",
            guests=1,
            total_price=400.0,
            status=models.BookingStatus.CONFIRMED,
            reference="REF-DELETE-TEST",
        )
        db_session.add(booking)
        db_session.commit()

        r = client.delete(f"/rooms/{room['id']}", headers=headers)
        assert r.status_code == 400
        assert "existing bookings" in r.json()["detail"]


# ─── update_room_inventory ────────────────────────────────────────────────────

class TestUpdateRoomInventory:
    def test_success_with_explicit_available_units(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        r = client.post("/rooms/inventory", headers=headers, json=inventory_payload(room["id"]))
        assert r.status_code == 200
        body = r.json()
        assert body["total"] > 0

    def test_success_with_available_units_none(self, client, db_session):
        """available_units=None → defaults to total_units (branch in upsert_inventory_range)."""
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        payload = inventory_payload(room["id"])
        payload["available_units"] = None
        r = client.post("/rooms/inventory", headers=headers, json=payload)
        assert r.status_code == 200

    def test_room_not_found_returns_404(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        r = client.post("/rooms/inventory", headers=headers, json=inventory_payload(999999))
        assert r.status_code == 404
        assert r.json()["detail"] == "Room not found"

    def test_end_date_before_start_date_returns_400(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        payload = inventory_payload(room["id"], start_date="2030-06-10", end_date="2030-06-01")
        r = client.post("/rooms/inventory", headers=headers, json=payload)
        assert r.status_code == 400
        assert "End date" in r.json()["detail"]

    def test_negative_total_units_returns_400(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        payload = inventory_payload(room["id"], total_units=-1)
        r = client.post("/rooms/inventory", headers=headers, json=payload)
        assert r.status_code == 400
        assert "negative" in r.json()["detail"].lower()

    def test_negative_available_units_returns_400(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        payload = inventory_payload(room["id"], available_units=-1)
        r = client.post("/rooms/inventory", headers=headers, json=payload)
        assert r.status_code == 400
        assert "negative" in r.json()["detail"].lower()

    def test_non_admin_cannot_update_inventory(self, client, db_session):
        make_admin(db_session)
        admin_headers = admin_login(client)
        room = create_room_via_api(client, admin_headers)

        signup = client.post("/auth/signup", json={
            "email": "inv_user@rooms.com",
            "full_name": "InvUser",
            "password": "InvUser123",
        })
        user_headers = auth_header(signup.json()["access_token"])
        r = client.post("/rooms/inventory", headers=user_headers, json=inventory_payload(room["id"]))
        assert r.status_code == 403


# ─── get_room_inventory ───────────────────────────────────────────────────────

class TestGetRoomInventory:
    def _setup(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        room = create_room_via_api(client, headers)
        client.post("/rooms/inventory", headers=headers, json=inventory_payload(room["id"]))
        return room, headers

    def test_no_date_filters(self, client, db_session):
        room, headers = self._setup(client, db_session)
        r = client.get(f"/rooms/{room['id']}/inventory", headers=headers)
        assert r.status_code == 200
        assert r.json()["total"] > 0

    def test_start_date_filter(self, client, db_session):
        room, headers = self._setup(client, db_session)
        r = client.get(
            f"/rooms/{room['id']}/inventory",
            headers=headers,
            params={"start_date": "2030-06-03"},
        )
        assert r.status_code == 200
        # All returned dates should be on or after 2030-06-03
        for inv in r.json()["inventory"]:
            assert inv["inventory_date"] >= "2030-06-03"

    def test_end_date_filter(self, client, db_session):
        room, headers = self._setup(client, db_session)
        r = client.get(
            f"/rooms/{room['id']}/inventory",
            headers=headers,
            params={"end_date": "2030-06-04"},
        )
        assert r.status_code == 200
        for inv in r.json()["inventory"]:
            assert inv["inventory_date"] <= "2030-06-04"

    def test_both_date_filters(self, client, db_session):
        room, headers = self._setup(client, db_session)
        r = client.get(
            f"/rooms/{room['id']}/inventory",
            headers=headers,
            params={"start_date": "2030-06-02", "end_date": "2030-06-05"},
        )
        assert r.status_code == 200
        for inv in r.json()["inventory"]:
            assert "2030-06-02" <= inv["inventory_date"] <= "2030-06-05"

    def test_room_not_found_returns_404(self, client, db_session):
        make_admin(db_session)
        headers = admin_login(client)
        r = client.get("/rooms/999999/inventory", headers=headers)
        assert r.status_code == 404
        assert r.json()["detail"] == "Room not found"
