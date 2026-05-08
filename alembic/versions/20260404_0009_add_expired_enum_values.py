"""Add 'expired' to booking_status and payment_status enums

Revision ID: 0009
Revises: 0008
Create Date: 2026-04-04
"""

from alembic import op
from sqlalchemy import text

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite has no enum types — skip entirely
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    existing_types = {
        row[0]
        for row in bind.execute(
            text(
                """
                SELECT typname
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE n.nspname = current_schema()
                  AND t.typtype = 'e'
                  AND typname IN ('booking_status', 'payment_status')
                """
            )
        )
    }
    if {"booking_status", "payment_status"} - existing_types:
        # Fresh installs may use non-native enum columns, so there is no
        # PostgreSQL enum type to alter. Existing installs with native enum
        # types are still updated below.
        return

    # ALTER TYPE ADD VALUE cannot run inside a transaction block in PostgreSQL.
    # We use the raw DBAPI connection with autocommit to work around this.
    raw = bind.connection
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
