"""partner payout maturity fields

Revision ID: 20260406_0014
Revises: 20260406_0013
Create Date: 2026-04-06 23:30:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260406_0014"
down_revision = "20260406_0013"
branch_labels = None
depends_on = None


payout_status = sa.Enum(
    "pending",
    "processing",
    "settled",
    "failed",
    "reversed",
    name="payout_status",
)


def upgrade() -> None:
    payout_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "partner_payouts",
        sa.Column("statement_generated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        """
        UPDATE partner_payouts
        SET status = CASE status
            WHEN 'paid' THEN 'settled'
            ELSE COALESCE(status, 'pending')
        END
        """
    )
    op.execute(
        """
        ALTER TABLE partner_payouts
        ALTER COLUMN status TYPE payout_status
        USING status::payout_status
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE partner_payouts
        ALTER COLUMN status TYPE VARCHAR(20)
        USING status::text
        """
    )
    op.drop_column("partner_payouts", "statement_generated_at")
    payout_status.drop(op.get_bind(), checkfirst=True)
