"""refund lifecycle fields

Revision ID: 20260406_0013
Revises: 20260406_0012
Create Date: 2026-04-06 22:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


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


def _existing_columns(table: str) -> set[str]:
    inspector = Inspector.from_engine(op.get_bind())
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    refund_status.create(op.get_bind(), checkfirst=True)
    columns = _existing_columns("bookings")
    if "refund_status" not in columns:
        op.add_column("bookings", sa.Column("refund_status", refund_status, nullable=True))
    if "refund_amount" not in columns:
        op.add_column("bookings", sa.Column("refund_amount", sa.Float(), nullable=False, server_default="0"))
    if "refund_requested_at" not in columns:
        op.add_column("bookings", sa.Column("refund_requested_at", sa.DateTime(timezone=True), nullable=True))
    if "refund_initiated_at" not in columns:
        op.add_column("bookings", sa.Column("refund_initiated_at", sa.DateTime(timezone=True), nullable=True))
    if "refund_expected_settlement_at" not in columns:
        op.add_column(
            "bookings",
            sa.Column("refund_expected_settlement_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "refund_completed_at" not in columns:
        op.add_column("bookings", sa.Column("refund_completed_at", sa.DateTime(timezone=True), nullable=True))
    if "refund_failed_reason" not in columns:
        op.add_column("bookings", sa.Column("refund_failed_reason", sa.String(length=500), nullable=True))
    if "refund_gateway_reference" not in columns:
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

    with op.batch_alter_table("bookings") as batch_op:
        batch_op.alter_column(
            "refund_amount",
            existing_type=sa.Float(),
            existing_nullable=False,
            server_default=None,
        )


def downgrade() -> None:
    columns = _existing_columns("bookings")
    for column in (
        "refund_gateway_reference",
        "refund_failed_reason",
        "refund_completed_at",
        "refund_expected_settlement_at",
        "refund_initiated_at",
        "refund_requested_at",
        "refund_amount",
        "refund_status",
    ):
        if column in columns:
            op.drop_column("bookings", column)
    refund_status.drop(op.get_bind(), checkfirst=True)
