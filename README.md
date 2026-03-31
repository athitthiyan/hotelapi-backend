# ⚙️ HotelAPI — FastAPI Backend

> Unified REST API powering StayEase, PayFlow, and InsightBoard.

**Live API:** [hotel-api.onrender.com](https://hotel-api.onrender.com)
**API Docs:** [hotel-api.onrender.com/docs](https://hotel-api.onrender.com/docs)

---

## 🛠️ Tech Stack

| Layer       | Technology              |
|-------------|-------------------------|
| Framework   | FastAPI 0.111           |
| ORM         | SQLAlchemy 2.0          |
| Database    | PostgreSQL (Supabase)   |
| Validation  | Pydantic v2             |
| Payments    | Stripe Python SDK       |
| Deployment  | Render                  |

## 📡 API Endpoints

### Rooms
```
GET  /rooms                 List rooms (city, type, price, guests filters)
GET  /rooms/featured        Featured rooms
GET  /rooms/{id}            Room detail
POST /rooms                 Create room (admin)
```

### Bookings
```
POST /bookings              Create booking
GET  /bookings              List bookings
GET  /bookings/history      Booking history by email
GET  /bookings/{id}         Booking detail
GET  /bookings/ref/{ref}    Lookup by booking reference
PATCH /bookings/{id}/cancel Cancel booking
```

### Payments
```
POST /payments/create-payment-intent   Create Stripe or mock intent
POST /payments/payment-success         Confirm + create transaction
POST /payments/payment-failure         Record failed payment
GET  /payments/transactions            List transactions
```

### Analytics
```
GET  /analytics             KPIs + charts data (days param)
GET  /analytics/recent-bookings
GET  /analytics/revenue-stats
```

## 🚀 Local Setup

```bash
# Clone and install
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure .env (copy from .env.example)
cp .env.example .env

# Run
uvicorn main:app --reload --port 8000

# Seed sample data
curl -X POST http://localhost:8000/seed
```

---

*Built by Athitthiyan — Portfolio 2026*
