<div align="center">

# HotelAPI Backend

Unified FastAPI backend powering the Stayvora platform — bookings, payments, inventory, analytics, and partner operations.

[![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Railway](https://img.shields.io/badge/Deployed-Railway-0B0D0E?logo=railway&logoColor=white)](https://railway.app/)
[![CI](https://github.com/athitthiyan/hotelapi-backend/actions/workflows/ci.yml/badge.svg)](https://github.com/athitthiyan/hotelapi-backend/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

**Live API:** [hotel-api-production-447d.up.railway.app/docs](https://hotel-api-production-447d.up.railway.app/docs) | **Platform:** [stayvora.co.in](https://stayvora.co.in)

</div>

---

## About

**HotelAPI** is the shared backend for the Stayvora platform. It serves four frontend apps — the guest booking app, the payment flow, the admin analytics dashboard, and the partner portal — via a single FastAPI service running on Railway with a Supabase PostgreSQL database.

## Features

- Room search and availability with date-range filtering
- Booking creation, hold management, and cancellation
- Stripe payment intent creation and webhook handling
- Partner inventory and hotel profile management
- Admin analytics endpoints for revenue, booking stats, and KPIs
- Alembic-managed schema migrations

## Tech Stack

| Layer | Technology |
| --- | --- |
| Framework | FastAPI |
| Language | Python 3.11 |
| ORM | SQLAlchemy |
| Validation | Pydantic v2 |
| Migrations | Alembic |
| Database | Supabase PostgreSQL |
| Payments | Stripe |
| Deployment | Railway |

## Quick Start

### Prerequisites

- Python 3.11+
- PostgreSQL (or a Supabase project URL)

### Setup

```bash
git clone https://github.com/athitthiyan/hotelapi-backend.git
cd hotelapi-backend
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your `DATABASE_URL` and Stripe keys, then:

```bash
alembic upgrade head
uvicorn main:app --reload --port 8000
```

API docs available at [http://localhost:8000/docs](http://localhost:8000/docs).

## Database Migrations

```bash
# Apply all pending migrations
alembic upgrade head

# Create a new migration after model changes
alembic revision --autogenerate -m "describe change"
```

For local-only bootstrapping you can set `AUTO_CREATE_SCHEMA=true`. Keep it disabled in production so schema changes stay explicit and repeatable.

## Railway Deployment

1. Create a Railway service from this repo.
2. Set `DATABASE_URL` in Railway Variables (Supabase connection string).
3. Set the start command to `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Run `alembic upgrade head` before serving production traffic.
5. Redeploy and verify `/health`.

## Architecture

```text
Stayvora Booking  ─┐
PayFlow           ─┤─> HotelAPI (Railway) -> Supabase PostgreSQL
InsightBoard      ─┤
Partner Portal    ─┘
```

## Connected Apps

| App | Repository | Purpose |
| --- | --- | --- |
| Stayvora Booking | [athitthiyan/stayease-booking-app](https://github.com/athitthiyan/stayease-booking-app) | Guest-facing booking frontend |
| PayFlow | [athitthiyan/payflow-payment-app](https://github.com/athitthiyan/payflow-payment-app) | Payment processing |
| InsightBoard | [athitthiyan/insightboard-admin](https://github.com/athitthiyan/insightboard-admin) | Admin analytics dashboard |
| Partner Portal | [athitthiyan/partner-portal](https://github.com/athitthiyan/partner-portal) | Hotel-partner operations |

## Contributing

Contributions are welcome — bug reports, feature ideas, and pull requests. See [CONTRIBUTING.md](CONTRIBUTING.md) and please follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## License

This project is licensed under the [MIT License](LICENSE).
