"""add apple_id and microsoft_id columns to users table

Revision ID: 20260509_0024
Revises: 20260509_0023
Create Date: 2026-05-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "20260509_0024"
down_revision = "20260509_0023"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {col["name"] for col in inspector.get_columns(table)}


def _existing_indexes(table: str) -> set:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {idx["name"] for idx in inspector.get_indexes(table)}


def upgrade() -> None:
    cols = _existing_columns("users")
    indexes = _existing_indexes("users")

    if "apple_id" not in cols:
        op.add_column("users", sa.Column("apple_id", sa.String(128), nullable=True))
    if "ix_users_apple_id" not in indexes:
        op.create_index("ix_users_apple_id", "users", ["apple_id"], unique=True)

    if "microsoft_id" not in cols:
        op.add_column("users", sa.Column("microsoft_id", sa.String(128), nullable=True))
    if "ix_users_microsoft_id" not in indexes:
        op.create_index("ix_users_microsoft_id", "users", ["microsoft_id"], unique=True)


def downgrade() -> None:
    cols = _existing_columns("users")
    indexes = _existing_indexes("users")

    if "ix_users_microsoft_id" in indexes:
        op.drop_index("ix_users_microsoft_id", table_name="users")
    if "microsoft_id" in cols:
        op.drop_column("users", "microsoft_id")

    if "ix_users_apple_id" in indexes:
        op.drop_index("ix_users_apple_id", table_name="users")
    if "apple_id" in cols:
        op.drop_column("users", "apple_id")
