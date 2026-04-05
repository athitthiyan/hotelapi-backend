"""refund lifecycle fields

Revision ID: 20260406_0013
Revises: 20260406_0012
Create Date: 2026-04-06 22:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260406_0013"
down_revision = "20260406_0012"
branch_labels = None
depends_on = None


refund_status = sa.Enum(
    "refund_requested",
    "refund_initiated",
    "refund_processing",
    "refund_success",
    "refund_failed",
    "refund_reversed",
    name="refund_status",
)


def upgrade() -> None:
    refund_status.create(op.get_bind(), checkfirst=True)
    op.add_column("bookings", sa.Column("refund_status", refund_status, nullable=True))
    op.add_column("bookings", sa.Column("refund_amount", sa.Float(), nullable=False, server_default="0"))
    op.add_column("bookings", sa.Column("refund_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("refund_initiated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("refund_expected_settlement_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("refund_completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bookings", sa.Column("refund_failed_reason", sa.String(length=500), nullable=True))
    op.add_column("bookings", sa.Column("refund_gateway_reference", sa.String(length=120), nullable=True))

    op.execute(
        """
        UPDATE bookings
        SET refund_status = 'refund_success',
            refund_amount = total_amount,
            refund_completed_at = COALESCE(updated_at, created_at)
        WHERE payment_status = 'refunded'
        """
    )

    op.alter_column("bookings", "refund_amount", server_default=None)


def downgrade() -> None:
    op.drop_column("bookings", "refund_gateway_reference")
    op.drop_column("bookings", "refund_failed_reason")
    op.drop_column("bookings", "refund_completed_at")
    op.drop_column("bookings", "refund_expected_settlement_at")
    op.drop_column("bookings", "refund_initiated_at")
    op.drop_column("bookings", "refund_requested_at")
    op.drop_column("bookings", "refund_amount")
    op.drop_column("bookings", "refund_status")
    refund_status.drop(op.get_bind(), checkfirst=True)
