"""partner inventory pricing fields

Revision ID: 20260406_0012
Revises: 0011
Create Date: 2026-04-06 19:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260406_0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("rooms", sa.Column("room_type_name", sa.String(length=120), nullable=True))
    op.add_column("rooms", sa.Column("total_room_count", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("rooms", sa.Column("weekend_price", sa.Float(), nullable=True))
    op.add_column("rooms", sa.Column("holiday_price", sa.Float(), nullable=True))
    op.add_column("rooms", sa.Column("extra_guest_charge", sa.Float(), nullable=False, server_default="0"))
    op.add_column("rooms", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("rooms", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(
        """
        UPDATE rooms
        SET room_type_name = CASE room_type
            WHEN 'standard' THEN 'Standard'
            WHEN 'deluxe' THEN 'Deluxe'
            WHEN 'suite' THEN 'Suite'
            WHEN 'penthouse' THEN 'Penthouse'
            ELSE 'Standard'
        END
        """
    )
    op.alter_column("rooms", "room_type_name", nullable=False)

    op.add_column("room_inventory", sa.Column("booked_units", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("room_inventory", sa.Column("blocked_units", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("room_inventory", sa.Column("block_reason", sa.String(length=120), nullable=True))
    op.add_column("room_inventory", sa.Column("price_override", sa.Float(), nullable=True))
    op.add_column("room_inventory", sa.Column("price_override_label", sa.String(length=120), nullable=True))


def downgrade() -> None:
    op.drop_column("room_inventory", "price_override_label")
    op.drop_column("room_inventory", "price_override")
    op.drop_column("room_inventory", "block_reason")
    op.drop_column("room_inventory", "blocked_units")
    op.drop_column("room_inventory", "booked_units")

    op.drop_column("rooms", "deleted_at")
    op.drop_column("rooms", "is_active")
    op.drop_column("rooms", "extra_guest_charge")
    op.drop_column("rooms", "holiday_price")
    op.drop_column("rooms", "weekend_price")
    op.drop_column("rooms", "total_room_count")
    op.drop_column("rooms", "room_type_name")
