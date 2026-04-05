"""Add partner schema: is_partner on users, partner_hotels table, partner_hotel_id on rooms, partner_payouts table

Revision ID: 0010
Revises: 0009
Create Date: 2026-04-05
"""

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Add is_partner to users ─────────────────────────────────────────
    op.add_column(
        "users",
        sa.Column(
            "is_partner",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # ── 2. Create partner_hotels ───────────────────────────────────────────
    op.create_table(
        "partner_hotels",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "owner_user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("legal_name", sa.String(200), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("gst_number", sa.String(30), nullable=True),
        sa.Column("support_email", sa.String(200), nullable=False),
        sa.Column("support_phone", sa.String(30), nullable=True),
        sa.Column("address_line", sa.String(255), nullable=False),
        sa.Column("city", sa.String(100), nullable=False),
        sa.Column("state", sa.String(100), nullable=True),
        sa.Column("country", sa.String(100), nullable=False, server_default="India"),
        sa.Column("postal_code", sa.String(20), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("check_in_time", sa.String(20), nullable=True, server_default="14:00"),
        sa.Column("check_out_time", sa.String(20), nullable=True, server_default="11:00"),
        sa.Column(
            "cancellation_window_hours",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("24"),
        ),
        sa.Column(
            "instant_confirmation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "free_cancellation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "verified_badge",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("bank_account_name", sa.String(150), nullable=True),
        sa.Column("bank_account_number_masked", sa.String(32), nullable=True),
        sa.Column("bank_ifsc", sa.String(20), nullable=True),
        sa.Column("bank_upi_id", sa.String(120), nullable=True),
        sa.Column("payout_cycle", sa.String(30), nullable=False, server_default="weekly"),
        sa.Column("payout_currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_partner_hotels_id", "partner_hotels", ["id"])
    op.create_index(
        "ix_partner_hotels_owner_user_id", "partner_hotels", ["owner_user_id"], unique=True
    )

    # ── 3. Add partner_hotel_id FK to rooms ────────────────────────────────
    op.add_column(
        "rooms",
        sa.Column(
            "partner_hotel_id",
            sa.Integer(),
            sa.ForeignKey("partner_hotels.id"),
            nullable=True,
        ),
    )
    op.create_index("ix_rooms_partner_hotel_id", "rooms", ["partner_hotel_id"])

    # ── 4. Create partner_payouts ──────────────────────────────────────────
    op.create_table(
        "partner_payouts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "hotel_id",
            sa.Integer(),
            sa.ForeignKey("partner_hotels.id"),
            nullable=False,
        ),
        sa.Column(
            "booking_id",
            sa.Integer(),
            sa.ForeignKey("bookings.id"),
            nullable=True,
        ),
        sa.Column("gross_amount", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("commission_amount", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("net_amount", sa.Float(), nullable=False, server_default=sa.text("0.0")),
        sa.Column("currency", sa.String(10), nullable=False, server_default="INR"),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("payout_reference", sa.String(100), nullable=True, unique=True),
        sa.Column("payout_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_partner_payouts_id", "partner_payouts", ["id"])
    op.create_index("ix_partner_payouts_hotel_id", "partner_payouts", ["hotel_id"])
    op.create_index("ix_partner_payouts_booking_id", "partner_payouts", ["booking_id"])
    op.create_index("ix_partner_payouts_status", "partner_payouts", ["status"])
    op.create_index(
        "ix_partner_payouts_payout_reference",
        "partner_payouts",
        ["payout_reference"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_partner_payouts_payout_reference", table_name="partner_payouts")
    op.drop_index("ix_partner_payouts_status", table_name="partner_payouts")
    op.drop_index("ix_partner_payouts_booking_id", table_name="partner_payouts")
    op.drop_index("ix_partner_payouts_hotel_id", table_name="partner_payouts")
    op.drop_index("ix_partner_payouts_id", table_name="partner_payouts")
    op.drop_table("partner_payouts")

    op.drop_index("ix_rooms_partner_hotel_id", table_name="rooms")
    op.drop_column("rooms", "partner_hotel_id")

    op.drop_index("ix_partner_hotels_owner_user_id", table_name="partner_hotels")
    op.drop_index("ix_partner_hotels_id", table_name="partner_hotels")
    op.drop_table("partner_hotels")

    op.drop_column("users", "is_partner")
