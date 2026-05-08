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

    enum_types = {
        row[1]: row[0]
        for row in bind.execute(
            text(
                """
                SELECT n.nspname, t.typname
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE t.typtype = 'e'
                  AND t.typname IN ('booking_status', 'payment_status')
                """
            )
        )
    }

    for type_name in ("booking_status", "payment_status"):
        schema_name = enum_types.get(type_name)
        if schema_name is None:
            # Some fresh/recovered installs use non-native enum columns,
            # so there is no PostgreSQL enum type to alter.
            continue
        quoted_schema = schema_name.replace('"', '""')
        quoted_type = type_name.replace('"', '""')
        op.execute(
            f'ALTER TYPE "{quoted_schema}"."{quoted_type}" '
            "ADD VALUE IF NOT EXISTS 'expired'"
        )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade is a no-op
    pass
