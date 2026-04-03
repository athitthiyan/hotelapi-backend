import os
import sys
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

import models  # noqa: E402
from database import Base, get_db  # noqa: E402
from routers import analytics, auth, bookings, payments, rooms  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(bookings.router)
    app.include_router(payments.router)
    app.include_router(rooms.router)
    app.include_router(analytics.router)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.state.testing_session_local = TestingSessionLocal
    app.state.testing_engine = engine

    yield app

    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db_session(app):
    db = app.state.testing_session_local()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def room_id(db_session):
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
    db_session.refresh(room)
    return room.id


@pytest.fixture()
def create_booking(client, room_id):
    def _create_booking():
        response = client.post(
            "/bookings",
            json={
                "user_name": "Athit",
                "email": "athit@example.com",
                "phone": "1234567890",
                "room_id": room_id,
                "check_in": datetime.now(timezone.utc).isoformat(),
                "check_out": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
                "guests": 2,
                "special_requests": "",
            },
        )
        assert response.status_code == 201
        return response.json()

    return _create_booking
