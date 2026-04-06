from sqlalchemy import (
    Column,
    Integer,
    String,
    Float,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    Enum,
    Date,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database import Base
import enum


def enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


class RoomType(str, enum.Enum):
    STANDARD = "standard"
    DELUXE = "deluxe"
    SUITE = "suite"
    PENTHOUSE = "penthouse"


class BookingStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class TransactionStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class RefundStatus(str, enum.Enum):
    REFUND_REQUESTED = "refund_requested"
    REFUND_INITIATED = "refund_initiated"
    REFUND_PROCESSING = "refund_processing"
    REFUND_SUCCESS = "refund_success"
    REFUND_FAILED = "refund_failed"
    REFUND_REVERSED = "refund_reversed"


class PayoutStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SETTLED = "settled"
    FAILED = "failed"
    REVERSED = "reversed"
    LEGACY_PAID = "paid"


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class InventoryStatus(str, enum.Enum):
    AVAILABLE = "available"
    LOCKED = "locked"
    BLOCKED = "blocked"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(200), unique=True, index=True, nullable=False)
    full_name = Column(String(100), nullable=False)
    hashed_password = Column(String(255), nullable=True)  # nullable for social-only accounts
    phone = Column(String(30))
    phone_verified = Column(Boolean, default=False, nullable=False)
    pending_phone = Column(String(30))
    phone_otp_hash = Column(String(64))
    phone_otp_expires_at = Column(DateTime(timezone=True))
    phone_otp_attempts = Column(Integer, default=0, nullable=False)
    avatar_url = Column(String(500))
    google_id = Column(String(128), unique=True, index=True)
    apple_id = Column(String(128), unique=True, index=True)
    microsoft_id = Column(String(128), unique=True, index=True)
    is_email_verified = Column(Boolean, default=False, nullable=False)
    email_verification_token = Column(String(128))
    email_verification_expires_at = Column(DateTime(timezone=True))
    is_admin = Column(Boolean, default=False, nullable=False)
    is_partner = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    reviews = relationship("Review", back_populates="user")
    wishlists = relationship("Wishlist", back_populates="user")
    partner_hotels = relationship("PartnerHotel", back_populates="owner")
    bookings = relationship("Booking", back_populates="user")
    password_reset_tokens = relationship("PasswordResetToken", back_populates="user")


