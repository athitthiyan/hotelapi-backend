"""add refresh_tokens table for token rotation and revocation

Revision ID: 20260407_0017
Revises: 20260407_0016
Create Date: 2026-04-07 14:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260407_0017"
down_revision = "20260407_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Integer(), primary_key=True, index=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False, index=True),
        sa.Column("family_id", sa.String(36), nullable=False, index=True),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("refresh_tokens")
