"""add email verification columns to users table

Revision ID: 20260509_0025
Revises: 20260509_0024
Create Date: 2026-05-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "20260509_0025"
down_revision = "20260509_0024"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    cols = _existing_columns("users")

    if "is_email_verified" not in cols:
        op.add_column(
            "users",
            sa.Column(
                "is_email_verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    if "email_verification_token" not in cols:
        op.add_column(
            "users",
            sa.Column("email_verification_token", sa.String(128), nullable=True),
        )

    if "email_verification_expires_at" not in cols:
        op.add_column(
            "users",
            sa.Column(
                "email_verification_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    cols = _existing_columns("users")
    for col in ("email_verification_expires_at", "email_verification_token", "is_email_verified"):
        if col in cols:
            op.drop_column("users", col)
