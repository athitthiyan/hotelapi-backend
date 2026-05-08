"""add booking guest breakdown columns

Revision ID: 20260509_0022
Revises: 20260509_0021
Create Date: 2026-05-09
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "20260509_0022"
down_revision = "20260509_0021"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    inspector = Inspector.from_engine(op.get_bind())
    return {col["name"] for col in inspector.get_columns(table)}


def upgrade() -> None:
    existing = _existing_columns("bookings")
    columns = (
        ("adults", 1),
        ("children", 0),
        ("infants", 0),
    )

    for name, default in columns:
        if name not in existing:
            op.add_column(
                "bookings",
                sa.Column(
                    name,
                    sa.Integer(),
                    nullable=True,
                    server_default=sa.text(str(default)),
                ),
            )


def downgrade() -> None:
    existing = _existing_columns("bookings")
    for name in ("infants", "children", "adults"):
        if name in existing:
            op.drop_column("bookings", name)
