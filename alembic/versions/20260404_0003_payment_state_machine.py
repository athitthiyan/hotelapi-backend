"""payment state machine"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector


revision = "20260404_0003"
down_revision = "20260404_0002"
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


def _existing_foreign_keys(table: str) -> set:
    bind = op.get_bind()
    inspector = Inspector.from_engine(bind)
    return {fk["name"] for fk in inspector.get_foreign_keys(table)}


def upgrade() -> None:
    cols = _existing_columns("transactions")

    if "idempotency_key" not in cols:
        op.add_column("transactions", sa.Column("idempotency_key", sa.String(length=100), nullable=True))

    if "provider_client_secret" not in cols:
        op.add_column("transactions", sa.Column("provider_client_secret", sa.Text(), nullable=True))

    if "retry_of_transaction_id" not in cols:
        op.add_column("transactions",