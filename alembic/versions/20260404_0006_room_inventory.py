"""add room inventory"""

from alembic import op
import sqlalchemy as sa


revision = "20260404_0006"
down_revision = "20260404_0005"
branch_labels = None
depends_on = None

inventory_status_enum = sa.Enum(
    "available",
    "locked",
    "blocked",
    name="inventory_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    inventory_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "room_inventory",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("inventory_date", sa.Date(), nullable=False),
        sa.Column("total_units", sa.Integer(), nullable=False),
        sa.Column("available_units", sa.Integer(), nullable=False),
        sa.Column("locked_units", sa.Integer(), nullable=False),
        sa.Column("status", inventory_status_enum, nullable=True),
        sa.Column("locked_by_booking_id", sa.Integer(), nullable=True),
        sa.Column("lock_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["locked_by_booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
    )
    op.create_index(op.f("ix_room_inventory_id"), "room_inventory", ["id"], unique=False)
    op.create_index(op.f("ix_room_inventory_inventory_date"), "room_inventory", ["inventory_date"], unique=False)
    op.create_index(op.f("ix_room_inventory_room_id"), "room_inventory", ["room_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_room_inventory_room_id"), table_name="room_inventory")
    op.drop_index(op.f("ix_room_inventory_inventory_date"), table_name="room_inventory")
    op.drop_index(op.f("ix_room_inventory_id"), table_name="room_inventory")
    op.drop_table("room_inventory")
    bind = op.get_bind()
    inventory_status_enum.drop(bind, checkfirst=True)
