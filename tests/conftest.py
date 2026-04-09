import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_bootstrap.db")

import models  # noqa: E402
import main  # noqa: E402
import database as database_module  # noqa: E402
from database import Base, get_db  # noqa: E402
from services.rate_limit_service import reset_rate_limits  # noqa: E402


@pytest.fixture()
def app(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    app = main.app
    original_main_engine = main.engine
    original_main_session_local = main.SessionLocal
    original_database_engine = database_module.engine
    original_database_session_local = database_module.SessionLocal
    original_background_scheduler = main.BackgroundScheduler
    original_scheduler = getattr(app.state, "hold_expiry_scheduler", None)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.state.testing_session_local = TestingSessionLocal
    app.state.testing_engine = engine
    app.state.hold_expiry_scheduler = None
    main.engine = engine
    main.SessionLocal = TestingSessionLocal
    main.BackgroundScheduler = None
    database_module.engine = engine
    database_module.SessionLocal = TestingSessionLocal

    yield app

    app.dependency_overrides.clear()
    app.state.hold_expiry_scheduler = original_scheduler
    main.engine = original_main_engine
    main.SessionLocal = original_main_session_local
    main.BackgroundScheduler = original_background_scheduler
    database_module.engine = original_database_engine
    database_module.SessionLocal = original_database_session_local
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def reset_rate_limit_state():
    reset_rate_limits()
    yield
    reset_rate_limits()


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
                "check_in": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
                "check_out": (datetime.now(timezone.utc) + timedelta(days=2, hours=2)).isoformat(),
                "guests": 2,
                "special_requests": "",
            },
        )
        assert response.status_code == 201
        return response.json()

    return _create_booking
