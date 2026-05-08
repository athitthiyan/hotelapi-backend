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
    # SQLite has no enum types — skip entirely
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    # ALTER TYPE ADD VALUE cannot run inside a transaction block in PostgreSQL.
    # We use the raw DBAPI connection with autocommit to work around this.
    raw = bind.connection
    old_isolation = raw.isolation_level
    raw.set_isolation_level(0)  # AUTOCOMMIT
    try:
        cursor = raw.cursor()
        try:
            cursor.execute(
                """
                SELECT n.nspname, t.typname
                FROM pg_type t
                JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE t.typtype = 'e'
                  AND t.typname IN ('booking_status', 'payment_status')
                """
            )
            enum_types = {row[1]: row[0] for row in cursor.fetchall()}

            for type_name in ("booking_status", "payment_status"):
                schema_name = enum_types.get(type_name)
                if schema_name is None:
                    # Some fresh/recovered installs use non-native enum columns,
                    # so there is no PostgreSQL enum type to alter.
                    continue
                quoted_schema = schema_name.replace('"', '""')
                quoted_type = type_name.replace('"', '""')
                cursor.execute(
                    f'ALTER TYPE "{quoted_schema}"."{quoted_type}" '
                    "ADD VALUE IF NOT EXISTS 'expired'"
                )
        finally:
            cursor.close()
    finally:
        raw.set_isolation_level(old_isolation)


def downgrade() -> None:
    # PostgreSQL does not support removing enum values; downgrade is a no-op
    pass
