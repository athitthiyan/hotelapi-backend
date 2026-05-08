"""add geolocation fields to rooms table

Revision ID: 20260508_0020
Revises: 20260410_0019
Create Date: 2026-05-08
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "20260508_0020"
down_revision = "20260410_0019"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    cols = _existing_columns("rooms")
    if "latitude" not in cols:
        op.add_column("rooms", sa.Column("latitude", sa.Float(), nullable=True))
    if "longitude" not in cols:
        op.add_column("rooms", sa.Column("longitude", sa.Float(), nullable=True))
    if "map_embed_url" not in cols:
        op.add_column("rooms", sa.Column("map_embed_url", sa.Text(), nullable=True))


def downgrade() -> None:
    cols = _existing_columns("rooms")
    if "map_embed_url" in cols:
        op.drop_column("rooms", "map_embed_url")
    if "longitude" in cols:
        op.drop_column("rooms", "longitude")
    if "latitude" in cols:
        op.drop_column("rooms", "latitude")
