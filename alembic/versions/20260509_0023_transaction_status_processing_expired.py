"""add processing and expired transaction statuses

Revision ID: 20260509_0023
Revises: 20260509_0022
Create Date: 2026-05-09
"""

from alembic import op
from sqlalchemy import text


revision = "20260509_0023"
down_revision = "20260509_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return

    enum_type = bind.execute(
        text(
            """
            SELECT n.nspname, t.typname
            FROM pg_type t
            JOIN pg_namespace n ON n.oid = t.typnamespace
            WHERE t.typtype = 'e'
              AND t.typname = 'transaction_status'
            """
        )
    ).first()
    if enum_type is None:
        return

    schema_name, type_name = enum_type
    quoted_schema = schema_name.replace('"', '""')
    quoted_type = type_name.replace('"', '""')
    for value in ("processing", "expired"):
        safe_value = value.replace("'", "''")
        op.execute(
            f'ALTER TYPE "{quoted_schema}"."{quoted_type}" '
            f"ADD VALUE IF NOT EXISTS '{safe_value}'"
        )


def downgrade() -> None:
    # PostgreSQL does not support removing enum values safely.
    pass
