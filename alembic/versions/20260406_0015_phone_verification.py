"""phone verification fields

Revision ID: 20260406_0015
Revises: 20260406_0014
Create Date: 2026-04-06 23:55:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "20260406_0015"
down_revision = "20260406_0014"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    inspector = Inspector.from_engine(op.get_bind())
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    columns = _existing_columns("users")
    if "phone_verified" not in columns:
        op.add_column(
            "users",
            sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        )
    if "pending_phone" not in columns:
        op.add_column("users", sa.Column("pending_phone", sa.String(length=30), nullable=True))
    if "phone_otp_hash" not in columns:
        op.add_column("users", sa.Column("phone_otp_hash", sa.String(length=64), nullable=True))
    if "phone_otp_expires_at" not in columns:
        op.add_column("users", sa.Column("phone_otp_expires_at", sa.DateTime(timezone=True), nullable=True))
    if "phone_otp_attempts" not in columns:
        op.add_column(
            "users",
            sa.Column("phone_otp_attempts", sa.Integer(), nullable=False, server_default="0"),
        )

    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column(
            "phone_verified",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "phone_otp_attempts",
            existing_type=sa.Integer(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    columns = _existing_columns("users")
    for column in (
        "phone_otp_attempts",
        "phone_otp_expires_at",
        "phone_otp_hash",
        "pending_phone",
        "phone_verified",
    ):
        if column in columns:
            op.drop_column("users", column)
