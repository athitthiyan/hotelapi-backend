"""Add booking user linkage, normalize emails, and enforce room inventory uniqueness.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def _inspector() -> Inspector:
    return Inspector.from_engine(op.get_bind())


def _existing_columns(table: str) -> set[str]:
    return {col["name"] for col in _inspector().get_columns(table)}


def _existing_indexes(table: str) -> set[str]:
    return {idx["name"] for idx in _inspector().get_indexes(table)}


def _existing_foreign_keys(table: str) -> set[str]:
    return {fk["name"] for fk in _inspector().get_foreign_keys(table)}


def _existing_unique_constraints(table: str) -> set[str]:
    return {constraint["name"] for constraint in _inspector().get_unique_constraints(table)}


def upgrade() -> None:
    op.execute("UPDATE users SET email = LOWER(email) WHERE email IS NOT NULL")
    op.execute("UPDATE bookings SET email = LOWER(email) WHERE email IS NOT NULL")

    booking_columns = _existing_columns("bookings")
    booking_foreign_keys = _existing_foreign_keys("bookings")
    with op.batch_alter_table("bookings") as batch_op:
        if "user_id" not in booking_columns:
            batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        if "fk_bookings_user_id" not in booking_foreign_keys:
            batch_op.create_foreign_key(
                "fk_bookings_user_id",
                "users",
                ["user_id"],
                ["id"],
            )

    if "ix_bookings_user_id" not in _existing_indexes("bookings"):
        op.create_index("ix_bookings_user_id", "bookings", ["user_id"])

    op.execute(
        """
        UPDATE bookings
        SET user_id = users.id
        FROM users
        WHERE LOWER(bookings.email) = LOWER(users.email)
          AND bookings.user_id IS NULL
        """
    )

    if "uq_room_inventory_room_date" not in _existing_unique_constraints("room_inventory"):
        with op.batch_alter_table("room_inventory") as batch_op:
            batch_op.create_unique_constraint(
                "uq_room_inventory_room_date",
                ["room_id", "inventory_date"],
            )


def downgrade() -> None:
    if "uq_room_inventory_room_date" in _existing_unique_constraints("room_inventory"):
        with op.batch_alter_table("room_inventory") as batch_op:
            batch_op.drop_constraint("uq_room_inventory_room_date", type_="unique")

    if "ix_bookings_user_id" in _existing_indexes("bookings"):
        op.drop_index("ix_bookings_user_id", table_name="bookings")

    booking_columns = _existing_columns("bookings")
    booking_foreign_keys = _existing_foreign_keys("bookings")
    with op.batch_alter_table("bookings") as batch_op:
        if "fk_bookings_user_id" in booking_foreign_keys:
            batch_op.drop_constraint("fk_bookings_user_id", type_="foreignkey")
        if "user_id" in booking_columns:
            batch_op.drop_column("user_id")
