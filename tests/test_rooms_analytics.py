from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import models
from routers.auth import hash_password


def create_room(db_session, **overrides):
    room = models.Room(
        hotel_name=overrides.get("hotel_name", "Hotel One"),
        room_type=overrides.get("room_type", models.RoomType.DELUXE),
        description=overrides.get("description", "desc"),
        price=overrides.get("price", 200.0),
        availability=overrides.get("availability", True),
        rating=overrides.get("rating", 4.5),
        review_count=overrides.get("review_count", 10),
        image_url=overrides.get("image_url", "https://example.com/room.jpg"),
        location=overrides.get("location", "New York"),
        city=overrides.get("city", "New York"),
        country=overrides.get("country", "USA"),
        max_guests=overrides.get("max_guests", 2),
        is_featured=overrides.get("is_featured", False),
    )
    db_session.add(room)
    db_session.commit()
    db_session.refresh(room)
    return room


def create_booking_and_transaction(db_session, room, success=True):
    booking = models.Booking(
        booking_ref="BKANLT01" if success else "BKANLT02",
        user_name="Athit",
        email="athit@example.com",
        room_id=room.id,
        check_in=datetime.now(timezone.utc),
        check_out=datetime.now(timezone.utc) + timedelta(days=2),
        guests=2,
        nights=2,
        room_rate=room.price * 2,
        taxes=20,
        service_fee=10,
        total_amount=room.price * 2 + 30,
        status=models.BookingStatus.CONFIRMED if success else models.BookingStatus.PENDING,
        payment_status=models.PaymentStatus.PAID if success else models.PaymentStatus.FAILED,
    )
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)

    txn = models.Transaction(
        booking_id=booking.id,
        transaction_ref="TXNANLT01" if success else "TXNANLT02",
        amount=booking.total_amount,
        currency="USD",
        payment_method="card",
        status=models.TransactionStatus.SUCCESS if success else models.TransactionStatus.FAILED,
        failure_reason=None if success else "Declined",
    )
    db_session.add(txn)
    db_session.commit()
    return booking, txn


