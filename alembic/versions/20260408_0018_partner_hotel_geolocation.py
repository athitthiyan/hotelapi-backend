"""Add geolocation fields to partner_hotels table

Revision ID: 20260408_0018
Revises: 20260407_0017
Create Date: 2026-04-08 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260408_0018"
down_revision = "20260407_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "partner_hotels",
        sa.Column("latitude", sa.Float(), nullable=True),
    )
    op.add_column(
        "partner_hotels",
        sa.Column("longitude", sa.Float(), nullable=True),
    )
    op.add_column(
        "partner_hotels",
        sa.Column("formatted_address", sa.String(500), nullable=True),
    )
    op.add_column(
        "partner_hotels",
        sa.Column("location_verified", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("partner_hotels", "location_verified")
    op.drop_column("partner_hotels", "formatted_address")
    op.drop_column("partner_hotels", "longitude")
    op.drop_column("partner_hotels", "latitude")
