"""payment state machine"""

from alembic import op
import sqlalchemy as sa


revision = "20260404_0003"
down_revision = "20260404_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("idempotency_key", sa.String(length=100), nullable=True))
    op.add_column("transactions", sa.Column("provider_client_secret", sa.Text(), nullable=True))
    op.add_column("transactions", sa.Column("retry_of_transaction_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_transactions_idempotency_key"), "transactions", ["idempotency_key"], unique=True)
    op.create_foreign_key(
        "fk_transactions_retry_of_transaction_id_transactions",
        "transactions",
        "transactions",
        ["retry_of_transaction_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_transactions_retry_of_transaction_id_transactions", "transactions", type_="foreignkey")
    op.drop_index(op.f("ix_transactions_idempotency_key"), table_name="transactions")
    op.drop_column("transactions", "retry_of_transaction_id")
    op.drop_column("transactions", "provider_client_secret")
    op.drop_column("transactions", "idempotency_key")
