"""Phase 1 MVP — reviews, wishlists, password_reset_tokens, extend users

Revision ID: 0008
Revises: 0007
Create Date: 2026-04-04
"""

from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "20260404_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Extend users table ──────────────────────────────────────────────────
    op.add_column("users", sa.Column("phone", sa.String(30), nullable=True))
    op.add_column("users", sa.Column("avatar_url", sa.String(500), nullable=True))
    op.add_column("users", sa.Column("google_id", sa.String(128), nullable=True))
    op.alter_column("users", "hashed_password", nullable=True)
    op.create_index("ix_users_google_id", "users", ["google_id"], unique=True)

    # ── reviews ────────────────────────────────────────────────────────────
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("room_id", sa.Integer, sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column("booking_id", sa.Integer, sa.ForeignKey("bookings.id"), nullable=False),
        sa.Column("rating", sa.Integer, nullable=False),
        sa.Column("cleanliness_rating", sa.Integer, nullable=True),
        sa.Column("service_rating", sa.Integer, nullable=True),
        sa.Column("value_rating", sa.Integer, nullable=True),
        sa.Column("location_rating", sa.Integer, nullable=True),
        sa.Column("title", sa.String(200), nullable=True),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("is_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("host_reply", sa.Text, nullable=True),
        sa.Column("host_replied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("booking_id", name="uq_reviews_booking_id"),
    )
    op.create_index("ix_reviews_user_id", "reviews", ["user_id"])
    op.create_index("ix_reviews_room_id", "reviews", ["room_id"])

    # ── wishlists ──────────────────────────────────────────────────────────
    op.create_table(
        "wishlists",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("room_id", sa.Integer, sa.ForeignKey("rooms.id"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "room_id", name="uq_wishlists_user_room"),
    )
    op.create_index("ix_wishlists_user_id", "wishlists", ["user_id"])
    op.create_index("ix_wishlists_room_id", "wishlists", ["room_id"])

    # ── password_reset_tokens ──────────────────────────────────────────────
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])
    op.create_index(
        "ix_password_reset_tokens_token_hash", "password_reset_tokens", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_table("password_reset_tokens")
    op.drop_table("wishlists")
    op.drop_table("reviews")
    op.drop_index("ix_users_google_id", table_name="users")
    op.drop_column("users", "google_id")
    op.drop_column("users", "avatar_url")
    op.drop_column("users", "phone")
    op.alter_column("users", "hashed_password", nullable=False)
