import re
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from typing import Optional, List
from datetime import date, datetime
from enum import Enum


# ─── Error Response ───────────────────────────────────────────────────────────

class ApiError(BaseModel):
    """Structured error response body returned alongside HTTP error status codes."""
    code: str
    message: str
    field: Optional[str] = None


# ─── Enums ────────────────────────────────────────────────────────────────────

class RoomType(str, Enum):
    STANDARD = "standard"
    DELUXE = "deluxe"
    SUITE = "suite"
    PENTHOUSE = "penthouse"


class BookingStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXPIRED = "expired"


class PaymentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class TransactionStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCESS = "success"
    FAILED = "failed"
    REFUNDED = "refunded"
    EXPIRED = "expired"


class RefundStatus(str, Enum):
    REFUND_REQUESTED = "refund_requested"
    REFUND_INITIATED = "refund_initiated"
    REFUND_PROCESSING = "refund_processing"
    REFUND_SUCCESS = "refund_success"
    REFUND_FAILED = "refund_failed"
    REFUND_REVERSED = "refund_reversed"


class PayoutStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    SETTLED = "settled"
    FAILED = "failed"
    REVERSED = "reversed"


class NotificationStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class InventoryStatus(str, Enum):
    AVAILABLE = "available"
    LOCKED = "locked"
    BLOCKED = "blocked"


# ─── Room Schemas ─────────────────────────────────────────────────────────────

class RoomBase(BaseModel):
    hotel_name: str
    room_type: RoomType
    room_type_name: Optional[str] = None
    description: Optional[str] = None
    price: float
    original_price: Optional[float] = None
    total_room_count: int = 1
    weekend_price: Optional[float] = None
    holiday_price: Optional[float] = None
    extra_guest_charge: float = 0.0
    availability: bool = True
    is_active: bool = True
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
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    map_embed_url: Optional[str] = None


class RoomCreate(RoomBase):
    pass


class RoomUpdate(BaseModel):
    hotel_name: Optional[str] = None
    room_type: Optional[RoomType] = None
    description: Optional[str] = None
    price: Optional[float] = None
    original_price: Optional[float] = None
    availability: Optional[bool] = None
    rating: Optional[float] = None
    review_count: Optional[int] = None
    image_url: Optional[str] = None
    gallery_urls: Optional[str] = None
    amenities: Optional[str] = None
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    max_guests: Optional[int] = None
    beds: Optional[int] = None
    bathrooms: Optional[int] = None
    size_sqft: Optional[int] = None
    floor: Optional[int] = None
    is_featured: Optional[bool] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    map_embed_url: Optional[str] = None


