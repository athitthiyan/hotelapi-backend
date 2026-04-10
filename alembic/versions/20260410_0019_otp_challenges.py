"""add otp_challenges table

Revision ID: 20260410_0019
Revises: 20260408_0018
Create Date: 2026-04-10 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260410_0019"
down_revision = "20260408_0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "otp_challenges",
        sa.Column("id", sa.String(length=36), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column("flow", sa.String(length=32), nullable=False, index=True),
        sa.Column("channel", sa.String(length=16), nullable=False, index=True),
        sa.Column("recipient", sa.String(length=200), nullable=False, index=True),
        sa.Column("otp_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("resend_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_resends", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("resend_available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("device_fingerprint", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("otp_challenges")
