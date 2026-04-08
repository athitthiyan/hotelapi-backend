import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from jose import jwt, JWTError

import models
from database import Base, SessionLocal, engine, settings, validate_runtime_configuration
from routers import analytics, auth, bookings, notifications, ops, partner, payments, razorpay_payments, reviews, rooms, wishlist

try:
    from apscheduler.schedulers.background import BackgroundScheduler
except ImportError:  # pragma: no cover - guarded for environments without optional dependency
    BackgroundScheduler = None

logger = logging.getLogger(__name__)

def run_expired_hold_release(session_factory=SessionLocal) -> int:
    db = session_factory()
    try:
        return bookings.release_expired_holds(db)
    finally:
        db.close()


def startup_checks(state) -> None:
    """Verify database connectivity and schema readiness at startup."""
    try:
        validate_runtime_configuration(settings)
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
    except (SQLAlchemyError, RuntimeError) as exc:
        logger.exception("Database initialization failed during startup: %s", exc)

    if BackgroundScheduler is None:
        logger.warning("APScheduler is not installed; expired-hold scheduler is disabled.")
        state.hold_expiry_scheduler = None
        return

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(run_expired_hold_release, "interval", minutes=2, id="release-expired-holds", replace_existing=True)
    scheduler.start()
    state.hold_expiry_scheduler = scheduler


