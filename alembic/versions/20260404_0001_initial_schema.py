"""initial schema"""

from alembic import op
import sqlalchemy as sa


revision = "20260404_0001"
down_revision = None
branch_labels = None
depends_on = None

room_type_enum = sa.Enum(
    "standard",
    "deluxe",
    "suite",
    "penthouse",
    name="room_type",
)
booking_status_enum = sa.Enum(
    "pending",
    "confirmed",
    "cancelled",
    "completed",
    name="booking_status",
)
payment_status_enum = sa.Enum(
    "pending",
    "paid",
    "failed",
    "refunded",
    name="payment_status",
)
transaction_status_enum = sa.Enum(
    "pending",
    "success",
    "failed",
    "refunded",
    name="transaction_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    room_type_enum.create(bind, checkfirst=True)
    booking_status_enum.create(bind, checkfirst=True)
    payment_status_enum.create(bind, checkfirst=True)
    transaction_status_enum.create(bind, checkfirst=True)

    op.create_table(
        "rooms",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("hotel_name", sa.String(length=200), nullable=False),
        sa.Column("room_type", room_type_enum, nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("original_price", sa.Float(), nullable=True),
        sa.Column("availability", sa.Boolean(), nullable=True),
        sa.Column("rating", sa.Float(), nullable=True),
        sa.Column("review_count", sa.Integer(), nullable=True),
        sa.Column("image_url", sa.String(length=500), nullable=True),
        sa.Column("gallery_urls", sa.Text(), nullable=True),
        sa.Column("amenities", sa.Text(), nullable=True),
        sa.Column("location", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("country", sa.String(length=100), nullable=True),
        sa.Column("max_guests", sa.Integer(), nullable=True),
        sa.Column("beds", sa.Integer(), nullable=True),
        sa.Column("bathrooms", sa.Integer(), nullable=True),
        sa.Column("size_sqft", sa.Integer(), nullable=True),
        sa.Column("floor", sa.Integer(), nullable=True),
        sa.Column("is_featured", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(op.f("ix_rooms_id"), "rooms", ["id"], unique=False)

    op.create_table(
        "bookings",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("booking_ref", sa.String(length=20), nullable=True),
        sa.Column("user_name", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=200), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("room_id", sa.Integer(), nullable=False),
        sa.Column("check_in", sa.DateTime(timezone=True), nullable=False),
        sa.Column("check_out", sa.DateTime(timezone=True), nullable=False),
        sa.Column("guests", sa.Integer(), nullable=True),
        sa.Column("nights", sa.Integer(), nullable=False),
        sa.Column("room_rate", sa.Float(), nullable=False),
        sa.Column("taxes", sa.Float(), nullable=True),
        sa.Column("service_fee", sa.Float(), nullable=True),
        sa.Column("total_amount", sa.Float(), nullable=False),
        sa.Column("status", booking_status_enum, nullable=True),
        sa.Column("payment_status", payment_status_enum, nullable=True),
        sa.Column("special_requests", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["room_id"], ["rooms.id"]),
        sa.UniqueConstraint("booking_ref"),
    )
    op.create_index(op.f("ix_bookings_booking_ref"), "bookings", ["booking_ref"], unique=True)
    op.create_index(op.f("ix_bookings_id"), "bookings", ["id"], unique=False)

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
        sa.Column("booking_id", sa.Integer(), nullable=False),
        sa.Column("transaction_ref", sa.String(length=100), nullable=True),
        sa.Column("stripe_payment_intent_id", sa.String(length=200), nullable=True),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("payment_method", sa.String(length=50), nullable=True),
        sa.Column("card_last4", sa.String(length=4), nullable=True),
        sa.Column("card_brand", sa.String(length=20), nullable=True),
        sa.Column("status", transaction_status_enum, nullable=True),
        sa.Column("failure_reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        sa.UniqueConstraint("transaction_ref"),
    )
    op.create_index(op.f("ix_transactions_id"), "transactions", ["id"], unique=False)
    op.create_index(
        op.f("ix_transactions_stripe_payment_intent_id"),
        "transactions",
        ["stripe_payment_intent_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_transactions_transaction_ref"),
        "transactions",
        ["transaction_ref"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_transactions_transaction_ref"), table_name="transactions")
    op.drop_index(op.f("ix_transactions_stripe_payment_intent_id"), table_name="transactions")
    op.drop_index(op.f("ix_transactions_id"), table_name="transactions")
    op.drop_table("transactions")

    op.drop_index(op.f("ix_bookings_id"), table_name="bookings")
    op.drop_index(op.f("ix_bookings_booking_ref"), table_name="bookings")
    op.drop_table("bookings")

    op.drop_index(op.f("ix_rooms_id"), table_name="rooms")
    op.drop_table("rooms")

    bind = op.get_bind()
    transaction_status_enum.drop(bind, checkfirst=True)
    payment_status_enum.drop(bind, checkfirst=True)
    booking_status_enum.drop(bind, checkfirst=True)
    room_type_enum.drop(bind, checkfirst=True)
