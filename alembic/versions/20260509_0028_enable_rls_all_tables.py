"""enable RLS on all public app tables (no policies = default deny for PostgREST)

Revision ID: 20260509_0028
Revises: 20260509_0027
Create Date: 2026-05-09

This FastAPI backend connects to PostgreSQL directly as the postgres/service_role
user, which has BYPASSRLS. Enabling RLS with zero policies means:
  - anon / authenticated PostgREST clients → access denied (default deny)
  - SQLAlchemy / Alembic / service_role    → unaffected (BYPASSRLS)
"""

from alembic import op

revision = "20260509_0028"
down_revision = "20260509_0027"
branch_labels = None
depends_on = None

# All application tables that need RLS enabled
_TABLES = [
    "users",
    "partner_hotels",
    "rooms",
    "bookings",
    "transactions",
    "notification_outbox",
    "room_inventory",
    "refresh_tokens",
    "audit_logs",
    "admin_notifications",
    "partner_payouts",
    "reviews",
    "wishlists",
    "password_reset_tokens",
    "otp_challenges",
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # RLS is PostgreSQL-only; no-op on SQLite dev

    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table in _TABLES:
        op.execute(f"ALTER TABLE public.{table} DISABLE ROW LEVEL SECURITY;")
