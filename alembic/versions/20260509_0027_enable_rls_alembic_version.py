"""enable RLS on alembic_version (no client access needed)

Revision ID: 20260509_0027
Revises: 20260509_0026
Create Date: 2026-05-09
"""

from alembic import op

revision = "20260509_0027"
down_revision = "20260509_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # alembic_version is only ever touched by migrations running as postgres/
    # service_role — it must never be accessible via PostgREST / client APIs.
    # Enabling RLS with no policies gives default-deny for anon/authenticated
    # while privileged connections (which bypass RLS) continue to work normally.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE public.alembic_version DISABLE ROW LEVEL SECURITY;")
