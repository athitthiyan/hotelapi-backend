"""partner inventory pricing fields

Revision ID: 20260406_0012
Revises: 0011
Create Date: 2026-04-06 19:10:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "20260406_0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    inspector = Inspector.from_engine(op.get_bind())
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    room_columns = _existing_columns("rooms")
    if "room_type_name" not in room_columns:
        op.add_column("rooms", sa.Column("room_type_name", sa.String(length=120), nullable=True))
    if "total_room_count" not in room_columns:
        op.add_column("rooms", sa.Column("total_room_count", sa.Integer(), nullable=False, server_default="1"))
    if "weekend_price" not in room_columns:
        op.add_column("rooms", sa.Column("weekend_price", sa.Float(), nullable=True))
    if "holiday_price" not in room_columns:
        op.add_column("rooms", sa.Column("holiday_price", sa.Float(), nullable=True))
    if "extra_guest_charge" not in room_columns:
        op.add_column("rooms", sa.Column("extra_guest_charge", sa.Float(), nullable=False, server_default="0"))
    if "is_active" not in room_columns:
        op.add_column("rooms", sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))
    if "deleted_at" not in room_columns:
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
    with op.batch_alter_table("rooms") as batch_op:
        batch_op.alter_column(
            "room_type_name",
            existing_type=sa.String(length=120),
            nullable=False,
        )

    inventory_columns = _existing_columns("room_inventory")
    if "booked_units" not in inventory_columns:
        op.add_column("room_inventory", sa.Column("booked_units", sa.Integer(), nullable=False, server_default="0"))
    if "blocked_units" not in inventory_columns:
        op.add_column("room_inventory", sa.Column("blocked_units", sa.Integer(), nullable=False, server_default="0"))
    if "block_reason" not in inventory_columns:
        op.add_column("room_inventory", sa.Column("block_reason", sa.String(length=120), nullable=True))
    if "price_override" not in inventory_columns:
        op.add_column("room_inventory", sa.Column("price_override", sa.Float(), nullable=True))
    if "price_override_label" not in inventory_columns:
        op.add_column("room_inventory", sa.Column("price_override_label", sa.String(length=120), nullable=True))


def downgrade() -> None:
    inventory_columns = _existing_columns("room_inventory")
    for column in (
        "price_override_label",
        "price_override",
        "block_reason",
        "blocked_units",
        "booked_units",
    ):
        if column in inventory_columns:
            op.drop_column("room_inventory", column)

    room_columns = _existing_columns("rooms")
    for column in (
        "deleted_at",
        "is_active",
        "extra_guest_charge",
        "holiday_price",
        "weekend_price",
        "total_room_count",
        "room_type_name",
    ):
        if column in room_columns:
            op.drop_column("rooms", column)
