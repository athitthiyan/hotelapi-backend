"""Create admin_notifications table

Revision ID: 20260509_0021
Revises: 20260508_0020
Create Date: 2026-05-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

revision = "20260509_0021"
down_revision = "20260508_0020"
branch_labels = None
depends_on = None


def _has_table(bind, table_name: str) -> bool:
    inspector = Inspector.from_engine(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "admin_notifications"):
        return  # idempotent — already exists

    op.create_table(
        "admin_notifications",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("type", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.create_index(op.f("ix_admin_notifications_id"), "admin_notifications", ["id"], unique=False)
    op.create_index(op.f("ix_admin_notifications_user_id"), "admin_notifications", ["user_id"], unique=False)
    op.create_index(op.f("ix_admin_notifications_type"), "admin_notifications", ["type"], unique=False)
    op.create_index(op.f("ix_admin_notifications_read"), "admin_notifications", ["read"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_admin_notifications_read"), table_name="admin_notifications")
    op.drop_index(op.f("ix_admin_notifications_type"), table_name="admin_notifications")
    op.drop_index(op.f("ix_admin_notifications_user_id"), table_name="admin_notifications")
    op.drop_index(op.f("ix_admin_notifications_id"), table_name="admin_notifications")
    op.drop_table("admin_notifications")
