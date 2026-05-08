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
        op.add_column("transactions", sa.Column("retry_of_transaction_id", sa.Integer(), nullable=True))

    idx_name = op.f("ix_transactions_idempotency_key")
    if idx_name not in _existing_indexes("transactions"):
        op.create_index(idx_name, "transactions", ["idempotency_key"], unique=True)

    fk_name = "fk_transactions_retry_of_transaction_id_transactions"
    if fk_name not in _existing_foreign_keys("transactions"):
        op.create_foreign_key(
            fk_name,
            "transactions",
            "transactions",
            ["retry_of_transaction_id"],
            ["id"],
        )


def downgrade() -> None:
    fk_name = "fk_transactions_retry_of_transaction_id_transactions"
    if fk_name in _existing_foreign_keys("transactions"):
        op.drop_constraint(fk_name, "transactions", type_="foreignkey")

    idx_name = op.f("ix_transactions_idempotency_key")
    if idx_name in _existing_indexes("transactions"):
        op.drop_index(idx_name, table_name="transactions")

    cols = _existing_columns("transactions")
    if "retry_of_transaction_id" in cols:
        op.drop_column("transactions", "retry_of_transaction_id")
    if "provider_client_secret" in cols:
        op.drop_column("transactions", "provider_client_secret")
    if "idempotency_key" in cols:
        op.drop_column("transactions", "idempotency_key")