class PartnerHotel(Base):
    __tablename__ = "partner_hotels"

    id = Column(Integer, primary_key=True, index=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    legal_name = Column(String(200), nullable=False)
    display_name = Column(String(200), nullable=False)
    gst_number = Column(String(30))
    support_email = Column(String(200), nullable=False)
    support_phone = Column(String(30))
    address_line = Column(String(255), nullable=False)
    city = Column(String(100), nullable=False)
    state = Column(String(100))
    country = Column(String(100), default="India", nullable=False)
    postal_code = Column(String(20))
    description = Column(Text)
    check_in_time = Column(String(20), default="14:00")
    check_out_time = Column(String(20), default="11:00")
    cancellation_window_hours = Column(Integer, default=24, nullable=False)
    instant_confirmation_enabled = Column(Boolean, default=True, nullable=False)
    free_cancellation_enabled = Column(Boolean, default=True, nullable=False)
    verified_badge = Column(Boolean, default=False, nullable=False)
    bank_account_name = Column(String(150))
    bank_account_number_masked = Column(String(32))
    bank_ifsc = Column(String(20))
    bank_upi_id = Column(String(120))
    payout_cycle = Column(String(30), default="weekly", nullable=False)
    payout_currency = Column(String(10), default="INR", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    owner = relationship("User", back_populates="partner_hotels")
    rooms = relationship("Room", back_populates="partner_hotel")
    payouts = relationship("PartnerPayout", back_populates="hotel")


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    partner_hotel_id = Column(Integer, ForeignKey("partner_hotels.id"), index=True)
    hotel_name = Column(String(200), nullable=False)
    room_type = Column(
        Enum(
            RoomType,
            name="room_type",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    room_type_name = Column(String(120), nullable=False, default="Standard")
    description = Column(Text)
    price = Column(Float, nullable=False)
    original_price = Column(Float)
    total_room_count = Column(Integer, default=1, nullable=False)
    weekend_price = Column(Float)
    holiday_price = Column(Float)
    extra_guest_charge = Column(Float, default=0.0, nullable=False)
    availability = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True, nullable=False)
    rating = Column(Float, default=4.5)
    review_count = Column(Integer, default=0)
    image_url = Column(String(500))
    gallery_urls = Column(Text)  # JSON array of URLs
    amenities = Column(Text)     # JSON array of amenities
    location = Column(String(200))
    city = Column(String(100))
    country = Column(String(100))
    latitude = Column(Float)
    longitude = Column(Float)
    map_embed_url = Column(Text)
    max_guests = Column(Integer, default=2)
    beds = Column(Integer, default=1)
    bathrooms = Column(Integer, default=1)
    size_sqft = Column(Integer)
    floor = Column(Integer)
    is_featured = Column(Boolean, default=False)
    deleted_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    partner_hotel = relationship("PartnerHotel", back_populates="rooms")
    bookings = relationship("Booking", back_populates="room")
    reviews = relationship("Review", back_populates="room")
    wishlists = relationship("Wishlist", back_populates="room")


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    booking_ref = Column(String(20), unique=True, index=True)
    user_name = Column(String(100), nullable=False)
    email = Column(String(200), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    phone = Column(String(20))
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False)
    check_in = Column(DateTime(timezone=True), nullable=False)
    check_out = Column(DateTime(timezone=True), nullable=False)
    hold_expires_at = Column(DateTime(timezone=True))
    guests = Column(Integer, default=1)
    nights = Column(Integer, nullable=False)
    room_rate = Column(Float, nullable=False)
    taxes = Column(Float, default=0.0)
    service_fee = Column(Float, default=0.0)
    total_amount = Column(Float, nullable=False)
    status = Column(
        Enum(
            BookingStatus,
            name="booking_status",
            values_callable=enum_values,
        ),
        default=BookingStatus.PENDING,
    )
    payment_status = Column(
        Enum(
            PaymentStatus,
            name="payment_status",
            values_callable=enum_values,
        ),
        default=PaymentStatus.PENDING,
    )
    refund_status = Column(
        Enum(
            RefundStatus,
            name="refund_status",
            values_callable=enum_values,
        ),
        nullable=True,
    )
    refund_amount = Column(Float, default=0.0, nullable=False)
    refund_requested_at = Column(DateTime(timezone=True))
    refund_initiated_at = Column(DateTime(timezone=True))
    refund_expected_settlement_at = Column(DateTime(timezone=True))
    refund_completed_at = Column(DateTime(timezone=True))
    refund_failed_reason = Column(String(500))
    refund_gateway_reference = Column(String(120))
    special_requests = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    room = relationship("Room", back_populates="bookings")
    user = relationship("User", back_populates="bookings")
    transactions = relationship("Transaction", back_populates="booking")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    transaction_ref = Column(String(100), unique=True, index=True)
    stripe_payment_intent_id = Column(String(200), index=True)
    razorpay_order_id = Column(String(100), index=True)
    razorpay_payment_id = Column(String(100))
    razorpay_signature = Column(String(256))
    gateway = Column(String(30), default="stripe")  # stripe, razorpay
    idempotency_key = Column(String(100), unique=True, index=True)
    provider_client_secret = Column(Text)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="INR")
    payment_method = Column(String(50))  # card, mock, upi, razorpay, gpay, phonepay
    card_last4 = Column(String(4))
    card_brand = Column(String(20))
    retry_of_transaction_id = Column(Integer, ForeignKey("transactions.id"))
    status = Column(
        Enum(
            TransactionStatus,
            name="transaction_status",
            values_callable=enum_values,
        ),
        default=TransactionStatus.PENDING,
    )
    failure_reason = Column(String(500))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    booking = relationship("Booking", back_populates="transactions")
    retry_of_transaction = relationship("Transaction", remote_side=[id])


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"))
    transaction_id = Column(Integer, ForeignKey("transactions.id"))
    event_type = Column(String(100), nullable=False, index=True)
    recipient_email = Column(String(200), nullable=False, index=True)
    subject = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    status = Column(
        Enum(
            NotificationStatus,
            name="notification_status",
            values_callable=enum_values,
        ),
        default=NotificationStatus.PENDING,
    )
    failure_reason = Column(String(500))
    sent_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class RoomInventory(Base):
    __tablename__ = "room_inventory"
    __table_args__ = (
        UniqueConstraint("room_id", "inventory_date", name="uq_room_inventory_room_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    inventory_date = Column(Date, nullable=False, index=True)
    total_units = Column(Integer, default=1, nullable=False)
    available_units = Column(Integer, default=1, nullable=False)
    locked_units = Column(Integer, default=0, nullable=False)
    booked_units = Column(Integer, default=0, nullable=False)
    blocked_units = Column(Integer, default=0, nullable=False)
    status = Column(
        Enum(
            InventoryStatus,
            name="inventory_status",
            values_callable=enum_values,
        ),
        default=InventoryStatus.AVAILABLE,
    )
    block_reason = Column(String(120))
    price_override = Column(Float)
    price_override_label = Column(String(120))
    locked_by_booking_id = Column(Integer, ForeignKey("bookings.id"))
    lock_expires_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"))
    action = Column(String(100), nullable=False, index=True)
    entity_type = Column(String(50), nullable=False, index=True)
    entity_id = Column(String(100), nullable=False, index=True)
    metadata_json = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    actor = relationship("User")


class PartnerPayout(Base):
    __tablename__ = "partner_payouts"

    id = Column(Integer, primary_key=True, index=True)
    hotel_id = Column(Integer, ForeignKey("partner_hotels.id"), nullable=False, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), index=True)
    gross_amount = Column(Float, nullable=False, default=0.0)
    commission_amount = Column(Float, nullable=False, default=0.0)
    net_amount = Column(Float, nullable=False, default=0.0)
    currency = Column(String(10), default="INR", nullable=False)
    status = Column(
        Enum(
            PayoutStatus,
            name="payout_status",
            values_callable=enum_values,
        ),
        default=PayoutStatus.PENDING,
        nullable=False,
        index=True,
    )
    payout_reference = Column(String(100), unique=True, index=True)
    payout_date = Column(DateTime(timezone=True))
    statement_generated_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    hotel = relationship("PartnerHotel", back_populates="payouts")
    booking = relationship("Booking")


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("booking_id", name="uq_reviews_booking_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    rating = Column(Integer, nullable=False)            # overall 1-5
    cleanliness_rating = Column(Integer)                # 1-5 sub-ratings
    service_rating = Column(Integer)
    value_rating = Column(Integer)
    location_rating = Column(Integer)
    title = Column(String(200))
    body = Column(Text)
    is_verified = Column(Boolean, default=False, nullable=False)
    host_reply = Column(Text)
    host_replied_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    user = relationship("User", back_populates="reviews")
    room = relationship("Room", back_populates="reviews")
    booking = relationship("Booking")


class Wishlist(Base):
    __tablename__ = "wishlists"
    __table_args__ = (
        UniqueConstraint("user_id", "room_id", name="uq_wishlists_user_room"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    room_id = Column(Integer, ForeignKey("rooms.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="wishlists")
    room = relationship("Room", back_populates="wishlists")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String(256), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User", back_populates="password_reset_tokens")
