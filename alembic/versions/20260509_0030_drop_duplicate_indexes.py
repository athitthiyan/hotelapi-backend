"""drop duplicate indexes caused by unique=True + index=True on same column

Revision ID: 20260509_0030
Revises: 20260509_0029
Create Date: 2026-05-09

When SQLAlchemy declares a column with both unique=True and index=True,
PostgreSQL ends up with two indexes on the same column:
  1. A unique constraint index (e.g. users_email_key)
  2. A plain btree index (e.g. ix_users_email)

The plain btree index is redundant — the unique index already covers all
btree lookups on that column. Dropping the plain ones reduces write overhead
and removes the "Duplicate Index" warnings in Supabase advisor.
"""

from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = "20260509_0030"
down_revision = "20260509_0029"
branch_labels = None
depends_on = None

# (index_name, table) — plain btree duplicates of unique constraint indexes
_DUPLICATE_INDEXES = [
    # users
    ("ix_users_email",                  "users"),
    ("ix_users_google_id",              "users"),
    ("ix_users_apple_id",               "users"),
    ("ix_users_microsoft_id",           "users"),
    # bookings
    ("ix_bookings_booking_ref",         "bookings"),
    # partner_hotels
    ("ix_partner_hotels_owner_user_id", "partner_hotels"),
    # partner_payouts
    ("ix_partner_payouts_payout_reference", "partner_payouts"),
    # password_reset_tokens
    ("ix_password_reset_tokens_token_hash", "password_reset_tokens"),
    # transactions
    ("ix_transactions_transaction_ref",  "transactions"),
    ("ix_transactions_idempotency_key",  "transactions"),
    # refresh_tokens
    ("ix_refresh_tokens_token_hash",     "refresh_tokens"),
]


def _existing_indexes(table: str) -> set:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # Only needed in PostgreSQL; SQLite dev is unaffected

    for index_name, table in _DUPLICATE_INDEXES:
        existing = _existing_indexes(table)
        if index_name in existing:
            op.drop_index(index_name, table_name=table)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # Recreate the plain btree indexes on downgrade
    _recreate = [
        ("ix_users_email",                  "users",                  ["email"]),
        ("ix_users_google_id",              "users",                  ["google_id"]),
        ("ix_users_apple_id",               "users",                  ["apple_id"]),
        ("ix_users_microsoft_id",           "users",                  ["microsoft_id"]),
        ("ix_bookings_booking_ref",         "bookings",               ["booking_ref"]),
        ("ix_partner_hotels_owner_user_id", "partner_hotels",         ["owner_user_id"]),
        ("ix_partner_payouts_payout_reference", "partner_payouts",    ["payout_reference"]),
        ("ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"]),
        ("ix_transactions_transaction_ref",  "transactions",           ["transaction_ref"]),
        ("ix_transactions_idempotency_key",  "transactions",           ["idempotency_key"]),
        ("ix_refresh_tokens_token_hash",     "refresh_tokens",         ["token_hash"]),
    ]
    for index_name, table, columns in _recreate:
        existing = _existing_indexes(table)
        if index_name not in existing:
            op.create_index(index_name, table, columns)