def shutdown_scheduler(state) -> None:
    scheduler = getattr(state, "hold_expiry_scheduler", None)
    if scheduler:
        scheduler.shutdown(wait=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    startup_checks(app.state)
    try:
        yield
    finally:
        shutdown_scheduler(app.state)


app = FastAPI(
    title="HotelAPI - Stayvora Backend",
    description="Unified backend for Stayvora booking, Stayvora Pay payments, Stayvora Admin operations, and partner workflows",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# Always-allowed production origins (code-level guarantee, not reliant on env var)
_HARDCODED_ORIGINS = [
    "https://stayvora.co.in",
    "https://www.stayvora.co.in",
    "https://pay.stayvora.co.in",
    "https://admin.stayvora.co.in",
    "https://partner.stayvora.co.in",
    "https://stayease-booking-app.vercel.app",
    "https://stayease-booking-app-git-main-athitthiyans-projects.vercel.app",
    "https://payflow-payment-app.vercel.app",
    "https://insightboard-admin.vercel.app",
    "https://stayease-partner-portal.vercel.app",
    "http://localhost:4200",
    "http://localhost:4201",
    "http://localhost:4202",
    "http://localhost:4203",
]
_env_origins = [o.strip() for o in settings.allowed_origins.split(",") if o.strip()]
origins = list(set(_env_origins + _HARDCODED_ORIGINS))
logger.info("Configured CORS origins: %s", ", ".join(sorted(origins)))

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
app.include_router(razorpay_payments.router)
app.include_router(analytics.router)
app.include_router(auth.router)
app.include_router(partner.router)
app.include_router(notifications.router)
app.include_router(ops.router)
app.include_router(reviews.router)
app.include_router(wishlist.router)


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


# ═══ Platform Sync — WebSocket Event Hub ═══════════════════════════════════
# Broadcasts real-time events to all connected frontends (customer, partner, admin).
# Event flow: Partner/Admin action → API save → broadcast_event() → all clients refresh.

class ConnectionManager:
    """Manages WebSocket connections for real-time platform sync, grouped by user role."""

    # Event routing rules: which roles can receive which event types
    EVENT_ROUTING = {
        "customer": {
            "booking-created",
            "booking-confirmed",
            "booking-cancelled",
            "booking-expired",
            "payment-completed",
            "refund-initiated",
            "refund-completed",
        },
        "partner": {
            "room-updated",
            "price-updated",
            "availability-updated",
            "booking-created",
            "booking-confirmed",
            "booking-cancelled",
            "inventory-updated",
            "payout-settled",
        },
        "admin": None,  # Admin receives ALL events
    }

    def __init__(self):
        self.connections_by_role: dict[str, Set[WebSocket]] = {
            "customer": set(),
            "partner": set(),
            "admin": set(),
        }

    async def connect(self, websocket: WebSocket, role: str = "customer"):
        """Accept connection and group by role."""
        await websocket.accept()
        # Normalize role; default to customer if unrecognized
        role = role if role in self.connections_by_role else "customer"
        self.connections_by_role[role].add(websocket)
        total = sum(len(conns) for conns in self.connections_by_role.values())
        logger.info("WebSocket client connected with role '%s'. Total: %d", role, total)

    def disconnect(self, websocket: WebSocket, role: str = "customer"):
        """Remove connection from role group."""
        role = role if role in self.connections_by_role else "customer"
        self.connections_by_role[role].discard(websocket)
        total = sum(len(conns) for conns in self.connections_by_role.values())
        logger.info("WebSocket client disconnected. Total: %d", total)

    def _should_receive_event(self, role: str, event_type: str) -> bool:
        """Check if a role can receive this event type."""
        if role == "admin":
            return True  # Admin receives all events
        allowed = self.EVENT_ROUTING.get(role, set())
        return event_type in allowed if allowed else False

    async def broadcast(self, event: dict):
        """Broadcast a platform event only to roles that should receive it."""
        event_type = event.get("type", "unknown")
        message = json.dumps(event)
        disconnected = {role: set() for role in self.connections_by_role}

        for role, connections in self.connections_by_role.items():
            if not self._should_receive_event(role, event_type):
                continue  # Skip roles that shouldn't receive this event

            for connection in connections:
                try:
                    await connection.send_text(message)
                except Exception:
                    disconnected[role].add(connection)

        # Clean up disconnected connections
        for role, conns in disconnected.items():
            for conn in conns:
                self.connections_by_role[role].discard(conn)

ws_manager = ConnectionManager()

def broadcast_event(event_type: str, payload: dict, source: str = "system"):
    """Fire-and-forget broadcast helper for sync routes."""
    import datetime
    event = {
        "type": event_type,
        "payload": payload,
        "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        "source": source,
    }
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(ws_manager.broadcast(event))
        else:
            loop.run_until_complete(ws_manager.broadcast(event))
    except RuntimeError:
        pass  # No event loop available (test/CLI context)

@app.websocket("/ws/events")
async def websocket_events(websocket: WebSocket):
    """WebSocket endpoint for real-time platform synchronization with JWT authentication."""
    token = websocket.query_params.get("token")
    user_role = "customer"  # default role

    if not token:
        await websocket.close(code=4001, reason="Missing token")
        return

    try:
        # Verify JWT using the same secret and algorithm as auth.py
        payload = jwt.decode(token, settings.secret_key, algorithms=[auth.ALGORITHM])
        # Determine user role from JWT payload
        if payload.get("is_admin"):
            user_role = "admin"
        elif payload.get("is_partner"):
            user_role = "partner"
        else:
            user_role = "customer"
    except JWTError:
        await websocket.close(code=4001, reason="Invalid token")
        return

    await ws_manager.connect(websocket, role=user_role)
    try:
        while True:
            # Keep connection alive; clients can also send events
            data = await websocket.receive_text()
            try:
                event = json.loads(data)
                event.setdefault("source", "client")
                await ws_manager.broadcast(event)
            except (json.JSONDecodeError, TypeError):
                pass
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket, role=user_role)


@app.post("/seed", tags=["Dev"])
def seed_database(db=None):
    """Seed the database with launch-ready starter rooms and operator accounts."""
    db = SessionLocal()
    try:
        rooms_created = 0

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
                "latitude": 40.7580,
                "longitude": -73.9855,
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
                "latitude": -8.4095,
                "longitude": 115.1889,
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
                "latitude": 46.0207,
                "longitude": 7.7491,
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
                "latitude": 35.0036,
                "longitude": 135.7756,
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
                "latitude": 51.5074,
                "longitude": -0.1278,
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
                "latitude": 25.2048,
                "longitude": 55.2708,
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 2,
                "size_sqft": 1800,
                "floor": 5,
                "is_featured": True,
            },
        ]

        existing_room_count = db.query(models.Room).count()
        if existing_room_count == 0:
            for room_data in sample_rooms:
                room = models.Room(**room_data)
                db.add(room)
                rooms_created += 1

        admin_user = db.query(models.User).filter(
            models.User.email == settings.seed_admin_email
        ).first()
        admin_created = False
        if not admin_user:
            admin_user = models.User(
                email=settings.seed_admin_email,
                full_name=settings.seed_admin_name,
                hashed_password=auth.hash_password(settings.seed_admin_password),
                is_admin=True,
                is_active=True,
            )
            db.add(admin_user)
            admin_created = True

        partner_user = db.query(models.User).filter(
            models.User.email == settings.seed_partner_email
        ).first()
        partner_created = False
        partner_hotel_created = False
        partner_room_created = False
        if not partner_user:
            partner_user = models.User(
                email=settings.seed_partner_email,
                full_name=settings.seed_partner_name,
                hashed_password=auth.hash_password(settings.seed_partner_password),
                is_admin=False,
                is_partner=True,
                is_active=True,
            )
            db.add(partner_user)
            db.flush()
            partner_created = True

        partner_hotel = db.query(models.PartnerHotel).filter(
            models.PartnerHotel.owner_user_id == partner_user.id
        ).first()
        if not partner_hotel:
            partner_hotel = models.PartnerHotel(
                owner_user_id=partner_user.id,
                legal_name="Stayvora Hospitality Private Limited",
                display_name=settings.seed_partner_hotel_name,
                gst_number="33ABCDE1234F1Z5",
                support_email=settings.seed_partner_email,
                support_phone="+91 98765 43210",
                address_line="12 Marina Beach Road",
                city="Chennai",
                state="Tamil Nadu",
                country="India",
                postal_code="600001",
                description="Partner launch starter hotel for Stayvora onboarding, room operations, and payout testing.",
                verified_badge=True,
                instant_confirmation_enabled=True,
                free_cancellation_enabled=True,
                bank_account_name=settings.seed_partner_hotel_name,
                bank_account_number_masked="********9012",
                bank_ifsc="HDFC0001234",
                bank_upi_id="stayvora@upi",
            )
            db.add(partner_hotel)
            db.flush()
            partner_hotel_created = True

        partner_room = db.query(models.Room).filter(
            models.Room.partner_hotel_id == partner_hotel.id
        ).first()
        if not partner_room:
            partner_room = models.Room(
                partner_hotel_id=partner_hotel.id,
                hotel_name=partner_hotel.display_name,
                room_type=models.RoomType.SUITE,
                description="Partner seeded room with breakfast, beach access, and GST-ready invoice support.",
                price=4800.0,
                original_price=5600.0,
                availability=True,
                rating=4.7,
                review_count=128,
                image_url="https://images.unsplash.com/photo-1566073771259-6a8506099945?w=800",
                gallery_urls=json.dumps(
                    [
                        "https://images.unsplash.com/photo-1566073771259-6a8506099945?w=800",
                        "https://images.unsplash.com/photo-1505693416388-ac5ce068fe85?w=800",
                    ]
                ),
                amenities=json.dumps(
                    [
                        "Breakfast included",
                        "Free WiFi",
                        "Beach access",
                        "Airport transfer",
                        "Family friendly",
                    ]
                ),
                location="Near Marina Beach",
                city="Chennai",
                country="India",
                latitude=13.0500,
                longitude=80.2824,
                max_guests=3,
                beds=2,
                bathrooms=1,
                size_sqft=420,
                floor=4,
                is_featured=True,
            )
            db.add(partner_room)
            partner_room_created = True

        db.commit()

        return {
            "message": "Seed completed successfully",
            "rooms_created": rooms_created,
            "admin_created": admin_created,
            "admin_email": settings.seed_admin_email,
            "admin_name": settings.seed_admin_name,
            "partner_created": partner_created,
            "partner_hotel_created": partner_hotel_created,
            "partner_room_created": partner_room_created,
            "partner_email": settings.seed_partner_email,
            "partner_name": settings.seed_partner_name,
            "partner_hotel_name": settings.seed_partner_hotel_name,
        }
    finally:
        db.close()


