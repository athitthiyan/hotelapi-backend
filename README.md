# HotelAPI Backend

Unified FastAPI backend for StayEase, PayFlow, and InsightBoard.

## Stack

- FastAPI
- SQLAlchemy
- PostgreSQL
- Alembic
- Stripe

## Local Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

## Railway Deployment

1. Create a Railway service from this repo.
2. Set `DATABASE_URL` in Railway Variables.
3. Set the start command to `uvicorn main:app --host 0.0.0.0 --port $PORT`.
4. Run `alembic upgrade head` before serving production traffic.
5. Redeploy and check `/health`.

## Database Migrations

This repo now includes Alembic migration scaffolding and an initial schema revision.

```bash
alembic upgrade head
```

For local-only bootstrapping, you can temporarily set `AUTO_CREATE_SCHEMA=true`. Keep it disabled in production so schema changes stay explicit and repeatable.