class RoomResponse(RoomBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RoomListResponse(BaseModel):
    rooms: List[RoomResponse]
    total: int
    page: int
    per_page: int


class DestinationResponse(BaseModel):
    city: str
    country: Optional[str] = None
    room_count: int
    featured_count: int
    average_price: float


class DestinationListResponse(BaseModel):
    destinations: List[DestinationResponse]
    total: int


# ─── Booking Schemas ──────────────────────────────────────────────────────────

class BookingCreate(BaseModel):
    user_name: str = Field(min_length=2, max_length=100)
    email: EmailStr
    phone: Optional[str] = None
    room_id: int = Field(gt=0)
    check_in: datetime
    check_out: datetime
    guests: int = Field(default=1, ge=1)
    special_requests: Optional[str] = Field(default=None, max_length=500)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value == "":
            return value
        cleaned = value.strip()
        if not re.fullmatch(r"[0-9+\-\s()]{7,20}", cleaned):
            raise ValueError("Phone number format is invalid")
        return cleaned


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
    hold_expires_at: Optional[datetime] = None
    guests: int
    nights: int
    room_rate: float
    taxes: float
    service_fee: float
    total_amount: float
    status: BookingStatus
    payment_status: PaymentStatus
    lifecycle_state: Optional[str] = None
    refund_status: Optional[RefundStatus] = None
    refund_amount: float = 0.0
    refund_requested_at: Optional[datetime] = None
    refund_initiated_at: Optional[datetime] = None
    refund_expected_settlement_at: Optional[datetime] = None
    refund_completed_at: Optional[datetime] = None
    refund_failed_reason: Optional[str] = None
    refund_gateway_reference: Optional[str] = None
    special_requests: Optional[str]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BookingListResponse(BaseModel):
    bookings: List[BookingResponse]
    total: int


class ActiveHoldResponse(BaseModel):
    booking_id: int
    room_id: int
    hotel_name: str
    room_name: str
    check_in: date
    check_out: date
    guests: int
    expires_at: datetime
    remaining_seconds: int
    lifecycle_state: Optional[str] = None
    booking_status: Optional[str] = None
    payment_status: Optional[str] = None


class UnavailableDatesResponse(BaseModel):
    """Dates for a specific room that cannot be booked.

    * ``unavailable_dates`` — fully confirmed / permanently blocked dates.
    * ``held_dates`` — temporarily locked by an active inventory hold; may
      become free once the hold expires.
    """
    unavailable_dates: List[str]  # ISO date strings, e.g. "2026-05-10"
    held_dates: List[str]


# ─── Payment Schemas ──────────────────────────────────────────────────────────

class CreatePaymentIntent(BaseModel):
    booking_id: int = Field(gt=0)
    payment_method: str = "card"  # card | mock
    idempotency_key: Optional[str] = Field(default=None, min_length=7, max_length=100)

    @field_validator("payment_method")
    @classmethod
    def validate_payment_method(cls, value: str) -> str:
        if value not in {"card", "mock"}:
            raise ValueError("Payment method must be card or mock")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", value):
            raise ValueError("Idempotency key may only contain letters, numbers, hyphens, and underscores")
        return value


class PaymentSuccess(BaseModel):
    booking_id: int = Field(gt=0)
    payment_intent_id: Optional[str] = None
    transaction_ref: str = Field(min_length=5, max_length=100)
    payment_method: str
    card_last4: Optional[str] = Field(default=None, min_length=4, max_length=4)
    card_brand: Optional[str] = Field(default=None, max_length=20)

    @field_validator("payment_method")
    @classmethod
    def validate_success_payment_method(cls, value: str) -> str:
        if value not in {"card", "mock"}:
            raise ValueError("Payment method must be card or mock")
        return value

    @field_validator("card_last4")
    @classmethod
    def validate_card_last4(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not re.fullmatch(r"\d{4}", value):
            raise ValueError("Card last4 must be exactly 4 digits")
        return value


class TransactionResponse(BaseModel):
    id: int
    booking_id: int
    transaction_ref: str
    stripe_payment_intent_id: Optional[str]
    idempotency_key: Optional[str]
    amount: float
    currency: str
    payment_method: str
    card_last4: Optional[str]
    card_brand: Optional[str]
    status: TransactionStatus
    lifecycle_state: Optional[str] = None
    failure_reason: Optional[str]
    created_at: datetime
    booking: Optional[BookingResponse] = None

    model_config = ConfigDict(from_attributes=True)


class TransactionListResponse(BaseModel):
    transactions: List[TransactionResponse]
    total: int


class PaymentStateResponse(BaseModel):
    booking_id: int
    booking_ref: str
    booking_status: BookingStatus
    payment_status: PaymentStatus
    lifecycle_state: Optional[str] = None
    latest_transaction: Optional[TransactionResponse] = None
    failed_payment_count: int = 0
    retry_after_seconds: int = 0
    retry_available_at: Optional[str] = None


class RefundRequest(BaseModel):
    booking_id: int = Field(gt=0)
    reason: str = Field(default="Refund approved by admin", min_length=4, max_length=255)


class RefundTimelineResponse(BaseModel):
    booking_id: int
    booking_ref: str
    refund_status: RefundStatus
    refund_amount: float
    requested_at: Optional[datetime] = None
    initiated_at: Optional[datetime] = None
    expected_settlement_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    failed_reason: Optional[str] = None
    gateway_reference: Optional[str] = None


class RefundAdminActionRequest(BaseModel):
    reason: str = Field(default="Manual refund update", min_length=4, max_length=255)
    amount: Optional[float] = Field(default=None, ge=0)
    gateway_reference: Optional[str] = Field(default=None, min_length=3, max_length=120)


class RefundAdminActionResponse(BaseModel):
    message: str
    timeline: RefundTimelineResponse


class BookingSupportRequest(BaseModel):
    category: str = Field(min_length=4, max_length=50)
    message: str = Field(min_length=8, max_length=500)

    @field_validator("category")
    @classmethod
    def validate_support_category(cls, value: str) -> str:
        allowed = {"payment_help", "cancellation_help", "refund_help", "booking_issue"}
        if value not in allowed:
            raise ValueError("Support category is invalid")
        return value


class BookingDashboardResponse(BaseModel):
    bookings: List[BookingResponse]
    total: int
    pending_count: int
    confirmed_count: int
    failed_payment_count: int


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


class UserSignup(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=10, max_length=128)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        has_upper = any(char.isupper() for char in value)
        has_lower = any(char.islower() for char in value)
        has_digit = any(char.isdigit() for char in value)
        if not (has_upper and has_lower and has_digit):
            raise ValueError(
                "Password must include uppercase, lowercase, and numeric characters"
            )
        return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserResponse(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    phone: Optional[str] = None
    phone_verified: bool = False
    is_email_verified: bool = False
    is_admin: bool
    is_partner: bool
    is_active: bool

    model_config = ConfigDict(from_attributes=True)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: UserResponse


class PartnerRegisterRequest(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=10, max_length=128)
    legal_name: str = Field(min_length=2, max_length=200)
    display_name: str = Field(min_length=2, max_length=200)
    support_email: EmailStr
    support_phone: str = Field(min_length=7, max_length=30)
    address_line: str = Field(min_length=5, max_length=255)
    city: str = Field(min_length=2, max_length=100)
    state: Optional[str] = Field(default=None, max_length=100)
    country: str = Field(default="India", min_length=2, max_length=100)
    postal_code: Optional[str] = Field(default=None, max_length=20)
    gst_number: Optional[str] = Field(default=None, max_length=30)
    bank_account_name: Optional[str] = Field(default=None, max_length=150)
    bank_account_number: Optional[str] = Field(default=None, min_length=6, max_length=24)
    bank_ifsc: Optional[str] = Field(default=None, max_length=20)
    bank_upi_id: Optional[str] = Field(default=None, max_length=120)

    @field_validator("password")
    @classmethod
    def validate_partner_password_strength(cls, value: str) -> str:
        has_upper = any(char.isupper() for char in value)
        has_lower = any(char.islower() for char in value)
        has_digit = any(char.isdigit() for char in value)
        if not (has_upper and has_lower and has_digit):
            raise ValueError(
                "Password must include uppercase, lowercase, and numeric characters"
            )
        return value


class PartnerLoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class PartnerHotelBase(BaseModel):
    legal_name: str
    display_name: str
    gst_number: Optional[str] = None
    support_email: EmailStr
    support_phone: Optional[str] = None
    address_line: str
    city: str
    state: Optional[str] = None
    country: str = "India"
    postal_code: Optional[str] = None
    description: Optional[str] = None
    check_in_time: str = "14:00"
    check_out_time: str = "11:00"
    cancellation_window_hours: int = 24
    instant_confirmation_enabled: bool = True
    free_cancellation_enabled: bool = True
    verified_badge: bool = False
    bank_account_name: Optional[str] = None
    bank_account_number_masked: Optional[str] = None
    bank_ifsc: Optional[str] = None
    bank_upi_id: Optional[str] = None
    payout_cycle: str = "weekly"
    payout_currency: str = "INR"


class PartnerHotelUpdate(BaseModel):
    legal_name: Optional[str] = None
    display_name: Optional[str] = None
    gst_number: Optional[str] = None
    support_email: Optional[EmailStr] = None
    support_phone: Optional[str] = None
    address_line: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    description: Optional[str] = None
    check_in_time: Optional[str] = None
    check_out_time: Optional[str] = None
    cancellation_window_hours: Optional[int] = Field(default=None, ge=0, le=720)
    instant_confirmation_enabled: Optional[bool] = None
    free_cancellation_enabled: Optional[bool] = None
    verified_badge: Optional[bool] = None
    bank_account_name: Optional[str] = None
    bank_account_number: Optional[str] = Field(default=None, min_length=6, max_length=24)
    bank_ifsc: Optional[str] = None
    bank_upi_id: Optional[str] = None
    payout_cycle: Optional[str] = None
    payout_currency: Optional[str] = None


class PartnerHotelResponse(PartnerHotelBase):
    id: int
    owner_user_id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PartnerRoomCreate(BaseModel):
    room_type: RoomType
    room_type_name: str = Field(min_length=2, max_length=120)
    description: Optional[str] = None
    price: float = Field(gt=0)
    original_price: Optional[float] = Field(default=None, gt=0)
    total_room_count: int = Field(default=1, ge=1, le=1000)
    weekend_price: Optional[float] = Field(default=None, gt=0)
    holiday_price: Optional[float] = Field(default=None, gt=0)
    extra_guest_charge: float = Field(default=0, ge=0)
    availability: bool = True
    is_active: bool = True
    image_url: Optional[str] = None
    gallery_urls: List[str] = Field(default_factory=list)
    amenities: List[str] = Field(default_factory=list)
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = "India"
    max_guests: int = Field(default=2, ge=1, le=12)
    beds: int = Field(default=1, ge=1, le=12)
    bathrooms: int = Field(default=1, ge=1, le=12)
    size_sqft: Optional[int] = Field(default=None, ge=0)
    floor: Optional[int] = Field(default=None, ge=0)


class PartnerRoomUpdate(BaseModel):
    room_type: Optional[RoomType] = None
    room_type_name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    description: Optional[str] = None
    price: Optional[float] = Field(default=None, gt=0)
    original_price: Optional[float] = Field(default=None, gt=0)
    total_room_count: Optional[int] = Field(default=None, ge=1, le=1000)
    weekend_price: Optional[float] = Field(default=None, gt=0)
    holiday_price: Optional[float] = Field(default=None, gt=0)
    extra_guest_charge: Optional[float] = Field(default=None, ge=0)
    availability: Optional[bool] = None
    is_active: Optional[bool] = None
    image_url: Optional[str] = None
    gallery_urls: Optional[List[str]] = None
    amenities: Optional[List[str]] = None
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    max_guests: Optional[int] = Field(default=None, ge=1, le=12)
    beds: Optional[int] = Field(default=None, ge=1, le=12)
    bathrooms: Optional[int] = Field(default=None, ge=1, le=12)
    size_sqft: Optional[int] = Field(default=None, ge=0)
    floor: Optional[int] = Field(default=None, ge=0)


class PartnerRoomResponse(BaseModel):
    id: int
    partner_hotel_id: Optional[int] = None
    hotel_name: str
    room_type: RoomType
    room_type_name: str
    description: Optional[str] = None
    price: float
    original_price: Optional[float] = None
    total_room_count: int
    weekend_price: Optional[float] = None
    holiday_price: Optional[float] = None
    extra_guest_charge: float = 0.0
    availability: bool
    is_active: bool
    image_url: Optional[str] = None
    gallery_urls: List[str] = Field(default_factory=list)
    amenities: List[str] = Field(default_factory=list)
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    max_guests: int
    beds: int
    bathrooms: int
    size_sqft: Optional[int] = None
    floor: Optional[int] = None
    created_at: datetime


class PartnerRoomListResponse(BaseModel):
    rooms: List[PartnerRoomResponse]
    total: int


class PartnerRevenueSummary(BaseModel):
    total_bookings: int
    confirmed_bookings: int
    cancelled_bookings: int
    gross_revenue: float
    commission_amount: float
    net_revenue: float
    pending_payouts: float
    paid_out: float
    default_commission_rate: float = 0.15


class PartnerPayoutResponse(BaseModel):
    id: int
    hotel_id: int
    booking_id: Optional[int] = None
    gross_amount: float
    commission_amount: float
    net_amount: float
    currency: str
    status: PayoutStatus
    payout_reference: Optional[str] = None
    payout_date: Optional[datetime] = None
    statement_generated_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PartnerPayoutListResponse(BaseModel):
    payouts: List[PartnerPayoutResponse]
    total: int


class PartnerCalendarDay(BaseModel):
    date: str
    total_units: int
    available_units: int
    locked_units: int
    booked_units: int
    blocked_units: int
    effective_price: float
    block_reason: Optional[str] = None
    price_override: Optional[float] = None
    price_override_label: Optional[str] = None
    status: InventoryStatus


class PartnerCalendarResponse(BaseModel):
    room_id: int
    hotel_id: int
    days: List[PartnerCalendarDay]


class PartnerInventoryUpdateRequest(BaseModel):
    room_type_id: int = Field(gt=0)
    start_date: date
    end_date: date
    total_units: Optional[int] = Field(default=None, ge=0, le=1000)
    available_units: Optional[int] = Field(default=None, ge=0, le=1000)
    blocked_units: Optional[int] = Field(default=None, ge=0, le=1000)
    block_reason: Optional[str] = Field(default=None, max_length=120)
    status: InventoryStatus = InventoryStatus.AVAILABLE


class PartnerBookingListResponse(BaseModel):
    bookings: List[BookingResponse]
    total: int


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class NotificationOutboxResponse(BaseModel):
    id: int
    booking_id: Optional[int] = None
    transaction_id: Optional[int] = None
    event_type: str
    recipient_email: str
    subject: str
    body: str
    status: NotificationStatus
    failure_reason: Optional[str] = None
    sent_at: Optional[datetime] = None
    has_attachment: bool = False
    attachment_filename: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class NotificationListResponse(BaseModel):
    notifications: List[NotificationOutboxResponse]
    total: int


class ProcessNotificationsResponse(BaseModel):
    sent: int
    failed: int
    total: int


class MaintenanceRunResponse(BaseModel):
    reconciled_payments: int
    processed_notifications: int
    sent_notifications: int
    failed_notifications: int


class ReadinessResponse(BaseModel):
    status: str
    service: str
    database: str
    pending_notifications: int
    processing_payments: int


class AuditLogResponse(BaseModel):
    id: int
    actor_user_id: Optional[int] = None
    action: str
    entity_type: str
    entity_id: str
    metadata_json: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuditLogListResponse(BaseModel):
    logs: List[AuditLogResponse]
    total: int


class IncidentBookingSummary(BaseModel):
    booking_id: int
    booking_ref: str
    status: BookingStatus
    payment_status: PaymentStatus
    room_id: int
    email: EmailStr
    transaction_ref: Optional[str] = None
    created_at: Optional[datetime] = None
    hold_expires_at: Optional[datetime] = None


class IncidentDashboardResponse(BaseModel):
    orphan_paid_bookings: List[IncidentBookingSummary]
    stale_processing_bookings: List[IncidentBookingSummary]
    active_holds: List[IncidentBookingSummary]


class InventoryUpdateRequest(BaseModel):
    room_id: int
    start_date: date
    end_date: date
    total_units: int
    available_units: Optional[int] = None
    status: InventoryStatus = InventoryStatus.AVAILABLE


class InventoryResponse(BaseModel):
    id: int
    room_id: int
    inventory_date: date
    total_units: int
    available_units: int
    locked_units: int
    booked_units: int
    blocked_units: int
    status: InventoryStatus
    block_reason: Optional[str] = None
    price_override: Optional[float] = None
    price_override_label: Optional[str] = None
    locked_by_booking_id: Optional[int] = None
    lock_expires_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class InventoryListResponse(BaseModel):
    inventory: List[InventoryResponse]
    total: int


class PartnerPricingUpdateRequest(BaseModel):
    room_type_id: int = Field(gt=0)
    start_date: date
    end_date: date
    price: float = Field(gt=0)
    label: Optional[str] = Field(default=None, max_length=120)


class PartnerPricingCalendarDay(BaseModel):
    date: str
    base_price: float
    weekend_price: Optional[float] = None
    holiday_price: Optional[float] = None
    effective_price: float
    override_price: Optional[float] = None
    override_label: Optional[str] = None


class PartnerPricingCalendarResponse(BaseModel):
    room_type_id: int
    hotel_id: int
    days: List[PartnerPricingCalendarDay]


# ─── User Profile ─────────────────────────────────────────────────────────────

class UserProfileUpdate(BaseModel):
    full_name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=30)
    avatar_url: Optional[str] = Field(default=None, max_length=500)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: Optional[str]) -> Optional[str]:
        if value is None or value.strip() == "":
            return value
        cleaned = value.strip()
        if not re.fullmatch(r"[0-9+\-\s()]{7,30}", cleaned):
            raise ValueError("Phone number format is invalid")
        return cleaned


class PhoneOtpRequest(BaseModel):
    phone: str = Field(min_length=7, max_length=30)

    @field_validator("phone")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        cleaned = value.strip()
        if not re.fullmatch(r"[0-9+\-\s()]{7,30}", cleaned):
            raise ValueError("Phone number format is invalid")
        return cleaned


class PhoneOtpVerifyRequest(PhoneOtpRequest):
    otp: str = Field(min_length=6, max_length=6, pattern=r"^[0-9]{6}$")


class PhoneOtpResponse(BaseModel):
    message: str
    phone: str
    expires_in_seconds: int
    dev_code: Optional[str] = None


class UserDetailResponse(BaseModel):
    id: int
    email: EmailStr
    full_name: str
    phone: Optional[str] = None
    phone_verified: bool = False
    pending_phone: Optional[str] = None
    avatar_url: Optional[str] = None
    is_admin: bool
    is_active: bool
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ─── Auth Extensions ──────────────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_password_strength(cls, value: str) -> str:
        has_upper = any(c.isupper() for c in value)
        has_lower = any(c.islower() for c in value)
        has_digit = any(c.isdigit() for c in value)
        if not (has_upper and has_lower and has_digit):
            raise ValueError(
                "Password must include uppercase, lowercase, and numeric characters"
            )
        return value


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=10, max_length=128)

    @field_validator("new_password")
    @classmethod
    def validate_new_password_strength(cls, value: str) -> str:
        has_upper = any(c.isupper() for c in value)
        has_lower = any(c.islower() for c in value)
        has_digit = any(c.isdigit() for c in value)
        if not (has_upper and has_lower and has_digit):
            raise ValueError(
                "Password must include uppercase, lowercase, and numeric characters"
            )
        return value


