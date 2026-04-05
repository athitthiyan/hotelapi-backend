"""add notification outbox"""

from alembic import op
import sqlalchemy as sa


revision = "20260404_0005"
down_revision = "20260404_0004"
branch_labels = None
depends_on = None

notification_status_enum = sa.Enum(
    "pending",
    "sent",
    "failed",
    name="notification_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    notification_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=True),
        sa.Column("transaction_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("recipient_email", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", notification_status_enum, nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
    )
    op.create_index(op.f("ix_notification_outbox_id"), "notification_outbox", ["id"], unique=False)
    op.create_index(op.f("ix_notification_outbox_event_type"), "notification_outbox", ["event_type"], unique=False)
    op.create_index(
        op.f("ix_notification_outbox_recipient_email"),
        "notification_outbox",
        ["recipient_email"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_notification_outbox_recipient_email"), table_name="notification_outbox")
    op.drop_index(op.f("ix_notification_outbox_event_type"), table_name="notification_outbox")
    op.drop_index(op.f("ix_notification_outbox_id"), table_name="notification_outbox")
    op.drop_table("notification_outbox")
    bind = op.get_bind()
    notification_status_enum.drop(bind, checkfirst=True)
