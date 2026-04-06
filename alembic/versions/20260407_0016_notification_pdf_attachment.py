"""add PDF attachment columns to notification_outbox

Revision ID: 20260407_0016
Revises: 20260406_0015
Create Date: 2026-04-07 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = "20260407_0016"
down_revision = "20260406_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "notification_outbox",
        sa.Column("attachment_pdf", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "notification_outbox",
        sa.Column("attachment_filename", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_outbox", "attachment_filename")
    op.drop_column("notification_outbox", "attachment_pdf")
