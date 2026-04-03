from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, Enum
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


class Room(Base):
    __tablename__ = "rooms"

    id = Column(Integer, primary_key=True, index=True)
    hotel_name = Column(String(200), nullable=False)
    room_type = Column(
        Enum(
            RoomType,
            name="room_type",
            values_callable=enum_values,
        ),
        nullable=False,
    )
    description = Column(Text)
    price = Column(Float, nullable=False)
    original_price = Column(Float)
    availability = Column(Boolean, default=True)
    rating = Column(Float, default=4.5)
    review_count = Column(Integer, default=0)
    image_url = Column(String(500))
    gallery_urls = Column(Text)  # JSON array of URLs
    amenities = Column(Text)     # JSON array of amenities
    location = Column(String(200))
    city = Column(String(100))
    country = Column(String(100))
    max_guests = Column(Integer, default=2)
    beds = Column(Integer, default=1)
    bathrooms = Column(Integer, default=1)
    size_sqft = Column(Integer)
    floor = Column(Integer)
    is_featured = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    bookings = relationship("Booking", back_populates="room")


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, index=True)
    booking_ref = Column(String(20), unique=True, index=True)
    user_name = Column(String(100), nullable=False)
    email = Column(String(200), nullable=False)
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
    special_requests = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    room = relationship("Room", back_populates="bookings")
    transactions = relationship("Transaction", back_populates="booking")


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    transaction_ref = Column(String(100), unique=True, index=True)
    stripe_payment_intent_id = Column(String(200), index=True)
    idempotency_key = Column(String(100), unique=True, index=True)
    provider_client_secret = Column(Text)
    amount = Column(Float, nullable=False)
    currency = Column(String(10), default="USD")
    payment_method = Column(String(50))  # card, mock
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
