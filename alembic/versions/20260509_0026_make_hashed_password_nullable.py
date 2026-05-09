"""make hashed_password nullable for social-only accounts

Revision ID: 20260509_0026
Revises: 20260509_0025
Create Date: 2026-05-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260509_0026"
down_revision = "20260509_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()

    # PostgreSQL supports ALTER COLUMN directly
    if bind.dialect.name == "postgresql":
        op.alter_column("users", "hashed_password", existing_type=sa.String(255), nullable=True)
        return

    # SQLite requires a full table rebuild to change column nullability
    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.alter_column(
            "hashed_password",
            existing_type=sa.String(255),
            nullable=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("UPDATE users SET hashed_password = '' WHERE hashed_password IS NULL")
        op.alter_column("users", "hashed_password", existing_type=sa.String(255), nullable=False)
        return

    with op.batch_alter_table("users", recreate="always") as batch_op:
        batch_op.alter_column(
            "hashed_password",
            existing_type=sa.String(255),
            nullable=False,
        )
