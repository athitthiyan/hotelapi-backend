from __future__ import annotations

import importlib
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

import main
import models
import schemas
from database import settings
from routers import auth, bookings, partner, payments, reviews, wishlist
from services import inventory_service

rooms_router = importlib.import_module("routers.rooms")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_user(
    db_session,
    *,
    email: str,
    full_name: str = "Test User",
    password: str | None = "StrongPass123",
    is_admin: bool = False,
    is_partner: bool = False,
    is_active: bool = True,
    google_id: str | None = None,
    avatar_url: str | None = None,
):
    user = models.User(
        email=email,
        full_name=full_name,
        hashed_password=auth.hash_password(password) if password is not None else None,
        is_admin=is_admin,
        is_partner=is_partner,
        is_active=is_active,
        google_id=google_id,
        avatar_url=avatar_url,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def make_room(
    db_session,
    *,
    partner_hotel_id: int | None = None,
    hotel_name: str = "Coverage Hotel",
    availability: bool = True,
    price: float = 250.0,
):
    room = models.Room(
        partner_hotel_id=partner_hotel_id,
        hotel_name=hotel_name,
        room_type=models.RoomType.SUITE,
        description="Coverage room",
        price=price,
        availability=availability,
        city="Chennai",
        country="India",
        max_guests=3,
        beds=2,
        bathrooms=1,
        rating=4.5,
        review_count=0,
        is_featured=False,
    )
    db_session.add(room)
    db_session.commit()
    db_session.refresh(room)
    return room


def make_booking(
    db_session,
    *,
    room_id: int,
    email: str,
    user_id: int | None = None,
    status: models.BookingStatus = models.BookingStatus.PENDING,
    payment_status: models.PaymentStatus = models.PaymentStatus.PENDING,
    hold_expires_at: datetime | None = None,
    check_in: datetime | None = None,
    check_out: datetime | None = None,
):
    now = datetime.now(timezone.utc)
    booking = models.Booking(
        booking_ref=f"BK{datetime.now(timezone.utc).timestamp():.6f}-{email}".replace(".", "").replace("@", "").replace(":", ""),
        user_name="Coverage Guest",
        email=email,
        user_id=user_id,
        room_id=room_id,
        phone="9999999999",
        check_in=check_in or (now + timedelta(days=1)),
        check_out=check_out or (now + timedelta(days=3)),
        hold_expires_at=hold_expires_at,
        guests=2,
        nights=2,
        room_rate=250.0,
        taxes=30.0,
        service_fee=15.0,
        total_amount=295.0,
        status=status,
        payment_status=payment_status,
    )
    db_session.add(booking)
    db_session.commit()
    db_session.refresh(booking)
    return booking


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeRequest:
    """Minimal Request mock for endpoints that need request.client / request.headers."""
    def __init__(self):
        self.client = SimpleNamespace(host="127.0.0.1")
        self.headers = {}


_fake_request = FakeRequest()


class FakeAsyncClient:
    def __init__(self, *, response: FakeResponse | None = None, exc: Exception | None = None):
        self.response = response
        self.exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, *args, **kwargs):
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture(autouse=True)
def _disable_google_audience_check():
    """Disable Google audience validation in tests unless a test explicitly sets it."""
    original = settings.google_client_id
    settings.google_client_id = ""
    yield
    settings.google_client_id = original


def partner_payload(**overrides):
    payload = {
        "email": "partner-edge@example.com",
        "full_name": "Partner Edge",
        "password": "PartnerPass123",
        "legal_name": "Edge Hospitality Pvt Ltd",
        "display_name": "Edge Suites",
        "support_email": "support@edge.example.com",
        "support_phone": "9876543210",
        "address_line": "12 Edge Road",
        "city": "Chennai",
        "state": "Tamil Nadu",
        "country": "India",
        "postal_code": "600001",
        "gst_number": "33ABCDE1234F1Z5",
        "bank_account_name": "Edge Suites",
        "bank_account_number": "123456789012",
        "bank_ifsc": "HDFC0001234",
        "bank_upi_id": "edge@upi",
    }
    payload.update(overrides)
    return payload


class TestSchemaCoverageEdges:
    def test_booking_create_phone_validation_covers_blank_and_invalid(self):
        valid = schemas.BookingCreate(
            user_name="Athit",
            email="athit@example.com",
            phone="",
            room_id=1,
            check_in=datetime.now(timezone.utc) + timedelta(days=1),
            check_out=datetime.now(timezone.utc) + timedelta(days=2),
            guests=1,
        )
        assert valid.phone == ""

        with pytest.raises(ValidationError):
            schemas.BookingCreate(
                user_name="Athit",
                email="athit@example.com",
                phone="bad-phone-ext",
                room_id=1,
                check_in=datetime.now(timezone.utc) + timedelta(days=1),
                check_out=datetime.now(timezone.utc) + timedelta(days=2),
                guests=1,
            )

    def test_payment_schema_validators_cover_error_branches(self):
        assert schemas.CreatePaymentIntent(booking_id=1).payment_method == "card"
        assert schemas.CreatePaymentIntent(booking_id=1, idempotency_key=None).idempotency_key is None
        assert (
            schemas.PaymentSuccess(
                booking_id=1,
                transaction_ref="TXN12345",
                payment_method="card",
                card_last4=None,
            ).card_last4
            is None
        )

        with pytest.raises(ValidationError):
            schemas.CreatePaymentIntent(
                booking_id=1,
                payment_method="paypal",
                idempotency_key="valid_key_01",
            )
        with pytest.raises(ValidationError):
            schemas.CreatePaymentIntent(
                booking_id=1,
                payment_method="card",
                idempotency_key="bad key",
            )
        with pytest.raises(ValidationError):
            schemas.PaymentSuccess(
                booking_id=1,
                transaction_ref="TXN12345",
                payment_method="wire",
            )
        with pytest.raises(ValidationError):
            schemas.PaymentSuccess(
                booking_id=1,
                transaction_ref="TXN12345",
                payment_method="card",
                card_last4="12AB",
            )

    def test_auth_and_partner_schema_password_validators_cover_failures(self):
        with pytest.raises(ValidationError):
            schemas.PartnerRegisterRequest(**partner_payload(password="alllowercase"))
        with pytest.raises(ValidationError):
            schemas.ResetPasswordRequest(token="token", new_password="weakpassxx")
        with pytest.raises(ValidationError):
            schemas.ChangePasswordRequest(current_password="old", new_password="weakpassxx")

    def test_user_profile_phone_validator_covers_blank_and_invalid(self):
        assert schemas.UserProfileUpdate(phone="   ").phone == "   "
        with pytest.raises(ValidationError):
            schemas.UserProfileUpdate(phone="not/a/phone")


