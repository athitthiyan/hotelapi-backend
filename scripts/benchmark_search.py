from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import models  # noqa: E402
from database import Base, get_db  # noqa: E402
from routers import analytics, auth, bookings, notifications, ops, partner, payments, rooms  # noqa: E402
from services.inventory_service import upsert_inventory_range  # noqa: E402
from services.search_service import clear_search_cache  # noqa: E402


@dataclass
class BenchmarkResult:
    room_count: int
    p50_ms: float
    p95_ms: float
    p99_ms: float


def build_app(db_url: str):
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(bookings.router)
    app.include_router(payments.router)
    app.include_router(rooms.router)
    app.include_router(analytics.router)
    app.include_router(notifications.router)
    app.include_router(ops.router)
    app.include_router(partner.router)

    def override_get_db():
        db = testing_session_local()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.state.testing_session_local = testing_session_local
    app.state.testing_engine = engine
    return app, testing_session_local, engine


def seed_rooms(session_factory, room_count: int) -> None:
    db = session_factory()
    try:
        check_in = date.today() + timedelta(days=30)
        check_out = check_in + timedelta(days=2)
        for idx in range(room_count):
            room = models.Room(
                hotel_name=f"Benchmark Hotel {idx}",
                room_type=models.RoomType.SUITE if idx % 2 == 0 else models.RoomType.DELUXE,
                description=f"Benchmark room {idx}",
                price=2500.0 + idx,
                availability=True,
                rating=4.0 + (idx % 10) / 10,
                review_count=25 + idx,
                location="Near Marina Beach" if idx % 3 == 0 else "City Centre",
                city="Chennai",
                country="India",
                max_guests=2 + (idx % 4),
                amenities='["WiFi","Breakfast included","Family room"]'
                if idx % 2 == 0
                else '["WiFi","Pool"]',
                image_url="https://example.com/image.jpg",
                is_featured=idx % 5 == 0,
            )
            db.add(room)
            db.flush()
            upsert_inventory_range(
                db,
                room_id=room.id,
                start_date=check_in,
                end_date=check_out - timedelta(days=1),
                total_units=4,
                available_units=4,
                status=models.InventoryStatus.AVAILABLE,
            )
        db.commit()
    finally:
        db.close()


def percentile(sorted_values: list[float], value: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    index = min(len(sorted_values) - 1, round((len(sorted_values) - 1) * value))
    return sorted_values[index]


def run_benchmark(room_count: int, iterations: int) -> BenchmarkResult:
    temp_db = Path(tempfile.gettempdir()) / f"stayease_benchmark_{room_count}.db"
    if temp_db.exists():
        temp_db.unlink()

    app, session_factory, engine = build_app(f"sqlite:///{temp_db}")
    try:
        seed_rooms(session_factory, room_count)
        client = TestClient(app)
        latencies: list[float] = []
        params = {
            "city": "Chennai",
            "landmark": "Marina Beach",
            "min_rating": 4.2,
            "amenities": "WiFi",
            "check_in": str(date.today() + timedelta(days=30)),
            "check_out": str(date.today() + timedelta(days=32)),
            "sort_by": "price_low_to_high",
            "page": 1,
            "per_page": 20,
        }

        for _ in range(iterations):
            clear_search_cache()
            start = time.perf_counter()
            response = client.get("/rooms", params=params)
            elapsed_ms = (time.perf_counter() - start) * 1000
            if response.status_code != 200:
                raise RuntimeError(f"Benchmark request failed with {response.status_code}: {response.text}")
            latencies.append(elapsed_ms)

        latencies.sort()
        return BenchmarkResult(
            room_count=room_count,
            p50_ms=round(statistics.median(latencies), 2),
            p95_ms=round(percentile(latencies, 0.95), 2),
            p99_ms=round(percentile(latencies, 0.99), 2),
        )
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()
        if temp_db.exists():
            temp_db.unlink()


def render_report(results: list[BenchmarkResult]) -> str:
    lines = [
        "# StayEase Search Benchmark Report",
        "",
        "| Rooms | p50 (ms) | p95 (ms) | p99 (ms) |",
        "|---|---:|---:|---:|",
    ]
    for result in results:
        lines.append(
            f"| {result.room_count} | {result.p50_ms:.2f} | {result.p95_ms:.2f} | {result.p99_ms:.2f} |"
        )
    lines.extend(
        [
            "",
            "Notes:",
            "- Environment: FastAPI TestClient with temporary SQLite database.",
            "- Cache was cleared before every request to measure uncached search latency.",
            "- Dataset was seeded with available Chennai rooms and active inventory for the queried date range.",
        ]
    )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Benchmark StayEase room search latency.")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("benchmarks/search_benchmark_report.md"),
    )
    args = parser.parse_args()

    results = [run_benchmark(room_count, args.iterations) for room_count in (100, 500, 1000)]
    report = render_report(results)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