# ── Geocoding API endpoint for partner onboarding ────────────────────────
@app.post("/api/geocode", tags=["Location"])
async def geocode_address(payload: dict):
    """
    Resolve an address string to latitude/longitude coordinates.
    Uses OpenStreetMap Nominatim as the geocoding provider (via stdlib urllib).
    Frontend sends: { "address": "12 Marina Beach Road, Chennai, Tamil Nadu, India" }
    Returns: { "latitude": 13.05, "longitude": 80.28, "formatted_address": "...", "found": true }
    """
    import asyncio
    import json as _json
    import urllib.request
    import urllib.parse

    address = payload.get("address", "").strip()
    if not address:
        return {"found": False, "error": "Address is required"}

    def _fetch_nominatim(addr: str):
        params = urllib.parse.urlencode({
            "q": addr,
            "format": "json",
            "limit": 1,
            "addressdetails": 1,
        })
        url = f"https://nominatim.openstreetmap.org/search?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Stayvora/1.0 (hotel-platform)"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return _json.loads(resp.read().decode())

    try:
        loop = asyncio.get_event_loop()
        results = await loop.run_in_executor(None, _fetch_nominatim, address)

        if not results:
            return {"found": False, "error": "Could not geocode this address. Please try a more specific address."}

        result = results[0]
        return {
            "found": True,
            "latitude": float(result["lat"]),
            "longitude": float(result["lon"]),
            "formatted_address": result.get("display_name", address),
        }
    except Exception as exc:
        logger.warning("Geocoding failed for '%s': %s", address, exc)
        return {"found": False, "error": "Geocoding service temporarily unavailable. You can adjust the pin manually."}


# ── Backfill coordinates for rooms that were seeded without them ──────────
CITY_COORDINATES = {
    "new york": (40.7580, -73.9855),
    "bali": (-8.4095, 115.1889),
    "zermatt": (46.0207, 7.7491),
    "kyoto": (35.0036, 135.7756),
    "london": (51.5074, -0.1278),
    "dubai": (25.2048, 55.2708),
    "chennai": (13.0500, 80.2824),
}


@app.post("/seed/backfill-coordinates", tags=["Dev"])
def backfill_room_coordinates():
    """Patch latitude/longitude on existing rooms that are missing coordinates."""
    import random
    db = SessionLocal()
    try:
        rooms = db.query(models.Room).filter(
            models.Room.latitude.is_(None)
        ).all()

        updated = 0
        for room in rooms:
            city_key = (room.city or "").strip().lower()
            if city_key in CITY_COORDINATES:
                lat, lng = CITY_COORDINATES[city_key]
                # Add slight offset per room so markers don't stack exactly
                room.latitude = lat + random.uniform(-0.008, 0.008)
                room.longitude = lng + random.uniform(-0.008, 0.008)
                updated += 1

        db.commit()
        return {"message": f"Backfilled coordinates for {updated} rooms", "updated": updated}
    finally:
        db.close()