class SocialLoginRequest(BaseModel):
    provider: str  # "google"
    id_token: str  # Google ID token


class MessageResponse(BaseModel):
    message: str


# ─── Review Schemas ───────────────────────────────────────────────────────────

class ReviewCreate(BaseModel):
    room_id: int = Field(gt=0)
    booking_id: int = Field(gt=0)
    rating: int = Field(ge=1, le=5)
    cleanliness_rating: Optional[int] = Field(default=None, ge=1, le=5)
    service_rating: Optional[int] = Field(default=None, ge=1, le=5)
    value_rating: Optional[int] = Field(default=None, ge=1, le=5)
    location_rating: Optional[int] = Field(default=None, ge=1, le=5)
    title: Optional[str] = Field(default=None, max_length=200)
    body: Optional[str] = Field(default=None, max_length=2000)


class HostReplyRequest(BaseModel):
    reply: str = Field(min_length=2, max_length=1000)


class ReviewResponse(BaseModel):
    id: int
    user_id: int
    room_id: int
    booking_id: int
    rating: int
    cleanliness_rating: Optional[int] = None
    service_rating: Optional[int] = None
    value_rating: Optional[int] = None
    location_rating: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    is_verified: bool
    host_reply: Optional[str] = None
    host_replied_at: Optional[datetime] = None
    reviewer_name: str = ""
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReviewListResponse(BaseModel):
    reviews: List[ReviewResponse]
    total: int
    average_rating: float
    rating_breakdown: dict