class TestMainCoverageEdges:
    def test_run_expired_hold_release_closes_session(self):
        db = MagicMock()
        session_factory = MagicMock(return_value=db)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main.bookings, "release_expired_holds", lambda session: 7)
            assert main.run_expired_hold_release(session_factory=session_factory) == 7

        db.close.assert_called_once()

    def test_shutdown_scheduler_handles_missing_scheduler(self):
        state = SimpleNamespace()
        assert main.shutdown_scheduler(state) is None

    def test_startup_checks_starts_scheduler_when_available(self):
        connection = MagicMock()
        manager = MagicMock()
        manager.__enter__.return_value = connection
        manager.__exit__.return_value = False
        inspector = MagicMock()
        inspector.get_table_names.return_value = list(main.Base.metadata.tables.keys())
        scheduler = MagicMock()
        scheduler_factory = MagicMock(return_value=scheduler)
        state = SimpleNamespace()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "BackgroundScheduler", scheduler_factory)
            mp.setattr(main.engine, "begin", lambda: manager)
            mp.setattr(main, "inspect", lambda connection: inspector)
            mp.setattr(main, "validate_runtime_configuration", lambda settings: None)
            main.startup_checks(state)

        scheduler_factory.assert_called_once_with(timezone="UTC")
        assert scheduler.add_job.call_count == 2
        scheduler.start.assert_called_once()
        assert state.hold_expiry_scheduler is scheduler

    @pytest.mark.anyio
    async def test_lifespan_runs_startup_and_shutdown(self):
        app = FastAPI()
        app.state = SimpleNamespace()
        calls: list[str] = []

        def fake_startup(state):
            calls.append("startup")
            state.started = True

        def fake_shutdown(state):
            calls.append("shutdown")
            assert state.started is True

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(main, "startup_checks", fake_startup)
            mp.setattr(main, "shutdown_scheduler", fake_shutdown)
            async with main.lifespan(app):
                calls.append("inside")

        assert calls == ["startup", "inside", "shutdown"]


