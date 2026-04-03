from pydantic import BaseModel, EmailStr, validator
from typing import Optional, List
from datetime import datetime
from enum import Enum


# ─── Enums ────────────────────────────────────────────────────────────────────

class RoomType(str, Enum):
    STANDARD = "standard"
    DELUXE = "deluxe"
    SUITE = "suite"
    PENTHOUSE = "penthouse"


class BookingStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"


class TransactionStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"


# ─── Room Schemas ─────────────────────────────────────────────────────────────

class RoomBase(BaseModel):
    hotel_name: str
    room_type: RoomType
    description: Optional[str] = None
    price: float
    original_price: Optional[float] = None
    availability: bool = True
    rating: float = 4.5
    review_count: int = 0
    image_url: Optional[str] = None
    gallery_urls: Optional[str] = None
    amenities: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    max_guests: int = 2
    beds: int = 1
    bathrooms: int = 1
    size_sqft: Optional[int] = None
    floor: Optional[int] = None
    is_featured: bool = False


class RoomCreate(RoomBase):
    pass


class RoomResponse(RoomBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class RoomListResponse(BaseModel):
    rooms: List[RoomResponse]
    total: int
    page: int
    per_page: int


# ─── Booking Schemas ──────────────────────────────────────────────────────────

class BookingCreate(BaseModel):
    user_name: str
    email: EmailStr
    phone: Optional[str] = None
    room_id: int
    check_in: datetime
    check_out: datetime
    guests: int = 1
    special_requests: Optional[str] = None


class BookingResponse(BaseModel):
    id: int
    booking_ref: str
    user_name: str
    email: str
    phone: Optional[str]
    room_id: int
    room: Optional[RoomResponse] = None
    check_in: datetime
    check_out: datetime
    guests: int
    nights: int
    room_rate: float
    taxes: float
    service_fee: float
    total_amount: float
    status: BookingStatus
    payment_status: PaymentStatus
    special_requests: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True


class BookingListResponse(BaseModel):
    bookings: List[BookingResponse]
    total: int


# ─── Payment Schemas ──────────────────────────────────────────────────────────

class CreatePaymentIntent(BaseModel):
    booking_id: int
    payment_method: str = "card"  # card | mock


class PaymentSuccess(BaseModel):
    booking_id: int
    payment_intent_id: Optional[str] = None
    transaction_ref: str
    payment_method: str
    card_last4: Optional[str] = None
    card_brand: Optional[str] = None


class TransactionResponse(BaseModel):
    id: int
    booking_id: int
    transaction_ref: str
    stripe_payment_intent_id: Optional[str]
    amount: float
    currency: str
    payment_method: str
    card_last4: Optional[str]
    card_brand: Optional[str]
    status: TransactionStatus
    failure_reason: Optional[str]
    created_at: datetime
    booking: Optional[BookingResponse] = None

    class Config:
        from_attributes = True


class TransactionListResponse(BaseModel):
    transactions: List[TransactionResponse]
    total: int


class PaymentStateResponse(BaseModel):
    booking_id: int
    booking_ref: str
    booking_status: BookingStatus
    payment_status: PaymentStatus
    latest_transaction: Optional[TransactionResponse] = None


# ─── Analytics Schemas ────────────────────────────────────────────────────────

class KPIStats(BaseModel):
    total_bookings: int
    total_revenue: float
    success_rate: float
    avg_booking_value: float
    bookings_today: int
    revenue_today: float
    pending_bookings: int
    failed_payments: int


class DailyStats(BaseModel):
    date: str
    bookings: int
    revenue: float


class MonthlyRevenue(BaseModel):
    month: str
    revenue: float
    bookings: int


class PaymentStatusBreakdown(BaseModel):
    status: str
    count: int
    percentage: float


class RoomTypeBreakdown(BaseModel):
    room_type: str
    count: int
    revenue: float


class AnalyticsResponse(BaseModel):
    kpis: KPIStats
    daily_stats: List[DailyStats]
    monthly_revenue: List[MonthlyRevenue]
    payment_breakdown: List[PaymentStatusBreakdown]
    room_type_breakdown: List[RoomTypeBreakdown]
