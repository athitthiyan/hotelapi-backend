"""phone verification fields

Revision ID: 20260406_0015
Revises: 20260406_0014
Create Date: 2026-04-06 23:55:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260406_0015"
down_revision = "20260406_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("users", sa.Column("pending_phone", sa.String(length=30), nullable=True))
    op.add_column("users", sa.Column("phone_otp_hash", sa.String(length=64), nullable=True))
    op.add_column("users", sa.Column("phone_otp_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("phone_otp_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.alter_column("users", "phone_verified", server_default=None)
    op.alter_column("users", "phone_otp_attempts", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "phone_otp_attempts")
    op.drop_column("users", "phone_otp_expires_at")
    op.drop_column("users", "phone_otp_hash")
    op.drop_column("users", "pending_phone")
    op.drop_column("users", "phone_verified")
