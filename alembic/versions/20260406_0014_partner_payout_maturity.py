"""partner payout maturity fields

Revision ID: 20260406_0014
Revises: 20260406_0013
Create Date: 2026-04-06 23:30:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.dialects.postgresql import ENUM as PgEnum


revision = "20260406_0014"
down_revision = "20260406_0013"
branch_labels = None
depends_on = None


payout_status = PgEnum(
    "pending",
    "processing",
    "settled",
    "failed",
    "reversed",
    name="payout_status",
    create_type=False,
)

_payout_status_sa = sa.Enum(
    "pending",
    "processing",
    "settled",
    "failed",
    "reversed",
    name="payout_status",
)


def _existing_columns(table: str) -> set[str]:
    inspector = Inspector.from_engine(op.get_bind())
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        _payout_status_sa.create(bind, checkfirst=True)
    if "statement_generated_at" not in _existing_columns("partner_payouts"):
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
    if bind.dialect.name != "sqlite":
        op.execute(
            """
            ALTER TABLE partner_payouts
            ALTER COLUMN status DROP DEFAULT
            """
        )
        op.execute(
            """
            ALTER TABLE partner_payouts
            ALTER COLUMN status TYPE payout_status
            USING status::payout_status
            """
        )
        op.execute(
            """
            ALTER TABLE partner_payouts
            ALTER COLUMN status SET DEFAULT 'pending'::payout_status
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.execute(
            """
            ALTER TABLE partner_payouts
            ALTER COLUMN status DROP DEFAULT
            """
        )
        op.execute(
            """
            ALTER TABLE partner_payouts
            ALTER COLUMN status TYPE VARCHAR(20)
            USING status::text
            """
        )
        op.execute(
            """
            ALTER TABLE partner_payouts
            ALTER COLUMN status SET DEFAULT 'pending'
            """
        )
    if "statement_generated_at" in _existing_columns("partner_payouts"):
        op.drop_column("partner_payouts", "statement_generated_at")
    if bind.dialect.name != "sqlite":
        _payout_status_sa.drop(bind, checkfirst=True)
