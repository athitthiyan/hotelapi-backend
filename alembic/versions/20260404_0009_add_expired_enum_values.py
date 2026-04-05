"""Add 'expired' to booking_status and payment_status enums

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-04
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ADD VALUE cannot run inside a transaction block in PostgreSQL.
    # We use the raw DBAPI connection with autocommit to work around this.
    conn = op.get_bind()
    raw = conn.connection
    old_isolation = raw.isolation_level
    raw.set_isolation_level(0)  # AUTOCOMMIT
    try:
        cursor = raw.cursor()
        cursor.execute("ALTER TYPE booking_status ADD VALUE IF NOT EXISTS 'expired'")
        cursor.execute("ALTER TYPE payment_status ADD VALUE IF NOT EXISTS 'expired'")
        cursor.close()
    finally:
        raw.set_isolation_level(old_isolation)


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade is a no-op
    pass