class RatingBreakdown(BaseModel):
    average_rating: float
    total_reviews: int
    cleanliness_avg: Optional[float] = None
    service_avg: Optional[float] = None
    value_avg: Optional[float] = None
    location_avg: Optional[float] = None
    five_star: int = 0
    four_star: int = 0
    three_star: int = 0
    two_star: int = 0
    one_star: int = 0


# ─── Wishlist Schemas ─────────────────────────────────────────────────────────

class WishlistToggleResponse(BaseModel):
    saved: bool
    message: str


class WishlistItemResponse(BaseModel):
    id: int
    room_id: int
    room: Optional[RoomResponse] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WishlistResponse(BaseModel):
    items: list[WishlistItemResponse]
    total: int


class WishlistStatusResponse(BaseModel):
    room_ids: list[int]


# ─── Razorpay Schemas ─────────────────────────────────────────────────────────

class RazorpayOrderRequest(BaseModel):
    booking_id: int
    payment_method: str  # upi, gpay, phonepe, card, netbanking, wallet, mock
    idempotency_key: Optional[str] = None


class RazorpayOrderResponse(BaseModel):
    order_id: str
    transaction_ref: str
    amount_paise: int
    currency: str
    key_id: str
    idempotent: bool = False


class RazorpayVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    transaction_ref: str


class RazorpayVerifyResponse(BaseModel):
    status: str
    transaction_ref: str
    razorpay_payment_id: str
    booking_status: Optional[str] = None