class TestAuthCoverageEdges:
    def test_update_profile_sets_optional_fields(self, db_session):
        user = make_user(db_session, email="profile-edge@example.com")
        user.phone = "+91 9000000000"
        user.phone_verified = True
        db_session.commit()

        updated = auth.update_profile(
            schemas.UserProfileUpdate(
                full_name="Updated Edge",
                phone="+91 9000000000",
                avatar_url="https://example.com/avatar.png",
            ),
            user=user,
            db=db_session,
        )

        assert updated.full_name == "Updated Edge"
        assert updated.phone == "+91 9000000000"
        assert updated.avatar_url == "https://example.com/avatar.png"

    def test_reset_password_rejects_inactive_user_record(self, db_session):
        user = make_user(
            db_session,
            email="inactive-reset@example.com",
            is_active=False,
        )
        token = "reset-token-edge"
        db_session.add(
            models.PasswordResetToken(
                user_id=user.id,
                token_hash=auth._hash_reset_token(token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            )
        )
        db_session.commit()

        with pytest.raises(HTTPException, match="User account is not available"):
            auth.reset_password(
                schemas.ResetPasswordRequest(token=token, new_password="NewStrong123"),
                db=db_session,
            )

    @pytest.mark.anyio
    async def test_social_login_covers_provider_and_google_error_paths(self, db_session):
        with pytest.raises(HTTPException, match="Unsupported provider"):
            await auth.social_login(
                schemas.SocialLoginRequest(provider="github", id_token="token"),
                request=_fake_request,
                response=Response(),
                db=db_session,
            )

        request_error = httpx.RequestError(
            "boom",
            request=httpx.Request("GET", auth.GOOGLE_USERINFO_URL),
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(exc=request_error),
            )
            with pytest.raises(HTTPException, match="Failed to verify Google token"):
                await auth.social_login(
                    schemas.SocialLoginRequest(provider="google", id_token="token"),
                    request=_fake_request,
                    response=Response(),
                    db=db_session,
                )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(response=FakeResponse(401, {})),
            )
            with pytest.raises(HTTPException, match="Google token verification failed"):
                await auth.social_login(
                    schemas.SocialLoginRequest(provider="google", id_token="token"),
                    request=_fake_request,
                    response=Response(),
                    db=db_session,
                )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(response=FakeResponse(200, {"sub": ""})),
            )
            with pytest.raises(HTTPException, match="Insufficient data from Google"):
                await auth.social_login(
                    schemas.SocialLoginRequest(provider="google", id_token="token"),
                    request=_fake_request,
                    response=Response(),
                    db=db_session,
                )

    @pytest.mark.anyio
    async def test_google_audience_mismatch_is_rejected(self, db_session):
        """When google_client_id is configured, tokeninfo aud must match."""
        original = settings.google_client_id
        settings.google_client_id = "expected-client-id"
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    auth._httpx,
                    "AsyncClient",
                    lambda timeout=10: FakeAsyncClient(
                        response=FakeResponse(200, {"aud": "wrong-client-id", "expires_in": "3600"})
                    ),
                )
                with pytest.raises(HTTPException, match="Google token audience mismatch"):
                    await auth.social_login(
                        schemas.SocialLoginRequest(provider="google", id_token="token"),
                        request=_fake_request,
                        response=Response(),
                        db=db_session,
                    )
        finally:
            settings.google_client_id = original

    @pytest.mark.anyio
    async def test_google_expired_token_is_rejected(self, db_session):
        """When google_client_id is configured, expired tokens are rejected."""
        original = settings.google_client_id
        settings.google_client_id = "expected-client-id"
        try:
            with pytest.MonkeyPatch.context() as mp:
                mp.setattr(
                    auth._httpx,
                    "AsyncClient",
                    lambda timeout=10: FakeAsyncClient(
                        response=FakeResponse(200, {"aud": "expected-client-id", "expires_in": "0"})
                    ),
                )
                with pytest.raises(HTTPException, match="Google token has expired"):
                    await auth.social_login(
                        schemas.SocialLoginRequest(provider="google", id_token="token"),
                        request=_fake_request,
                        response=Response(),
                        db=db_session,
                    )
        finally:
            settings.google_client_id = original

    @pytest.mark.anyio
    async def test_social_login_links_existing_user_and_blocks_inactive_user(self, db_session):
        existing = make_user(
            db_session,
            email="linked@example.com",
            avatar_url=None,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(
                    response=FakeResponse(
                        200,
                        {
                            "sub": "google-123",
                            "email": existing.email.upper(),
                            "name": "Linked User",
                            "picture": "https://example.com/picture.png",
                            "email_verified": True,
                        },
                    )
                ),
            )
            response = await auth.social_login(
                schemas.SocialLoginRequest(provider="google", id_token="token"),
                request=_fake_request,
                response=Response(),
                db=db_session,
            )

        db_session.refresh(existing)
        assert response.user.id == existing.id
        assert existing.google_id == "google-123"
        assert existing.avatar_url == "https://example.com/picture.png"

        existing.is_active = False
        db_session.commit()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(
                    response=FakeResponse(
                        200,
                        {
                            "sub": "google-123",
                            "email": existing.email,
                            "name": "Linked User",
                            "email_verified": True,
                        },
                    )
                ),
            )
            with pytest.raises(HTTPException, match="deactivated"):
                await auth.social_login(
                    schemas.SocialLoginRequest(provider="google", id_token="token"),
                    request=_fake_request,
                    response=Response(),
                    db=db_session,
                )

    @pytest.mark.anyio
    async def test_social_login_creates_new_user_when_email_is_unknown(self, db_session):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(
                    response=FakeResponse(
                        200,
                        {
                            "sub": "google-new-user",
                            "email": "fresh-user@example.com",
                            "name": "Fresh User",
                            "picture": "https://example.com/fresh.png",
                            "email_verified": True,
                        },
                    )
                ),
            )
            response = await auth.social_login(
                schemas.SocialLoginRequest(provider="google", id_token="token"),
                request=_fake_request,
                response=Response(),
                db=db_session,
            )

        created_user = (
            db_session.query(models.User)
            .filter(models.User.email == "fresh-user@example.com")
            .first()
        )
        assert response.user.email == "fresh-user@example.com"
        assert created_user is not None
        assert created_user.google_id == "google-new-user"
        assert created_user.avatar_url == "https://example.com/fresh.png"

    @pytest.mark.anyio
    async def test_social_login_reuses_google_id_match_without_commit_path(self, db_session):
        existing = make_user(
            db_session,
            email="google-match@example.com",
            google_id="google-match-123",
            avatar_url="https://example.com/existing.png",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(
                    response=FakeResponse(
                        200,
                        {
                            "sub": "google-match-123",
                            "email": existing.email,
                            "name": "Google Match",
                            "picture": "https://example.com/new-picture.png",
                            "email_verified": True,
                        },
                    )
                ),
            )
            response = await auth.social_login(
                schemas.SocialLoginRequest(provider="google", id_token="token"),
                request=_fake_request,
                response=Response(),
                db=db_session,
            )

        db_session.refresh(existing)
        assert response.user.id == existing.id
        assert existing.avatar_url == "https://example.com/existing.png"

    @pytest.mark.anyio
    async def test_social_login_links_existing_email_without_avatar_backfill(self, db_session):
        existing = make_user(
            db_session,
            email="email-link@example.com",
            avatar_url="https://example.com/already-set.png",
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auth._httpx,
                "AsyncClient",
                lambda timeout=10: FakeAsyncClient(
                    response=FakeResponse(
                        200,
                        {
                            "sub": "google-email-link",
                            "email": existing.email,
                            "name": "Email Link",
                            "picture": "https://example.com/should-not-overwrite.png",
                            "email_verified": True,
                        },
                    )
                ),
            )
            response = await auth.social_login(
                schemas.SocialLoginRequest(provider="google", id_token="token"),
                request=_fake_request,
                response=Response(),
                db=db_session,
            )

        db_session.refresh(existing)
        assert response.user.id == existing.id
        assert existing.google_id == "google-email-link"
        assert existing.avatar_url == "https://example.com/already-set.png"

    def test_my_bookings_counts_upcoming_past_and_cancelled(self, db_session):
        user = make_user(db_session, email="bookings-edge@example.com")
        room = make_room(db_session)
        now = datetime.now(timezone.utc)
        db_session.add_all(
            [
                models.Booking(
                    booking_ref="BK-UPCOMING",
                    user_name="Guest",
                    email=user.email,
                    user_id=user.id,
                    room_id=room.id,
                    check_in=now + timedelta(days=1),
                    check_out=now + timedelta(days=2),
                    guests=1,
                    nights=1,
                    room_rate=100.0,
                    taxes=10.0,
                    service_fee=5.0,
                    total_amount=115.0,
                    status=models.BookingStatus.CONFIRMED,
                    payment_status=models.PaymentStatus.PAID,
                ),
                models.Booking(
                    booking_ref="BK-PAST",
                    user_name="Guest",
                    email=user.email,
                    user_id=user.id,
                    room_id=room.id,
                    check_in=(now - timedelta(days=4)).replace(tzinfo=None),
                    check_out=(now - timedelta(days=2)).replace(tzinfo=None),
                    guests=1,
                    nights=2,
                    room_rate=100.0,
                    taxes=10.0,
                    service_fee=5.0,
                    total_amount=215.0,
                    status=models.BookingStatus.CONFIRMED,
                    payment_status=models.PaymentStatus.PAID,
                ),
                models.Booking(
                    booking_ref="BK-CANCELLED",
                    user_name="Guest",
                    email=user.email,
                    user_id=user.id,
                    room_id=room.id,
                    check_in=now - timedelta(days=1),
                    check_out=now,
                    guests=1,
                    nights=1,
                    room_rate=100.0,
                    taxes=10.0,
                    service_fee=5.0,
                    total_amount=115.0,
                    status=models.BookingStatus.CANCELLED,
                    payment_status=models.PaymentStatus.FAILED,
                ),
            ]
        )
        db_session.commit()

        response = auth.my_bookings(user=user, db=db_session)
        assert response.total == 1
        assert response.upcoming == 1
        assert response.past == 1
        assert response.cancelled == 1
        assert response.expired == 0
        assert response.tab == "upcoming"
        assert len(response.bookings) == 1
        assert response.bookings[0].booking_ref == "BK-UPCOMING"


