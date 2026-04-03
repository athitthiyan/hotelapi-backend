import json
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from database import Base, SessionLocal, engine, settings
from routers import analytics, bookings, payments, rooms

logger = logging.getLogger(__name__)

app = FastAPI(
    title="HotelAPI - Portfolio Backend",
    description="Unified backend for StayEase Booking, PayFlow Payment Gateway and InsightBoard Admin",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.on_event("startup")
def startup_checks():
    """Verify database connectivity and schema readiness at startup."""
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT 1"))
            existing_tables = set(inspect(connection).get_table_names())
            required_tables = set(Base.metadata.tables.keys())
            missing_tables = sorted(required_tables - existing_tables)

            if missing_tables and settings.auto_create_schema:
                Base.metadata.create_all(bind=connection)
                logger.warning(
                    "AUTO_CREATE_SCHEMA enabled; created missing tables: %s",
                    ", ".join(missing_tables),
                )
            elif missing_tables:
                logger.warning(
                    "Database schema is missing tables: %s. Run Alembic migrations before serving production traffic.",
                    ", ".join(missing_tables),
                )

        logger.info("Database connection established.")
    except SQLAlchemyError as exc:
        logger.exception("Database initialization failed during startup: %s", exc)


# Always-allowed production origins (code-level guarantee, not reliant on env var)
_HARDCODED_ORIGINS = [
    "https://stayease-booking-app.vercel.app",
    "https://payflow-payment-app.vercel.app",
    "https://insightboard-admin.vercel.app",
]
_env_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
origins = list(set(_env_origins + _HARDCODED_ORIGINS))

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://.*\.vercel\.app",  # covers all Vercel preview URLs too
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

app.include_router(rooms.router)
app.include_router(bookings.router)
app.include_router(payments.router)
app.include_router(analytics.router)


@app.get("/", tags=["Health"])
def root():
    return {
        "status": "HotelAPI is running",
        "version": "1.0.0",
        "docs": "/docs",
    }


@app.api_route("/health", methods=["GET", "HEAD"], tags=["Health"])
def health_check():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        database_status = "connected"
    except SQLAlchemyError:
        database_status = "unavailable"

    return {
        "status": "healthy",
        "service": "hotel-api",
        "database": database_status,
    }


@app.post("/seed", tags=["Dev"])
def seed_database(db=None):
    """Seed the database with sample rooms. Run once after deployment."""
    db = SessionLocal()
    try:
        if db.query(__import__("models").Room).count() > 0:
            return {"message": "Database already seeded"}

        sample_rooms = [
            {
                "hotel_name": "The Grand Azure",
                "room_type": "penthouse",
                "description": "Spectacular penthouse suite with panoramic city views, private terrace, and butler service.",
                "price": 850.00,
                "original_price": 1200.00,
                "rating": 4.9,
                "review_count": 284,
                "image_url": "https://images.unsplash.com/photo-1631049307264-da0ec9d70304?w=800",
                "gallery_urls": json.dumps(
                    [
                        "https://images.unsplash.com/photo-1631049307264-da0ec9d70304?w=800",
                        "https://images.unsplash.com/photo-1582719478250-c89cae4dc85b?w=800",
                        "https://images.unsplash.com/photo-1590490359683-658d3d23f972?w=800",
                    ]
                ),
                "amenities": json.dumps(
                    [
                        "King Bed",
                        "Private Terrace",
                        "Jacuzzi",
                        "Butler Service",
                        "Minibar",
                        "Smart TV",
                        "WiFi",
                        "City View",
                    ]
                ),
                "location": "Manhattan, New York",
                "city": "New York",
                "country": "USA",
                "max_guests": 4,
                "beds": 2,
                "bathrooms": 3,
                "size_sqft": 2800,
                "floor": 52,
                "is_featured": True,
            },
            {
                "hotel_name": "Serenity Beach Resort",
                "room_type": "suite",
                "description": "Oceanfront suite with direct beach access, infinity pool view, and tropical ambiance.",
                "price": 420.00,
                "original_price": 580.00,
                "rating": 4.8,
                "review_count": 512,
                "image_url": "https://images.unsplash.com/photo-1520250497591-112f2f40a3f4?w=800",
                "gallery_urls": json.dumps(
                    [
                        "https://images.unsplash.com/photo-1520250497591-112f2f40a3f4?w=800",
                        "https://images.unsplash.com/photo-1571003123894-1f0594d2b5d9?w=800",
                    ]
                ),
                "amenities": json.dumps(
                    [
                        "Ocean View",
                        "Infinity Pool",
                        "Spa Access",
                        "King Bed",
                        "WiFi",
                        "Room Service",
                        "Private Balcony",
                    ]
                ),
                "location": "Bali, Indonesia",
                "city": "Bali",
                "country": "Indonesia",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 2,
                "size_sqft": 1200,
                "floor": 3,
                "is_featured": True,
            },
            {
                "hotel_name": "Alpine Summit Lodge",
                "room_type": "deluxe",
                "description": "Cozy mountain chalet room with fireplace, ski-in/ski-out access, and mountain views.",
                "price": 280.00,
                "original_price": 350.00,
                "rating": 4.7,
                "review_count": 198,
                "image_url": "https://images.unsplash.com/photo-1551882547-ff40c63fe5fa?w=800",
                "amenities": json.dumps(
                    [
                        "Fireplace",
                        "Mountain View",
                        "Ski-in/Ski-out",
                        "Hot Tub",
                        "WiFi",
                        "Breakfast Included",
                    ]
                ),
                "location": "Zermatt, Switzerland",
                "city": "Zermatt",
                "country": "Switzerland",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 1,
                "size_sqft": 650,
                "floor": 2,
                "is_featured": True,
            },
            {
                "hotel_name": "Kyoto Garden Inn",
                "room_type": "suite",
                "description": "Traditional Japanese suite with tatami floors, soaking tub, and zen garden view.",
                "price": 310.00,
                "rating": 4.9,
                "review_count": 445,
                "image_url": "https://images.unsplash.com/photo-1578683010236-d716f9a3f461?w=800",
                "amenities": json.dumps(
                    [
                        "Tatami Floor",
                        "Soaking Tub",
                        "Zen Garden View",
                        "Yukata Robes",
                        "Tea Ceremony",
                        "WiFi",
                    ]
                ),
                "location": "Gion District, Kyoto",
                "city": "Kyoto",
                "country": "Japan",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 1,
                "size_sqft": 900,
                "floor": 1,
                "is_featured": True,
            },
            {
                "hotel_name": "Metropolis Business Hotel",
                "room_type": "deluxe",
                "description": "Modern executive room with ergonomic workspace, high-speed WiFi, and city skyline views.",
                "price": 195.00,
                "original_price": 240.00,
                "rating": 4.6,
                "review_count": 820,
                "image_url": "https://images.unsplash.com/photo-1566665797739-1674de7a421a?w=800",
                "amenities": json.dumps(
                    [
                        "Work Desk",
                        "High-Speed WiFi",
                        "City View",
                        "Smart TV",
                        "Coffee Machine",
                        "Gym Access",
                    ]
                ),
                "location": "City Centre, London",
                "city": "London",
                "country": "UK",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 1,
                "size_sqft": 480,
                "floor": 15,
                "is_featured": False,
            },
            {
                "hotel_name": "Desert Mirage Palace",
                "room_type": "suite",
                "description": "Luxurious desert suite with private pool, Arabian decor, and spectacular dune views.",
                "price": 520.00,
                "rating": 4.8,
                "review_count": 167,
                "image_url": "https://images.unsplash.com/photo-1542314831-068cd1dbfeeb?w=800",
                "amenities": json.dumps(
                    [
                        "Private Pool",
                        "Desert View",
                        "Butler Service",
                        "Camel Ride",
                        "Spa",
                        "King Bed",
                    ]
                ),
                "location": "Dubai, UAE",
                "city": "Dubai",
                "country": "UAE",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 2,
                "size_sqft": 1800,
                "floor": 5,
                "is_featured": True,
            },
        ]

        for room_data in sample_rooms:
            room = __import__("models").Room(**room_data)
            db.add(room)
        db.commit()
        return {"message": f"Seeded {len(sample_rooms)} rooms successfully"}
    finally:
        db.close()