def test_get_rooms_filters_and_pagination(client, db_session):
    create_room(db_session, hotel_name="Hotel A", city="Paris", price=300, max_guests=4, is_featured=True)
    create_room(db_session, hotel_name="Hotel B", city="London", price=100, availability=False)
    create_room(db_session, hotel_name="Hotel C", city="Paris", price=150, room_type=models.RoomType.SUITE)

    response = client.get(
        "/rooms",
        params={
            "city": "Paris",
            "min_price": 140,
            "max_price": 350,
            "guests": 2,
            "featured": "true",
            "page": 1,
            "per_page": 10,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["rooms"][0]["hotel_name"] == "Hotel A"


def test_get_rooms_supports_sorting_and_text_query(client, db_session):
    create_room(
        db_session,
        hotel_name="Budget Paris Stay",
        city="Paris",
        price=120,
        rating=4.2,
        review_count=50,
        is_featured=False,
    )
    create_room(
        db_session,
        hotel_name="Luxury Paris Palace",
        city="Paris",
        price=450,
        rating=4.9,
        review_count=400,
        is_featured=True,
    )
    create_room(
        db_session,
        hotel_name="Paris Business Hub",
        city="Paris",
        price=220,
        rating=4.6,
        review_count=180,
        is_featured=False,
    )

    recommended = client.get("/rooms", params={"query": "Paris", "sort_by": "recommended"})
    price_asc = client.get("/rooms", params={"query": "Paris", "sort_by": "price_asc"})
    rating_desc = client.get("/rooms", params={"query": "Paris", "sort_by": "rating_desc"})

    assert recommended.status_code == 200
    assert price_asc.status_code == 200
    assert rating_desc.status_code == 200
    assert recommended.json()["total"] == 3
    assert recommended.json()["rooms"][0]["hotel_name"] == "Luxury Paris Palace"
    assert price_asc.json()["rooms"][0]["hotel_name"] == "Budget Paris Stay"
    assert rating_desc.json()["rooms"][0]["hotel_name"] == "Luxury Paris Palace"


def test_get_rooms_filters_by_date_inventory_and_admin_inventory_endpoints(client, db_session):
    room = create_room(db_session, hotel_name="Inventory Room", city="Paris", price=220)
    admin = models.User(
        email="admin-inventory@example.com",
        full_name="Admin Inventory",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-inventory@example.com", "password": "AdminPass123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    update_inventory = client.post(
        "/rooms/inventory",
        headers=headers,
        json={
            "room_id": room.id,
            "start_date": "2026-04-20",
            "end_date": "2026-04-22",
            "total_units": 0,
            "available_units": 0,
            "status": "blocked",
        },
    )
    filtered = client.get(
        "/rooms",
        params={
            "city": "Paris",
            "check_in": "2026-04-20T00:00:00+00:00",
            "check_out": "2026-04-22T00:00:00+00:00",
        },
    )
    inventory_view = client.get(
        f"/rooms/{room.id}/inventory",
        headers=headers,
        params={"start_date": "2026-04-20", "end_date": "2026-04-22"},
    )

    assert update_inventory.status_code == 200
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 0
    assert inventory_view.status_code == 200
    assert inventory_view.json()["total"] == 3


def test_get_destinations_returns_ranked_destination_summaries(client, db_session):
    create_room(db_session, hotel_name="Goa Beach One", city="Goa", country="India", price=200, is_featured=True)
    create_room(db_session, hotel_name="Goa Beach Two", city="Goa", country="India", price=180, is_featured=False)
    create_room(db_session, hotel_name="Bali Retreat", city="Bali", country="Indonesia", price=260, is_featured=True)

    response = client.get("/rooms/destinations", params={"query": "a", "limit": 10})

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 2
    assert body["destinations"][0]["city"] == "Goa"
    assert body["destinations"][0]["room_count"] == 2


def test_delete_room_with_bookings_is_blocked(client, db_session):
    room = create_room(db_session, hotel_name="Booked Room")
    admin = models.User(
        email="admin-delete@example.com",
        full_name="Admin Delete",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-delete@example.com", "password": "AdminPass123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    booking = models.Booking(
        booking_ref="BKDELETE1",
        user_name="Booker",
        email="booker@example.com",
        room_id=room.id,
        check_in=datetime.now(timezone.utc),
        check_out=datetime.now(timezone.utc) + timedelta(days=2),
        guests=2,
        nights=2,
        room_rate=room.price * 2,
        taxes=10,
        service_fee=5,
        total_amount=room.price * 2 + 15,
        status=models.BookingStatus.CONFIRMED,
        payment_status=models.PaymentStatus.PAID,
    )
    db_session.add(booking)
    db_session.commit()

    response = client.delete(f"/rooms/{room.id}", headers=headers)

    assert response.status_code == 400
    assert response.json()["detail"] == "Cannot delete a room with existing bookings"


def test_get_featured_get_room_and_create_room(client, db_session):
    featured = create_room(db_session, hotel_name="Featured", is_featured=True)
    admin = models.User(
        email="admin-rooms@example.com",
        full_name="Admin Rooms",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()

    login = client.post(
        "/auth/login",
        json={"email": "admin-rooms@example.com", "password": "AdminPass123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    featured_response = client.get("/rooms/featured", params={"limit": 5})
    room_response = client.get(f"/rooms/{featured.id}")
    create_response = client.post(
        "/rooms",
        headers=headers,
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

    assert featured_response.status_code == 200
    assert len(featured_response.json()) == 1
    assert room_response.status_code == 200
    assert room_response.json()["hotel_name"] == "Featured"
    assert create_response.status_code == 201
    assert create_response.json()["hotel_name"] == "Created Room"

    update_response = client.patch(
        f"/rooms/{featured.id}",
        headers=headers,
        json={"price": 333, "is_featured": False},
    )
    delete_with_booking_block = client.delete(f"/rooms/{featured.id}", headers=headers)
    delete_created_room = client.delete(f"/rooms/{create_response.json()['id']}", headers=headers)

    assert update_response.status_code == 200
    assert update_response.json()["price"] == 333
    assert delete_with_booking_block.status_code == 200
    assert delete_created_room.status_code == 200
    assert delete_created_room.json()["message"] == "Room deleted successfully"

    audit_logs = client.get("/ops/audit-logs", headers=headers)
    actions = {item["action"] for item in audit_logs.json()["logs"]}

    assert audit_logs.status_code == 200
    assert "room.create" in actions
    assert "room.update" in actions
    assert "room.delete" in actions


def test_get_room_not_found(client):
    response = client.get("/rooms/999999")
    assert response.status_code == 404
    assert response.json()["detail"] == "Room not found"


def test_analytics_recent_bookings_and_revenue_stats(client, db_session):
    admin = models.User(
        email="admin-analytics@example.com",
        full_name="Admin Analytics",
        hashed_password=hash_password("AdminPass123"),
        is_admin=True,
        is_active=True,
    )
    db_session.add(admin)
    db_session.commit()
    login = client.post(
        "/auth/login",
        json={"email": "admin-analytics@example.com", "password": "AdminPass123"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    success_room = create_room(db_session, hotel_name="Analytics Success", room_type=models.RoomType.SUITE)
    failed_room = create_room(db_session, hotel_name="Analytics Failed", room_type=models.RoomType.DELUXE)
    create_booking_and_transaction(db_session, success_room, success=True)
    create_booking_and_transaction(db_session, failed_room, success=False)

    with patch("routers.analytics.cast", side_effect=lambda value, _type: value):
        analytics = client.get("/analytics", headers=headers, params={"days": 30})
        recent = client.get("/analytics/recent-bookings", headers=headers, params={"limit": 5})
        revenue = client.get("/analytics/revenue-stats", headers=headers)

    assert analytics.status_code == 200
    analytics_body = analytics.json()
    assert analytics_body["kpis"]["total_bookings"] == 2
    assert analytics_body["kpis"]["failed_payments"] == 1
    assert analytics_body["payment_breakdown"]
    assert analytics_body["room_type_breakdown"]

    assert recent.status_code == 200
    assert recent.json()["total"] == 2

    assert revenue.status_code == 200
    assert revenue.json()["this_month"] >= 0