class TestReviewCoverageEdges:
    def test_review_helpers_cover_guest_and_empty_rating_paths(self, db_session):
        assert reviews._build_rating_breakdown([]) == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
        assert reviews._build_rating_breakdown([SimpleNamespace(rating=5), SimpleNamespace(rating=3)]) == {
            "1": 0,
            "2": 0,
            "3": 1,
            "4": 0,
            "5": 1,
        }
        assert reviews._calc_avg([None, None]) is None
        assert reviews._calc_avg([5, None, 3]) == 4.0
        assert reviews._calc_avg([]) is None

        review = models.Review(
            id=1,
            user_id=1,
            room_id=1,
            booking_id=1,
            rating=5,
            is_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        response = reviews._review_to_response(review)
        assert response.reviewer_name == "Guest"

        reviews._refresh_room_rating(db_session, room_id=999999)

    def test_create_review_rejects_non_confirmed_booking_and_host_reply_succeeds(self, db_session):
        user = make_user(db_session, email="review-edge@example.com")
        admin = make_user(db_session, email="review-admin@example.com", is_admin=True)
        room = make_room(db_session)
        booking = make_booking(
            db_session,
            room_id=room.id,
            email=user.email,
            user_id=user.id,
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )

        with pytest.raises(HTTPException, match="confirmed or completed"):
            reviews.create_review(
                schemas.ReviewCreate(room_id=room.id, booking_id=booking.id, rating=4),
                current_user=user,
                db=db_session,
            )

        booking.status = models.BookingStatus.COMPLETED
        db_session.commit()

        created = reviews.create_review(
            schemas.ReviewCreate(room_id=room.id, booking_id=booking.id, rating=5),
            current_user=user,
            db=db_session,
        )
        replied = reviews.host_reply(
            created.id,
            schemas.HostReplyRequest(reply="Thank you"),
            _admin=admin,
            db=db_session,
        )
        assert replied.host_reply == "Thank you"
        assert replied.host_replied_at is not None


class TestWishlistCoverageEdges:
    def test_toggle_wishlist_integrity_error_returns_already_saved(self):
        fake_room_query = MagicMock()
        fake_room_query.filter.return_value.first.return_value = object()
        fake_wishlist_query = MagicMock()
        fake_wishlist_query.filter.return_value.first.return_value = None
        db = MagicMock()
        db.query.side_effect = [fake_room_query, fake_wishlist_query]
        db.commit.side_effect = [IntegrityError("stmt", "params", "orig")]

        response = wishlist.toggle_wishlist(
            room_id=10,
            current_user=SimpleNamespace(id=1),
            db=db,
        )

        assert response.saved is True
        assert response.message == "Already saved"
        db.rollback.assert_called_once()


class TestPartnerCoverageEdges:
    def test_partner_register_duplicate_and_login_forbidden_variants(self, client, db_session):
        make_user(db_session, email="duplicate-partner@example.com", is_partner=True)

        duplicate = client.post(
            "/partner/register",
            json=partner_payload(email="duplicate-partner@example.com"),
        )
        assert duplicate.status_code == 409

        inactive_user = make_user(
            db_session,
            email="inactive-partner@example.com",
            is_partner=True,
            is_active=False,
        )
        inactive_login = client.post(
            "/partner/login",
            json={"email": inactive_user.email, "password": "StrongPass123"},
        )
        assert inactive_login.status_code == 403

        non_partner_user = make_user(
            db_session,
            email="not-partner@example.com",
            is_partner=False,
        )
        non_partner_login = client.post(
            "/partner/login",
            json={"email": non_partner_user.email, "password": "StrongPass123"},
        )
        assert non_partner_login.status_code == 403

        valid_login = client.post(
            "/partner/login",
            json={"email": "duplicate-partner@example.com", "password": "StrongPass123"},
        )
        assert valid_login.status_code == 200

    def test_partner_helper_functions_cover_edge_inputs(self, db_session):
        assert partner._mask_account_number(None) is None
        assert partner._mask_account_number("1234") == "1234"
        assert partner._decode_string_list(None) == []
        assert partner._decode_string_list("not-json") == []
        assert partner._encode_string_list([" WiFi ", "", "Pool"]) == '["WiFi", "Pool"]'

        with pytest.raises(HTTPException, match="Partner hotel not found"):
            partner._get_partner_hotel_or_404(db_session, partner_user_id=999)
        with pytest.raises(HTTPException, match="Partner room not found"):
            partner._get_partner_room_or_404(db_session, partner_hotel_id=999, room_id=999)

    def test_partner_login_without_password_returns_401(self, client, db_session):
        make_user(
            db_session,
            email="nopassword-partner@example.com",
            password=None,
            is_partner=True,
        )

        response = client.post(
            "/partner/login",
            json={"email": "nopassword-partner@example.com", "password": "PartnerPass123"},
        )
        assert response.status_code == 401

    def test_partner_endpoints_cover_missing_hotel_delete_success_and_calendar_update(self, client):
        register = client.post("/partner/register", json=partner_payload())
        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        room_response = client.post(
            "/partner/rooms",
            headers=headers,
            json={
                "room_type": "suite",
                "room_type_name": "Edge Suite",
                "description": "Edge room",
                "price": 4500,
                "availability": True,
                "total_room_count": 4,
                "weekend_price": 4800,
                "holiday_price": 5200,
                "extra_guest_charge": 500,
                "is_active": True,
                "gallery_urls": ["https://example.com/1.jpg"],
                "amenities": ["WiFi"],
                "location": "Marina",
                "city": "Chennai",
                "country": "India",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 1,
            },
        )
        room_id = room_response.json()["id"]

        hotel_update = client.put(
            "/partner/hotel",
            headers=headers,
            json={"bank_account_number": "654321"},
        )
        assert hotel_update.status_code == 200
        assert hotel_update.json()["bank_account_number_masked"] == "**4321"

        room_update = client.put(
            f"/partner/rooms/{room_id}",
            headers=headers,
            json={"price": 4700},
        )
        assert room_update.status_code == 200
        assert room_update.json()["price"] == 4700

        start_date = (date.today() + timedelta(days=1)).isoformat()
        first_calendar = client.put(
            "/partner/calendar",
            headers=headers,
            json={
                "room_type_id": room_id,
                "start_date": start_date,
                "end_date": start_date,
                "total_units": 3,
                "available_units": 1,
                "status": "available",
            },
        )
        assert first_calendar.status_code == 200

        second_calendar = client.put(
            "/partner/calendar",
            headers=headers,
            json={
                "room_type_id": room_id,
                "start_date": start_date,
                "end_date": start_date,
                "total_units": 4,
                "available_units": None,
                "status": "blocked",
            },
        )
        assert second_calendar.status_code == 200
        target_day = next(day for day in second_calendar.json()["days"] if day["date"] == start_date)
        assert target_day["available_units"] == 2

        payout_first = client.get("/partner/payouts", headers=headers)
        payout_second = client.get("/partner/payouts", headers=headers)
        assert payout_first.status_code == 200
        assert payout_second.status_code == 200

        delete_response = client.delete(f"/partner/rooms/{room_id}", headers=headers)
        assert delete_response.status_code == 204

    def test_partner_hotel_room_and_payout_generation_paths(self, client):
        register = client.post(
            "/partner/register",
            json=partner_payload(email="payouts-partner@example.com"),
        )
        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        hotel_response = client.get("/partner/hotel", headers=headers)
        assert hotel_response.status_code == 200

        room_response = client.post(
            "/partner/rooms",
            headers=headers,
            json={
                "room_type": "suite",
                "room_type_name": "Payout Suite",
                "description": "Payout room",
                "price": 5500,
                "availability": True,
                "total_room_count": 6,
                "weekend_price": 5800,
                "holiday_price": 6200,
                "extra_guest_charge": 650,
                "is_active": True,
                "gallery_urls": ["https://example.com/a.jpg", "https://example.com/b.jpg"],
                "amenities": ["WiFi", "Pool"],
                "location": "ECR",
                "city": "Chennai",
                "country": "India",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 1,
            },
        )
        room_id = room_response.json()["id"]

        updated_room = client.put(
            f"/partner/rooms/{room_id}",
            headers=headers,
            json={
                "gallery_urls": ["https://example.com/new.jpg"],
                "amenities": ["Spa"],
            },
        )
        assert updated_room.status_code == 200
        assert updated_room.json()["gallery_urls"] == ["https://example.com/new.jpg"]
        assert updated_room.json()["amenities"] == ["Spa"]

        db = client.app.state.testing_session_local()
        try:
            partner_user = (
                db.query(models.User)
                .filter(models.User.email == "payouts-partner@example.com")
                .first()
            )
            hotel = db.query(models.PartnerHotel).filter(models.PartnerHotel.owner_user_id == partner_user.id).first()
            booking = make_booking(
                db,
                room_id=room_id,
                email="paid-guest@example.com",
                status=models.BookingStatus.CONFIRMED,
                payment_status=models.PaymentStatus.PAID,
            )
            assert booking.room_id == room_id
            assert hotel is not None
        finally:
            db.close()

        payouts = client.get("/partner/payouts", headers=headers)
        assert payouts.status_code == 200
        assert payouts.json()["total"] == 1

    def test_partner_payouts_return_existing_records_without_generation(self, client):
        register = client.post(
            "/partner/register",
            json=partner_payload(email="existing-payouts@example.com"),
        )
        token = register.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        room_response = client.post(
            "/partner/rooms",
            headers=headers,
            json={
                "room_type": "suite",
                "room_type_name": "Existing Payout Suite",
                "description": "Existing payout room",
                "price": 6000,
                "availability": True,
                "total_room_count": 5,
                "weekend_price": 6400,
                "holiday_price": 6800,
                "extra_guest_charge": 700,
                "is_active": True,
                "gallery_urls": [],
                "amenities": [],
                "location": "OMR",
                "city": "Chennai",
                "country": "India",
                "max_guests": 2,
                "beds": 1,
                "bathrooms": 1,
            },
        )
        room_id = room_response.json()["id"]

        db = client.app.state.testing_session_local()
        try:
            partner_user = db.query(models.User).filter(models.User.email == "existing-payouts@example.com").first()
            hotel = db.query(models.PartnerHotel).filter(models.PartnerHotel.owner_user_id == partner_user.id).first()
            booking = make_booking(
                db,
                room_id=room_id,
                email="existing-payout-guest@example.com",
                status=models.BookingStatus.CONFIRMED,
                payment_status=models.PaymentStatus.PAID,
            )
            db.add(
                models.PartnerPayout(
                    hotel_id=hotel.id,
                    booking_id=booking.id,
                    gross_amount=booking.total_amount,
                    commission_amount=round(booking.total_amount * partner.DEFAULT_COMMISSION_RATE, 2),
                    net_amount=round(booking.total_amount * (1 - partner.DEFAULT_COMMISSION_RATE), 2),
                    currency="INR",
                    status="paid",
                    payout_reference="payout_existing_001",
                )
            )
            db.commit()
        finally:
            db.close()

        payouts = client.get("/partner/payouts", headers=headers)
        assert payouts.status_code == 200
        assert payouts.json()["total"] == 1


class TestPaymentCoverageEdges:
    def test_payment_helpers_cover_expired_and_retry_edge_paths(self, db_session):
        room = make_room(db_session)

        expired_booking = make_booking(
            db_session,
            room_id=room.id,
            email="expired-pay@example.com",
            hold_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(payments, "release_expired_holds", lambda db, booking_id=None: None)
            mp.setattr(payments, "expire_stale_booking_hold", lambda booking: True)
            with pytest.raises(HTTPException, match="cannot be paid"):
                payments.ensure_booking_can_accept_payment(db_session, expired_booking)

        missing_lock_booking = make_booking(
            db_session,
            room_id=room.id,
            email="missing-lock@example.com",
            hold_expires_at=None,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(payments, "release_expired_holds", lambda db, booking_id=None: None)
            mp.setattr(payments, "expire_stale_booking_hold", lambda booking: False)
            mp.setattr(payments, "get_success_transaction_for_booking", lambda db, booking_id: None)
            mp.setattr(payments, "has_recent_failed_payment_burst", lambda db, booking_id: False)
            mp.setattr(payments, "is_booking_inventory_locked", lambda db, booking: False)
            with pytest.raises(HTTPException, match="hold has expired"):
                payments.ensure_booking_can_accept_payment(db_session, missing_lock_booking)

        valid_hold_booking = make_booking(
            db_session,
            room_id=room.id,
            email="valid-hold@example.com",
            hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(payments, "release_expired_holds", lambda db, booking_id=None: None)
            mp.setattr(payments, "expire_stale_booking_hold", lambda booking: False)
            mp.setattr(payments, "get_success_transaction_for_booking", lambda db, booking_id: None)
            mp.setattr(payments, "has_recent_failed_payment_burst", lambda db, booking_id: False)
            mp.setattr(payments, "is_booking_inventory_locked", lambda db, booking: False)
            mp.setattr(payments, "lock_inventory_for_booking", lambda db, booking, lock_expires_at: (_ for _ in ()).throw(ValueError("gone")))
            with pytest.raises(HTTPException, match="inventory was released"):
                payments.ensure_booking_can_accept_payment(db_session, valid_hold_booking)

        existing_success_booking = make_booking(
            db_session,
            room_id=room.id,
            email="existing-success@example.com",
            hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(payments, "release_expired_holds", lambda db, booking_id=None: None)
            mp.setattr(payments, "expire_stale_booking_hold", lambda booking: False)
            mp.setattr(payments, "get_success_transaction_for_booking", lambda db, booking_id: object())
            with pytest.raises(HTTPException, match="Booking already paid"):
                payments.ensure_booking_can_accept_payment(db_session, existing_success_booking)

        relock_booking = make_booking(
            db_session,
            room_id=room.id,
            email="relock-success@example.com",
            hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        re_locked = {"called": False}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(payments, "release_expired_holds", lambda db, booking_id=None: None)
            mp.setattr(payments, "expire_stale_booking_hold", lambda booking: False)
            mp.setattr(payments, "get_success_transaction_for_booking", lambda db, booking_id: None)
            mp.setattr(payments, "has_recent_failed_payment_burst", lambda db, booking_id: False)
            mp.setattr(payments, "is_booking_inventory_locked", lambda db, booking: False)
            mp.setattr(
                payments,
                "lock_inventory_for_booking",
                lambda db, booking, lock_expires_at: re_locked.__setitem__("called", True),
            )
            accepted = payments.ensure_booking_can_accept_payment(db_session, relock_booking)
        assert accepted.id == relock_booking.id
        assert re_locked["called"] is True

    def test_record_failed_transaction_releases_inventory_when_hold_missing(self, db_session):
        room = make_room(db_session)
        booking = make_booking(
            db_session,
            room_id=room.id,
            email="failed-txn@example.com",
            hold_expires_at=None,
        )

        with pytest.MonkeyPatch.context() as mp:
            released = {"called": False}
            mp.setattr(
                payments,
                "release_inventory_for_booking",
                lambda db, booking: released.__setitem__("called", True),
            )
            mp.setattr(payments, "queue_payment_failure_email", lambda *args, **kwargs: None)
            transaction = payments.record_failed_transaction(
                db_session,
                booking=booking,
                reason="Declined",
            )

        assert transaction.status == models.TransactionStatus.FAILED
        assert released["called"] is True

    def test_get_successful_or_processing_response_and_confirm_success_reuse_existing_success(self, db_session):
        room = make_room(db_session)
        booking = make_booking(
            db_session,
            room_id=room.id,
            email="success-txn@example.com",
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )
        success_txn = models.Transaction(
            booking_id=booking.id,
            transaction_ref="TXN-SUCCESS-EDGE",
            amount=booking.total_amount,
            currency="USD",
            payment_method="card",
            status=models.TransactionStatus.SUCCESS,
        )
        db_session.add(success_txn)
        db_session.commit()
        db_session.refresh(success_txn)

        existing = payments.get_successful_or_processing_response(
            db_session,
            booking=booking,
            transaction_ref=success_txn.transaction_ref,
            payment_method="card",
        )
        assert existing.id == success_txn.id

        confirmed = payments.confirm_payment_success(
            schemas.PaymentSuccess(
                booking_id=booking.id,
                transaction_ref=success_txn.transaction_ref,
                payment_method="card",
            ),
            db=db_session,
        )
        assert confirmed.id == success_txn.id

    def test_confirm_payment_success_falls_through_to_processing_when_paid_but_missing_success_txn(self, db_session):
        room = make_room(db_session)
        booking = make_booking(
            db_session,
            room_id=room.id,
            email="paid-no-success@example.com",
            payment_status=models.PaymentStatus.PAID,
            status=models.BookingStatus.CONFIRMED,
        )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(payments, "ensure_booking_can_accept_payment", lambda db, current: current)
            mp.setattr(
                payments,
                "get_successful_or_processing_response",
                lambda **kwargs: SimpleNamespace(id=999, transaction_ref=kwargs["transaction_ref"]),
            )
            result = payments.confirm_payment_success(
                schemas.PaymentSuccess(
                    booking_id=booking.id,
                    transaction_ref="TXN-MISSING-SUCCESS",
                    payment_method="card",
                ),
                db=db_session,
            )

        assert result.id == 999

    def test_record_payment_failure_marks_expired_booking_when_hold_becomes_stale(self, db_session):
        room = make_room(db_session)
        booking = make_booking(
            db_session,
            room_id=room.id,
            email="stale-failure@example.com",
            hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        request = MagicMock()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(payments, "enforce_rate_limit", lambda *args, **kwargs: None)
            mp.setattr(payments, "release_expired_holds", lambda db, booking_id=None: None)
            mp.setattr(payments, "expire_stale_booking_hold", lambda current_booking: True)
            with pytest.raises(HTTPException, match="cannot be updated"):
                payments.record_payment_failure(
                    request=request,
                    booking_id=booking.id,
                    db=db_session,
                )

        db_session.refresh(booking)
        assert booking.status == models.BookingStatus.EXPIRED
        assert booking.payment_status == models.PaymentStatus.EXPIRED

    def test_create_payment_intent_surfaces_stripe_errors(self, client, create_booking):
        booking = create_booking()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                payments.stripe.PaymentIntent,
                "create",
                lambda **kwargs: (_ for _ in ()).throw(Exception("stripe boom")),
            )
            response = client.post(
                "/payments/create-payment-intent",
                json={
                    "booking_id": booking["id"],
                    "payment_method": "card",
                    "idempotency_key": "stripe-fail-001",
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "stripe boom"


class TestBookingAndRoomCoverageEdges:
    def test_bookings_cover_past_check_in_and_history_without_user(self, client, db_session):
        room = make_room(db_session, availability=True)
        db_session.add(
            models.Booking(
                booking_ref="BK-HISTORY-EDGE",
                user_name="History Guest",
                email="history-only@example.com",
                room_id=room.id,
                check_in=datetime.now(timezone.utc) + timedelta(days=2),
                check_out=datetime.now(timezone.utc) + timedelta(days=3),
                guests=1,
                nights=1,
                room_rate=200.0,
                taxes=24.0,
                service_fee=10.0,
                total_amount=234.0,
                status=models.BookingStatus.CONFIRMED,
                payment_status=models.PaymentStatus.PAID,
            )
        )
        db_session.commit()

        create_response = client.post(
            "/bookings",
            json={
                "user_name": "Past Guest",
                "email": "past@example.com",
                "room_id": room.id,
                "check_in": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                "check_out": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                "guests": 1,
            },
        )
        assert create_response.status_code == 400
        assert create_response.json()["detail"]["code"] == "CHECK_IN_PAST"

        history_response = client.get("/bookings/history", params={"email": "history-only@example.com"})
        assert history_response.status_code == 200
        assert history_response.json()["total"] == 1

    def test_booking_overlap_minimum_stay_and_lock_failure_paths(self, db_session):
        room = make_room(db_session)
        now = datetime.now(timezone.utc)

        pending_overlap = make_booking(
            db_session,
            room_id=room.id,
            email="pending-overlap@example.com",
            hold_expires_at=now + timedelta(minutes=10),
        )
        assert bookings.has_active_booking_overlap(
            db_session,
            room_id=room.id,
            check_in=pending_overlap.check_in,
            check_out=pending_overlap.check_out,
        ) is True
        assert bookings.has_active_pending_hold(
            SimpleNamespace(
                status=models.BookingStatus.PENDING,
                payment_status=models.PaymentStatus.PENDING,
                hold_expires_at=None,
            ),
            now=now,
        ) is False
        expired_pending = SimpleNamespace(
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
            hold_expires_at=now - timedelta(minutes=1),
        )
        assert bookings.has_active_pending_hold(expired_pending, now=now) is False
        inactive_pending = make_booking(
            db_session,
            room_id=room.id,
            email="inactive-pending@example.com",
            hold_expires_at=now - timedelta(minutes=5),
            check_in=now + timedelta(days=10),
            check_out=now + timedelta(days=12),
        )
        assert bookings.has_active_booking_overlap(
            db_session,
            room_id=room.id,
            check_in=inactive_pending.check_in,
            check_out=inactive_pending.check_out,
            exclude_booking_id=inactive_pending.id,
        ) is False
        second_active_pending = make_booking(
            db_session,
            room_id=room.id,
            email="second-active@example.com",
            hold_expires_at=now + timedelta(minutes=5),
            check_in=inactive_pending.check_in,
            check_out=inactive_pending.check_out,
        )
        assert bookings.has_active_booking_overlap(
            db_session,
            room_id=room.id,
            check_in=inactive_pending.check_in,
            check_out=inactive_pending.check_out,
        ) is True
        assert second_active_pending.id is not None

        with pytest.raises(HTTPException, match="Minimum stay is 1 night"):
            bookings.create_booking(
                schemas.BookingCreate(
                    user_name="Min Stay",
                    email="minstay@example.com",
                    room_id=room.id,
                    check_in=now + timedelta(days=2, hours=3),
                    check_out=now + timedelta(days=2, hours=20),
                    guests=1,
                ),
                db=db_session,
            )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bookings, "release_expired_holds", lambda *args, **kwargs: None)
            mp.setattr(bookings, "release_expired_inventory_locks", lambda *args, **kwargs: None)
            mp.setattr(bookings, "has_active_booking_overlap", lambda *args, **kwargs: True)
            with pytest.raises(HTTPException, match="no longer available"):
                bookings.create_booking(
                    schemas.BookingCreate(
                        user_name="Overlap",
                        email="overlap@example.com",
                        room_id=room.id,
                        check_in=now + timedelta(days=7),
                        check_out=now + timedelta(days=9),
                        guests=1,
                    ),
                    db=db_session,
                )

    def test_extend_hold_covers_confirmed_and_lock_error_paths(self, db_session):
        room = make_room(db_session)
        now = datetime.now(timezone.utc)
        confirmed_booking = make_booking(
            db_session,
            room_id=room.id,
            email="confirmed-extend@example.com",
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PAID,
        )

        with pytest.raises(HTTPException, match="already been paid and confirmed"):
            bookings.extend_booking_hold(
                confirmed_booking.id,
                bookings.ExtendHoldRequest(email=confirmed_booking.email),
                db=db_session,
            )

        status_only_booking = make_booking(
            db_session,
            room_id=room.id,
            email="status-only-extend@example.com",
            status=models.BookingStatus.CONFIRMED,
            payment_status=models.PaymentStatus.PENDING,
        )
        with pytest.raises(HTTPException, match="already been paid and confirmed"):
            bookings.extend_booking_hold(
                status_only_booking.id,
                bookings.ExtendHoldRequest(email=status_only_booking.email),
                db=db_session,
            )

        pending_booking = make_booking(
            db_session,
            room_id=room.id,
            email="lock-error-extend@example.com",
            status=models.BookingStatus.PENDING,
            payment_status=models.PaymentStatus.PENDING,
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bookings, "release_expired_holds", lambda *args, **kwargs: None)
            mp.setattr(bookings, "release_expired_inventory_locks", lambda *args, **kwargs: None)
            mp.setattr(bookings, "has_active_booking_overlap", lambda *args, **kwargs: False)
            mp.setattr(
                bookings,
                "lock_inventory_for_booking",
                lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("busy")),
            )
            with pytest.raises(HTTPException, match="no longer available"):
                bookings.extend_booking_hold(
                    pending_booking.id,
                    bookings.ExtendHoldRequest(email=pending_booking.email),
                    db=db_session,
                )

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(bookings, "release_expired_holds", lambda *args, **kwargs: None)
            mp.setattr(bookings, "release_expired_inventory_locks", lambda *args, **kwargs: None)
            mp.setattr(bookings, "has_active_booking_overlap", lambda *args, **kwargs: False)
            mp.setattr(
                bookings,
                "lock_inventory_for_booking",
                lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("locked")),
            )
            mp.setattr(bookings, "queue_booking_hold_email", lambda *args, **kwargs: None)
            with pytest.raises(HTTPException, match="no longer available"):
                bookings.create_booking(
                    schemas.BookingCreate(
                        user_name="Lock Failure",
                        email="lockfail@example.com",
                        room_id=room.id,
                        check_in=now + timedelta(days=11),
                        check_out=now + timedelta(days=13),
                        guests=1,
                    ),
                    db=db_session,
                )

    def test_unavailable_dates_marks_zero_inventory_without_active_lock_as_unavailable(self, db_session):
        room = make_room(db_session)
        target_date = date.today() + timedelta(days=10)
        db_session.add(
            models.RoomInventory(
                room_id=room.id,
                inventory_date=target_date,
                total_units=1,
                available_units=0,
                locked_units=0,
                status=models.InventoryStatus.BLOCKED,
            )
        )
        db_session.commit()

        response = rooms_router.get_room_unavailable_dates(
            room_id=room.id,
            from_date=target_date,
            to_date=target_date,
            db=db_session,
        )
        assert target_date.isoformat() in response.unavailable_dates

    def test_room_helpers_cover_timezone_and_hold_date_paths(self, db_session):
        booking_window = SimpleNamespace(
            check_in=datetime.now(timezone.utc),
            check_out=datetime.now(timezone.utc) + timedelta(days=2),
        )
        assert (
            rooms_router.booking_overlaps_date_window(
                booking_window,
                from_date=date.today(),
                to_date=date.today() + timedelta(days=1),
            )
            is True
        )

        room = make_room(db_session)
        held_date = date.today() + timedelta(days=15)
        confirmed_check_in = datetime.combine(held_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        confirmed_check_out = confirmed_check_in + timedelta(days=2)
        db_session.add(
            models.RoomInventory(
                room_id=room.id,
                inventory_date=held_date,
                total_units=1,
                available_units=0,
                locked_units=1,
                lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
                status=models.InventoryStatus.LOCKED,
            )
        )
        db_session.add(
            models.Booking(
                booking_ref="BK-CONFIRMED-UNAVAILABLE",
                user_name="Confirmed Guest",
                email="confirmed@example.com",
                room_id=room.id,
                check_in=confirmed_check_in,
                check_out=confirmed_check_out,
                guests=1,
                nights=2,
                room_rate=100.0,
                taxes=10.0,
                service_fee=5.0,
                total_amount=115.0,
                status=models.BookingStatus.CONFIRMED,
                payment_status=models.PaymentStatus.PAID,
            )
        )
        db_session.commit()

        response = rooms_router.get_room_unavailable_dates(
            room_id=room.id,
            from_date=held_date,
            to_date=held_date + timedelta(days=1),
            db=db_session,
        )
        assert held_date.isoformat() in response.unavailable_dates
        assert held_date.isoformat() not in response.held_dates

    def test_unavailable_dates_cover_expired_hold_fallback_and_out_of_window_dates(self, db_session):
        room = make_room(db_session)
        target_date = date.today() + timedelta(days=20)
        db_session.add(
            models.RoomInventory(
                room_id=room.id,
                inventory_date=target_date,
                total_units=1,
                available_units=0,
                locked_units=1,
                lock_expires_at=None,
                status=models.InventoryStatus.LOCKED,
            )
        )
        db_session.add(
            models.RoomInventory(
                room_id=room.id,
                inventory_date=target_date + timedelta(days=1),
                total_units=1,
                available_units=0,
                locked_units=1,
                lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
                status=models.InventoryStatus.LOCKED,
            )
        )
        db_session.add(
            models.Booking(
                booking_ref="BK-OUTSIDE-WINDOW",
                user_name="Window Guest",
                email="window@example.com",
                room_id=room.id,
                check_in=datetime.combine(target_date - timedelta(days=1), datetime.min.time()).replace(tzinfo=timezone.utc),
                check_out=datetime.combine(target_date + timedelta(days=2), datetime.min.time()).replace(tzinfo=timezone.utc),
                guests=1,
                nights=3,
                room_rate=100.0,
                taxes=10.0,
                service_fee=5.0,
                total_amount=115.0,
                status=models.BookingStatus.CONFIRMED,
                payment_status=models.PaymentStatus.PAID,
            )
        )
        db_session.commit()

        response = rooms_router.get_room_unavailable_dates(
            room_id=room.id,
            from_date=target_date,
            to_date=target_date,
            db=db_session,
        )
        assert target_date.isoformat() in response.unavailable_dates

    def test_unavailable_dates_cover_aware_and_expired_hold_branches_without_cleanup(self, db_session):
        room = make_room(db_session)
        aware_date = date.today() + timedelta(days=25)
        expired_date = aware_date + timedelta(days=1)
        db_session.add_all(
            [
                models.RoomInventory(
                    room_id=room.id,
                    inventory_date=aware_date,
                    total_units=1,
                    available_units=0,
                    locked_units=1,
                    lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                    status=models.InventoryStatus.LOCKED,
                ),
                models.RoomInventory(
                    room_id=room.id,
                    inventory_date=expired_date,
                    total_units=1,
                    available_units=0,
                    locked_units=1,
                    lock_expires_at=datetime.now(timezone.utc) - timedelta(minutes=10),
                    status=models.InventoryStatus.LOCKED,
                ),
            ]
        )
        db_session.commit()

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(rooms_router, "release_expired_inventory_locks", lambda *args, **kwargs: None)
            response = rooms_router.get_room_unavailable_dates(
                room_id=room.id,
                from_date=aware_date,
                to_date=expired_date,
                db=db_session,
            )

        assert aware_date.isoformat() in response.held_dates
        assert expired_date.isoformat() not in response.held_dates

    def test_unavailable_dates_directly_covers_aware_lock_branch(self):
        aware_date = date.today() + timedelta(days=30)
        room = SimpleNamespace(id=77)
        aware_row = SimpleNamespace(
            inventory_date=aware_date,
            available_units=0,
            locked_units=1,
            lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )

        class FakeQuery:
            def __init__(self, result):
                self.result = result

            def filter(self, *args, **kwargs):
                return self

            def first(self):
                return self.result

            def all(self):
                return self.result

        class FakeDB:
            def __init__(self):
                self.calls = 0

            def query(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    return FakeQuery(room)
                if self.calls == 2:
                    return FakeQuery([aware_row])
                return FakeQuery([])

        fake_db = FakeDB()
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(rooms_router, "release_expired_inventory_locks", lambda *args, **kwargs: None)
            response = rooms_router.get_room_unavailable_dates(
                room_id=room.id,
                from_date=aware_date,
                to_date=aware_date,
                db=fake_db,
            )

        assert response.held_dates == [aware_date.isoformat()]


class TestInventoryCoverageEdges:
    def test_inventory_helpers_cover_sqlite_and_missing_lock_branches(self, db_session):
        room = make_room(db_session)
        booking = make_booking(
            db_session,
            room_id=room.id,
            email="inventory-edge@example.com",
            hold_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        with inventory_service.inventory_lock_scope(db_session, room.id):
            assert True

        row = models.RoomInventory(
            room_id=room.id,
            inventory_date=booking.check_in.date(),
            total_units=1,
            available_units=1,
            locked_units=1,
            locked_by_booking_id=booking.id,
            lock_expires_at=None,
            status=models.InventoryStatus.LOCKED,
        )
        db_session.add(row)
        db_session.commit()

        assert inventory_service.release_expired_inventory_locks(db_session) == 0
        assert inventory_service.is_booking_inventory_locked(db_session, booking=booking) is False

    def test_inventory_helpers_cover_non_sqlite_scope_and_none_expiry_row(self):
        class FakeQuery:
            def filter(self, *args, **kwargs):
                return self

            def all(self):
                return [SimpleNamespace(lock_expires_at=None, locked_units=1)]

        class FakeDB:
            def __init__(self):
                self.bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
                self.committed = False

            def query(self, *_args, **_kwargs):
                return FakeQuery()

            def commit(self):
                self.committed = True

        fake_db = FakeDB()
        with inventory_service.inventory_lock_scope(fake_db, room_id=77):
            assert True

        assert inventory_service.inventory_lock_scope is not None
