"""Add booking user linkage, normalize emails, and enforce room inventory uniqueness.

Revision ID: 0011
Revises: 0010
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE users SET email = LOWER(email) WHERE email IS NOT NULL")
    op.execute("UPDATE bookings SET email = LOWER(email) WHERE email IS NOT NULL")

    op.add_column("bookings", sa.Column("user_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_bookings_user_id", "bookings", "users", ["user_id"], ["id"])
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

    op.create_unique_constraint(
        "uq_room_inventory_room_date",
        "room_inventory",
        ["room_id", "inventory_date"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_room_inventory_room_date", "room_inventory", type_="unique")
    op.drop_index("ix_bookings_user_id", table_name="bookings")
    op.drop_constraint("fk_bookings_user_id", "bookings", type_="foreignkey")
    op.drop_column("bookings", "user_id")
