"""add missing foreign key indexes (unindexed FK columns flagged by Supabase)

Revision ID: 20260509_0029
Revises: 20260509_0028
Create Date: 2026-05-09

Foreign keys without indexes cause sequential scans on JOINs and cascading
deletes. The following FK columns were missing indexes in the original schema.
"""

from alembic import op
from sqlalchemy.engine.reflection import Inspector

revision = "20260509_0029"
down_revision = "20260509_0028"
branch_labels = None
depends_on = None

# (index_name, table, columns)
_INDEXES = [
    ("ix_audit_logs_actor_user_id",                "audit_logs",         ["actor_user_id"]),
    ("ix_bookings_room_id",                        "bookings",           ["room_id"]),
    ("ix_notification_outbox_booking_id",          "notification_outbox",["booking_id"]),
    ("ix_notification_outbox_transaction_id",      "notification_outbox",["transaction_id"]),
    ("ix_room_inventory_locked_by_booking_id",     "room_inventory",     ["locked_by_booking_id"]),
    ("ix_transactions_booking_id",                 "transactions",       ["booking_id"]),
    ("ix_transactions_retry_of_transaction_id",    "transactions",       ["retry_of_transaction_id"]),
    ("ix_reviews_booking_id",                      "reviews",            ["booking_id"]),
]


def _existing_indexes(table: str) -> set:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {ix["name"] for ix in inspector.get_indexes(table)}


def upgrade() -> None:
    for index_name, table, columns in _INDEXES:
        existing = _existing_indexes(table)
        if index_name not in existing:
            op.create_index(index_name, table, columns)


def downgrade() -> None:
    for index_name, table, _columns in _INDEXES:
        existing = _existing_indexes(table)
        if index_name in existing:
            op.drop_index(index_name, table_name=table)
