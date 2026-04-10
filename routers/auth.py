import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
import httpx as _httpx
from jose import JWTError, jwt
from passlib.context import CryptContext
from passlib.exc import PasswordValueError, UnknownHashError
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

import models
import schemas
from database import get_db, settings
from services.audit_service import write_audit_log
from services.otp_service import (
    build_challenge_response,
    issue_otp_challenge,
    normalize_recipient,
    verify_otp_challenge,
)
from services.payment_state_service import attach_bookings_lifecycle_state
from services.rate_limit_service import enforce_rate_limit

__all__ = ["router", "MyBookingsResponse", "utc_now", "ensure_aware_utc"]


class MyBookingsResponse(BaseModel):
    total: int
    upcoming: int
    past: int
    cancelled: int
    expired: int
    page: int = 1
    per_page: int = 5
    total_pages: int = 1
    tab: str = "upcoming"
    bookings: list = []

router = APIRouter(prefix="/auth", tags=["Auth"])

# Keep pbkdf2_sha256 as the preferred scheme for newly created passwords,
# while still accepting older/manual bcrypt hashes already stored in production.
pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")
ALGORITHM = "HS256"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone(phone: str) -> str:
    return " ".join(phone.strip().split())


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except (UnknownHashError, PasswordValueError, ValueError):
        if hashed_password.startswith(("$2a$", "$2b$", "$2y$")):
            try:
                return bcrypt.checkpw(
                    plain_password.encode("utf-8"),
                    hashed_password.encode("utf-8"),
                )
            except ValueError:
                return False
        return False


def create_token(user: models.User, token_type: str, expires_delta: timedelta) -> str:
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "is_admin": user.is_admin,
        "is_partner": user.is_partner,
        "token_type": token_type,
        "jti": str(uuid.uuid4()),
        "exp": utc_now() + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def build_token_response(user: models.User, db: Session | None = None) -> schemas.TokenResponse:
    access_token = create_token(
        user, "access", timedelta(minutes=settings.access_token_exp_minutes)
    )
    refresh_token = create_token(
        user, "refresh", timedelta(days=settings.refresh_token_exp_days)
    )

    # Persist refresh token for rotation / revocation
    if db is not None:
        db.add(models.RefreshToken(
            user_id=user.id,
            token_hash=_hash_token(refresh_token),
            family_id=str(uuid.uuid4()),
            expires_at=utc_now() + timedelta(days=settings.refresh_token_exp_days),
        ))
        db.flush()

    return schemas.TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user,
    )


REFRESH_COOKIE_NAME = "sv_refresh_token"


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    """Set the refresh token as an HttpOnly cookie on the response."""
    is_prod = settings.app_env.lower() == "production"
    # Production: API (Railway) and frontend (stayvora.co.in) are on different
    # domains, so we need SameSite=None + Secure=True to allow cross-site cookies.
    # Local dev: frontend and API both on localhost → SameSite=Lax is fine.
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=refresh_token,
        httponly=True,
        secure=is_prod,
        samesite="none" if is_prod else "lax",
        domain=settings.cookie_domain or None,  # e.g. ".stayvora.co.in" for cross-subdomain
        path="/auth",  # only sent to /auth/* endpoints (refresh, logout)
        max_age=settings.refresh_token_exp_days * 86400,
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Clear the refresh token cookie."""
    is_prod = settings.app_env.lower() == "production"
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        httponly=True,
        secure=is_prod,
        samesite="none" if is_prod else "lax",
        domain=settings.cookie_domain or None,
        path="/auth",
    )


def hash_phone_otp(phone: str, otp: str) -> str:
    raw = f"{normalize_phone(phone)}:{otp}:{settings.secret_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def clear_phone_otp(user: models.User) -> None:
    user.pending_phone = None
    user.phone_otp_hash = None
    user.phone_otp_expires_at = None
    user.phone_otp_attempts = 0


def get_verified_challenge(
    db: Session,
    *,
    challenge_id: str,
    flow: schemas.OtpFlow,
    channel: schemas.OtpChannel,
    recipient: str,
    user_id: int | None,
) -> models.OtpChallenge:
    challenge = db.query(models.OtpChallenge).filter(
        models.OtpChallenge.id == challenge_id,
        models.OtpChallenge.flow == flow.value,
        models.OtpChallenge.channel == channel.value,
    ).first()
    if not challenge:
        raise HTTPException(status_code=400, detail="Verification challenge is invalid.")
    if user_id is None:
        if challenge.user_id is not None:
            raise HTTPException(status_code=400, detail="Verification challenge is invalid.")
    elif challenge.user_id != user_id:
        raise HTTPException(status_code=400, detail="Verification challenge is invalid.")
    if challenge.recipient != recipient:
        raise HTTPException(status_code=400, detail="Verification challenge does not match the submitted contact.")
    if not challenge.verified_at:
        raise HTTPException(status_code=400, detail="Verification challenge has not been completed.")
    if challenge.consumed_at:
        raise HTTPException(status_code=400, detail="Verification challenge has already been used.")
    return challenge


def consume_challenge(challenge: models.OtpChallenge) -> None:
    challenge.consumed_at = utc_now()


def revoke_all_refresh_tokens(db: Session, user_id: int) -> None:
    db.query(models.RefreshToken).filter(
        models.RefreshToken.user_id == user_id,
        models.RefreshToken.revoked.is_(False),
    ).update({"revoked": True})


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc

    if payload.get("token_type") != expected_type:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Expected a {expected_type} token",
        )
    return payload


def get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization header is required",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header",
        )
    return token


def get_user_from_payload(db: Session, payload: dict) -> models.User:
    user_id = payload.get("sub")
    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User is not available",
        )
    return user


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> models.User:
    token = get_bearer_token(authorization)
    payload = decode_token(token, "access")
    return get_user_from_payload(db, payload)


def get_optional_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> models.User | None:
    if not authorization:
        return None
    token = get_bearer_token(authorization)
    payload = decode_token(token, "access")
    return get_user_from_payload(db, payload)


def get_current_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


def get_current_partner(user: models.User = Depends(get_current_user)) -> models.User:
    if not user.is_partner:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Partner access required",
        )
    return user


def _request_meta(request: Request) -> dict:
    """Extract IP and User-Agent for audit logs."""
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    return {"ip": ip or "unknown", "user_agent": request.headers.get("user-agent", "")}


@router.post("/signup", response_model=schemas.TokenResponse, status_code=201)
def signup(payload: schemas.UserSignup, request: Request, response: Response, db: Session = Depends(get_db)):
    normalized_email = normalize_email(payload.email)
    normalized_phone = normalize_phone(payload.phone)
    enforce_rate_limit("auth:signup", request, subject=normalized_email)
    existing_user = db.query(models.User).filter(models.User.email == normalized_email).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="Email already registered")
    existing_phone = db.query(models.User).filter(models.User.phone == normalized_phone).first()
    if existing_phone:
        raise HTTPException(status_code=409, detail="Phone number already registered")

    email_challenge = get_verified_challenge(
        db,
        challenge_id=payload.email_challenge_id,
        flow=schemas.OtpFlow.SIGNUP,
        channel=schemas.OtpChannel.EMAIL,
        recipient=normalized_email,
        user_id=None,
    )
    phone_challenge = get_verified_challenge(
        db,
        challenge_id=payload.phone_challenge_id,
        flow=schemas.OtpFlow.SIGNUP,
        channel=schemas.OtpChannel.PHONE,
        recipient=normalized_phone,
        user_id=None,
    )

    user = models.User(
        email=normalized_email,
        phone=normalized_phone,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        phone_verified=True,
        is_email_verified=True,
        is_admin=False,
        is_partner=False,
        is_active=True,
    )
    db.add(user)
    consume_challenge(email_challenge)
    consume_challenge(phone_challenge)
    db.commit()
    db.refresh(user)
    resp = build_token_response(user, db)
    _set_refresh_cookie(response, resp.refresh_token)
    write_audit_log(db, user.id, "auth.signup", "user", user.id, _request_meta(request))
    db.commit()
    return resp


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.UserLogin, request: Request, response: Response, db: Session = Depends(get_db)):
    normalized_email = normalize_email(payload.email)
    enforce_rate_limit("auth:login", request, subject=normalized_email)
    user = db.query(models.User).filter(models.User.email == normalized_email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        write_audit_log(db, None, "auth.login.failed", "user", normalized_email, {**_request_meta(request), "reason": "invalid_credentials"})
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        write_audit_log(db, user.id, "auth.login.failed", "user", user.id, {**_request_meta(request), "reason": "inactive"})
        db.commit()
        raise HTTPException(status_code=403, detail="User account is inactive")
    resp = build_token_response(user, db)
    _set_refresh_cookie(response, resp.refresh_token)
    write_audit_log(db, user.id, "auth.login.success", "user", user.id, _request_meta(request))
    db.commit()
    return resp


@router.post("/refresh", response_model=schemas.TokenResponse)
def refresh_token(
    response: Response,
    payload: schemas.RefreshTokenRequest | None = None,
    sv_refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    # Read refresh token from HttpOnly cookie first, fall back to request body
    raw_refresh = sv_refresh_token or (payload.refresh_token if payload else None)
    if not raw_refresh:
        raise HTTPException(status_code=401, detail="Refresh token is required")

    token_payload = decode_token(raw_refresh, "refresh")
    user = get_user_from_payload(db, token_payload)

    # Validate refresh token exists and is not revoked
    incoming_hash = _hash_token(raw_refresh)
    stored = db.query(models.RefreshToken).filter(
        models.RefreshToken.token_hash == incoming_hash
    ).first()

    if stored and stored.revoked:
        # Reuse of a revoked token → token theft detected → revoke entire family
        db.query(models.RefreshToken).filter(
            models.RefreshToken.family_id == stored.family_id
        ).update({"revoked": True})
        write_audit_log(db, user.id, "auth.refresh.reuse_detected", "user", user.id, {"family_id": stored.family_id})
        db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Token reuse detected. All sessions revoked.")

    # Issue new tokens, inheriting the family_id
    family_id = stored.family_id if stored else str(uuid.uuid4())
    if stored:
        stored.revoked = True  # revoke the old refresh token

    access_token = create_token(
        user, "access", timedelta(minutes=settings.access_token_exp_minutes)
    )
    new_refresh_token = create_token(
        user, "refresh", timedelta(days=settings.refresh_token_exp_days)
    )
    db.add(models.RefreshToken(
        user_id=user.id,
        token_hash=_hash_token(new_refresh_token),
        family_id=family_id,
        expires_at=utc_now() + timedelta(days=settings.refresh_token_exp_days),
    ))
    db.commit()
    _set_refresh_cookie(response, new_refresh_token)
    return schemas.TokenResponse(
        access_token=access_token,
        refresh_token=new_refresh_token,
        user=user,
    )


@router.post("/logout", response_model=schemas.MessageResponse)
def logout(
    response: Response,
    payload: schemas.LogoutRequest | None = None,
    sv_refresh_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
):
    raw_refresh = sv_refresh_token or (payload.refresh_token if payload else None)
    if raw_refresh:
        incoming_hash = _hash_token(raw_refresh)
        stored = db.query(models.RefreshToken).filter(
            models.RefreshToken.token_hash == incoming_hash
        ).first()
        if stored:
            # Revoke entire token family for this session
            db.query(models.RefreshToken).filter(
                models.RefreshToken.family_id == stored.family_id
            ).update({"revoked": True})
            db.commit()
    _clear_refresh_cookie(response)
    return schemas.MessageResponse(message="Logged out successfully")


@router.get("/me", response_model=schemas.UserResponse)
def read_me(user: models.User = Depends(get_current_user)):
    return user


@router.get("/me/bookings")
def my_bookings(
    user: models.User = Depends(get_current_user),
    tab: str = "upcoming",
    page: int = 1,
    per_page: int = 5,
    db: Session = Depends(get_db),
):
    from datetime import timezone as _tz

    def _aware(dt: datetime) -> datetime:
        return dt.replace(tzinfo=_tz.utc) if dt.tzinfo is None else dt

    def _is_upcoming(booking: models.Booking) -> bool:
        return booking.status in (
            models.BookingStatus.CONFIRMED,
            models.BookingStatus.PROCESSING,
            models.BookingStatus.PENDING,
        ) and _aware(booking.check_in) >= now

    def _is_past(booking: models.Booking) -> bool:
        return booking.status == models.BookingStatus.COMPLETED or (
            booking.status
            not in (models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED)
            and _aware(booking.check_in) < now
        )

    def _is_cancelled(booking: models.Booking) -> bool:
        return booking.status == models.BookingStatus.CANCELLED

    def _is_expired(booking: models.Booking) -> bool:
        return booking.status == models.BookingStatus.EXPIRED

    now = datetime.now(_tz.utc)
    normalized_tab = tab.lower().strip()
    if normalized_tab not in {"upcoming", "past", "cancelled", "expired"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported bookings tab",
        )
    if page < 1:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Page must be >= 1")
    if per_page < 1 or per_page > 50:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="per_page must be between 1 and 50")

    bookings = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(
            or_(
                models.Booking.email == user.email,
                models.Booking.user_id == user.id,
            )
        )
        .order_by(models.Booking.created_at.desc())
        .all()
    )
    attach_bookings_lifecycle_state(db, bookings)
    upcoming = sum(1 for b in bookings if _is_upcoming(b))
    past = sum(1 for b in bookings if _is_past(b))
    cancelled = sum(1 for b in bookings if _is_cancelled(b))
    expired = sum(1 for b in bookings if _is_expired(b))

    if normalized_tab == "upcoming":
        filtered_bookings = [b for b in bookings if _is_upcoming(b)]
    elif normalized_tab == "past":
        filtered_bookings = [b for b in bookings if _is_past(b)]
    elif normalized_tab == "cancelled":
        filtered_bookings = [b for b in bookings if _is_cancelled(b)]
    else:
        filtered_bookings = [b for b in bookings if _is_expired(b)]

    total = len(filtered_bookings)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)
    start = (page - 1) * per_page
    paginated_bookings = filtered_bookings[start : start + per_page]

    return MyBookingsResponse(
        total=total,
        upcoming=upcoming,
        past=past,
        cancelled=cancelled,
        expired=expired,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        tab=normalized_tab,
        bookings=[schemas.BookingResponse.model_validate(b) for b in paginated_bookings],
    )


# ─── Profile ──────────────────────────────────────────────────────────────────

@router.post("/otp/request", response_model=schemas.OtpChallengeResponse)
def request_otp(
    payload: schemas.OtpChallengeStartRequest,
    request: Request,
    current_user: models.User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    user_id = current_user.id if current_user and payload.flow == schemas.OtpFlow.PROFILE else None
    recipient = normalize_recipient(payload.channel, payload.recipient)

    if payload.flow == schemas.OtpFlow.PROFILE:
        if not current_user:
            raise HTTPException(status_code=401, detail="Authentication required for profile verification.")
        if payload.channel == schemas.OtpChannel.EMAIL:
            if normalize_email(current_user.email) == recipient and current_user.is_email_verified:
                raise HTTPException(status_code=400, detail="This email address is already verified.")
            conflict = db.query(models.User).filter(
                models.User.id != current_user.id,
                models.User.email == recipient,
            ).first()
            if conflict:
                raise HTTPException(status_code=409, detail="Email already registered")
        else:
            if normalize_phone(current_user.phone or "") == recipient and current_user.phone_verified:
                raise HTTPException(status_code=400, detail="This phone number is already verified.")
            conflict = db.query(models.User).filter(
                models.User.id != current_user.id,
                models.User.phone == recipient,
            ).first()
            if conflict:
                raise HTTPException(status_code=409, detail="Phone number already registered")
    elif payload.flow == schemas.OtpFlow.SIGNUP:
        if payload.channel == schemas.OtpChannel.EMAIL:
            if db.query(models.User).filter(models.User.email == recipient).first():
                raise HTTPException(status_code=409, detail="Email already registered")
        else:
            if db.query(models.User).filter(models.User.phone == recipient).first():
                raise HTTPException(status_code=409, detail="Phone number already registered")
    elif payload.flow == schemas.OtpFlow.PASSWORD_RESET:
        if payload.channel == schemas.OtpChannel.EMAIL:
            user = db.query(models.User).filter(models.User.email == recipient, models.User.is_active.is_(True)).first()
        else:
            user = db.query(models.User).filter(models.User.phone == recipient, models.User.phone_verified.is_(True), models.User.is_active.is_(True)).first()
        if not user:
            generic_challenge = models.OtpChallenge(
                id=str(uuid.uuid4()),
                user_id=None,
                flow=payload.flow.value,
                channel=payload.channel.value,
                recipient=recipient,
                otp_hash="masked",
                attempts=0,
                max_attempts=5,
                resend_count=0,
                max_resends=3,
                resend_available_at=utc_now() + timedelta(seconds=30),
                expires_at=utc_now() + timedelta(minutes=5),
                device_fingerprint=payload.device_fingerprint,
            )
            return build_challenge_response(generic_challenge, message="If the account exists, an OTP has been sent.")
        user_id = user.id

    challenge, dev_code = issue_otp_challenge(
        db,
        request,
        flow=payload.flow,
        channel=payload.channel,
        recipient=recipient,
        secret_key=settings.secret_key,
        user_id=user_id,
        device_fingerprint=payload.device_fingerprint,
    )
    db.commit()
    return build_challenge_response(
        challenge,
        message="OTP sent successfully.",
        dev_code=dev_code if settings.app_env.lower() != "production" else None,
    )


@router.post("/otp/verify", response_model=schemas.OtpChallengeVerifyResponse)
def verify_otp(
    payload: schemas.OtpChallengeVerifyRequest,
    request: Request,
    current_user: models.User | None = Depends(get_optional_current_user),
    db: Session = Depends(get_db),
):
    challenge = verify_otp_challenge(
        db,
        request,
        challenge_id=payload.challenge_id,
        otp=payload.otp,
        secret_key=settings.secret_key,
        user_id=current_user.id if current_user else None,
    )
    reset_token = None
    if challenge.flow == schemas.OtpFlow.PASSWORD_RESET.value and challenge.user_id:
        reset_token = jwt.encode(
            {
                "sub": str(challenge.user_id),
                "challenge_id": challenge.id,
                "token_type": "password_reset_session",
                "exp": utc_now() + timedelta(minutes=15),
            },
            settings.secret_key,
            algorithm=ALGORITHM,
        )
    db.commit()
    return schemas.OtpChallengeVerifyResponse(
        message="OTP verified successfully.",
        challenge_id=challenge.id,
        flow=schemas.OtpFlow(challenge.flow),
        channel=schemas.OtpChannel(challenge.channel),
        recipient=challenge.recipient,
        reset_token=reset_token,
    )


@router.put("/me", response_model=schemas.UserDetailResponse)
def update_profile(
    payload: schemas.UserProfileUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.email is not None:
        email = normalize_email(payload.email)
        if email != normalize_email(user.email):
            if db.query(models.User).filter(models.User.id != user.id, models.User.email == email).first():
                raise HTTPException(status_code=409, detail="Email already registered")
            if not payload.email_challenge_id:
                raise HTTPException(status_code=400, detail="Verify this email address with OTP first")
            email_challenge = get_verified_challenge(
                db,
                challenge_id=payload.email_challenge_id,
                flow=schemas.OtpFlow.PROFILE,
                channel=schemas.OtpChannel.EMAIL,
                recipient=email,
                user_id=user.id,
            )
            user.email = email
            user.is_email_verified = True
            consume_challenge(email_challenge)
    if payload.phone is not None:
        phone = normalize_phone(payload.phone)
        if not phone:
            raise HTTPException(status_code=400, detail="Phone number is required")
        if phone != normalize_phone(user.phone or ""):
            if db.query(models.User).filter(models.User.id != user.id, models.User.phone == phone).first():
                raise HTTPException(status_code=409, detail="Phone number already registered")
            if not payload.phone_challenge_id:
                raise HTTPException(status_code=400, detail="Verify this phone number with OTP first")
            phone_challenge = get_verified_challenge(
                db,
                challenge_id=payload.phone_challenge_id,
                flow=schemas.OtpFlow.PROFILE,
                channel=schemas.OtpChannel.PHONE,
                recipient=phone,
                user_id=user.id,
            )
            consume_challenge(phone_challenge)
            user.phone_verified = True
        user.phone = phone
    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url
    db.commit()
    db.refresh(user)
    return user


@router.post("/change-password", response_model=schemas.MessageResponse)
def change_password(
    payload: schemas.ChangePasswordRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.hashed_password or not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return schemas.MessageResponse(message="Password changed successfully")


# ─── Forgot / Reset Password ───────────────────────────────────────────────────

def _hash_reset_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


@router.post("/forgot-password", response_model=schemas.OtpChallengeResponse)
def forgot_password(
    payload: schemas.ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    recipient = normalize_recipient(payload.channel, payload.recipient)
    enforce_rate_limit("auth:forgot-password", request, subject=f"{payload.channel.value}:{recipient}")
    if payload.channel == schemas.OtpChannel.EMAIL:
        user = db.query(models.User).filter(models.User.email == recipient, models.User.is_active.is_(True)).first()
    else:
        user = db.query(models.User).filter(
            models.User.phone == recipient,
            models.User.phone_verified.is_(True),
            models.User.is_active.is_(True),
        ).first()
    if not user:
        generic = models.OtpChallenge(
            id=str(uuid.uuid4()),
            user_id=None,
            flow=schemas.OtpFlow.PASSWORD_RESET.value,
            channel=payload.channel.value,
            recipient=recipient,
            otp_hash="masked",
            attempts=0,
            max_attempts=5,
            resend_count=0,
            max_resends=3,
            resend_available_at=utc_now() + timedelta(seconds=30),
            expires_at=utc_now() + timedelta(minutes=5),
            device_fingerprint=payload.device_fingerprint,
        )
        return build_challenge_response(generic, message="If the account exists, an OTP has been sent.")

    challenge, dev_code = issue_otp_challenge(
        db,
        request,
        flow=schemas.OtpFlow.PASSWORD_RESET,
        channel=payload.channel,
        recipient=recipient,
        secret_key=settings.secret_key,
        user_id=user.id,
        device_fingerprint=payload.device_fingerprint,
    )
    db.commit()
    return build_challenge_response(
        challenge,
        message="If the account exists, an OTP has been sent.",
        dev_code=dev_code if settings.app_env.lower() != "production" else None,
    )
    enforce_rate_limit("auth:forgot-password", request, subject=payload.email.lower())
    user = db.query(models.User).filter(models.User.email == normalize_email(payload.email)).first()
    # Always return 200 to prevent email enumeration
    if user and user.is_active:
        raw_token = secrets.token_urlsafe(32)
        token_hash = _hash_reset_token(raw_token)
        expires_at = utc_now() + timedelta(hours=1)
        # Invalidate previous tokens for this user
        db.query(models.PasswordResetToken).filter(
            models.PasswordResetToken.user_id == user.id,
            models.PasswordResetToken.used_at == None,  # noqa: E711
        ).delete()
        reset_token = models.PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
        db.add(reset_token)
        db.commit()
        # In production: send email with reset link containing raw_token
        # For now the raw token is returned in the response body for dev testing
        import logging
        logging.getLogger(__name__).info(
            "Password reset requested for %s — token (dev only): %s", user.email, raw_token
        )
    return schemas.MessageResponse(
        message="If that email exists, a reset link has been sent."
    )


@router.post("/reset-password", response_model=schemas.MessageResponse)
def reset_password(
    payload: schemas.ResetPasswordRequest,
    response: Response,
    db: Session = Depends(get_db),
):
    session_payload = decode_token(payload.reset_token, "password_reset_session")
    challenge_id = session_payload.get("challenge_id")
    user_id = int(session_payload.get("sub"))
    challenge = db.query(models.OtpChallenge).filter(
        models.OtpChallenge.id == challenge_id,
        models.OtpChallenge.user_id == user_id,
        models.OtpChallenge.flow == schemas.OtpFlow.PASSWORD_RESET.value,
        models.OtpChallenge.verified_at.is_not(None),
        models.OtpChallenge.consumed_at.is_(None),
    ).first()
    if not challenge:
        raise HTTPException(status_code=400, detail="Reset session is invalid or has expired")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="User account is not available")
    user.hashed_password = hash_password(payload.new_password)
    consume_challenge(challenge)
    revoke_all_refresh_tokens(db, user.id)
    db.commit()
    _clear_refresh_cookie(response)
    return schemas.MessageResponse(message="Password has been reset successfully")
    token_hash = _hash_reset_token(payload.token)
    record = (
        db.query(models.PasswordResetToken)
        .filter(
            models.PasswordResetToken.token_hash == token_hash,
            models.PasswordResetToken.used_at == None,  # noqa: E711
            models.PasswordResetToken.expires_at > utc_now(),
        )
        .first()
    )
    if not record:
        raise HTTPException(status_code=400, detail="Reset token is invalid or has expired")
    user = db.query(models.User).filter(models.User.id == record.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=400, detail="User account is not available")
    user.hashed_password = hash_password(payload.new_password)
    record.used_at = utc_now()
    db.commit()
    return schemas.MessageResponse(message="Password has been reset successfully")


# ─── Social Login ─────────────────────────────────────────────────────────────

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
MICROSOFT_JWKS_URL = "https://login.microsoftonline.com/common/discovery/v2.0/keys"


async def _verify_google_token(id_token: str) -> dict:
    async with _httpx.AsyncClient(timeout=10) as client:
        # Validate audience (client_id) via tokeninfo to prevent cross-app token reuse
        if settings.google_client_id:
            try:
                ti_resp = await client.get(
                    GOOGLE_TOKENINFO_URL,
                    params={"access_token": id_token},
                )
            except _httpx.RequestError as exc:
                raise HTTPException(status_code=502, detail="Failed to verify Google token") from exc
            if ti_resp.status_code != 200:
                raise HTTPException(status_code=401, detail="Google token verification failed")
            ti_data = ti_resp.json()
            if ti_data.get("aud") != settings.google_client_id:
                raise HTTPException(status_code=401, detail="Google token audience mismatch")
            token_exp = ti_data.get("expires_in", "0")
            if int(token_exp) <= 0:
                raise HTTPException(status_code=401, detail="Google token has expired")

        # Fetch user profile data
        try:
            resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {id_token}"},
            )
        except _httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Failed to verify Google token") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Google token verification failed")
    data = resp.json()
    if not data.get("email"):
        raise HTTPException(status_code=401, detail="Insufficient data from Google")
    return {
        "provider_id": data.get("sub", ""),
        "email": data.get("email", ""),
        "full_name": data.get("name", data.get("email", "").split("@")[0]),
        "avatar_url": data.get("picture"),
        "email_verified": data.get("email_verified", False),
    }


async def _verify_jwks_token(id_token: str, jwks_url: str, audience: str | None) -> dict:
    """Verify a JWT signed by a JWKS-backed provider (Apple, Microsoft)."""
    try:
        from jose import jwt as jose_jwt
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="jose not installed") from exc
    async with _httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(jwks_url)
        except _httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Failed to fetch provider JWKS") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Provider JWKS endpoint unavailable")
    jwks = resp.json()
    try:
        options = {"verify_aud": audience is not None}
        claims = jose_jwt.decode(
            id_token,
            jwks,
            algorithms=["RS256"],
            audience=audience,
            options=options,
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail=f"Token verification failed: {exc}") from exc
    email = claims.get("email", "")
    sub = claims.get("sub", "")
    if not sub:
        raise HTTPException(status_code=401, detail="Token missing subject claim")
    name_parts = [claims.get("given_name", ""), claims.get("family_name", "")]
    full_name = " ".join(p for p in name_parts if p) or claims.get("name") or email.split("@")[0]
    return {
        "provider_id": sub,
        "email": email,
        "full_name": full_name,
        "avatar_url": claims.get("picture"),
        "email_verified": claims.get("email_verified", True),  # Apple always verified
    }


@router.post("/social-login", response_model=schemas.TokenResponse)
async def social_login(
    payload: schemas.SocialLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    enforce_rate_limit("auth:social-login", request)
    if payload.provider not in ("google", "apple", "microsoft"):
        raise HTTPException(status_code=400, detail="Unsupported provider. Use: google, apple, microsoft")

    if payload.provider == "google":
        provider_data = await _verify_google_token(payload.id_token)
        id_field = "google_id"
    elif payload.provider == "apple":
        provider_data = await _verify_jwks_token(
            payload.id_token,
            APPLE_JWKS_URL,
            audience=settings.apple_client_id or None,
        )
        id_field = "apple_id"
    else:  # microsoft
        provider_data = await _verify_jwks_token(
            payload.id_token,
            MICROSOFT_JWKS_URL,
            audience=settings.microsoft_client_id or None,
        )
        id_field = "microsoft_id"

    provider_id: str = provider_data["provider_id"]
    email: str = provider_data["email"]
    full_name: str = provider_data["full_name"]
    avatar_url: str | None = provider_data["avatar_url"]
    email_verified: bool = provider_data["email_verified"]

    if not email:
        raise HTTPException(status_code=401, detail="Provider did not return an email address")

    if not email_verified:
        raise HTTPException(status_code=401, detail="Email not verified with provider")

    # Look up user by provider ID first, then by email
    user = db.query(models.User).filter(
        getattr(models.User, id_field) == provider_id
    ).first()
    if not user:
        user = db.query(models.User).filter(
            models.User.email == normalize_email(email)
        ).first()

    if user:
        # Update provider ID and avatar if missing
        if not getattr(user, id_field):
            setattr(user, id_field, provider_id)
        if avatar_url and not user.avatar_url:
            user.avatar_url = avatar_url
        if email_verified and not user.is_email_verified:
            user.is_email_verified = True
        if not user.is_active:
            raise HTTPException(status_code=403, detail="Account is deactivated")
        db.commit()
    else:
        user = models.User(
            email=normalize_email(email),
            full_name=full_name,
            avatar_url=avatar_url,
            is_email_verified=email_verified,
            is_active=True,
        )
        setattr(user, id_field, provider_id)
        db.add(user)
        db.commit()
        db.refresh(user)

    db.commit()
    db.refresh(user)
    resp = build_token_response(user, db)
    _set_refresh_cookie(response, resp.refresh_token)
    write_audit_log(db, user.id, "auth.social_login.success", "user", user.id, {**_request_meta(request), "provider": payload.provider})
    db.commit()
    return resp


# ─── Email Verification ───────────────────────────────────────────────────────

@router.post("/send-verification-email", response_model=schemas.MessageResponse)
async def send_verification_email(
    current_user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.is_email_verified:
        return schemas.MessageResponse(message="Email is already verified")

    token = secrets.token_urlsafe(32)
    current_user.email_verification_token = token
    current_user.email_verification_expires_at = utc_now() + timedelta(hours=24)
    db.commit()

    from services.notification_service import enqueue_notification
    enqueue_notification(
        db,
        event_type="email_verification",
        recipient_email=current_user.email,
        subject="Verify your Stayvora email address",
        body=(
            f"Hi {current_user.full_name},\n\n"
            f"Please verify your email by clicking the link below:\n\n"
            f"https://stayvora.co.in/verify-email?token={token}\n\n"
            f"This link expires in 24 hours.\n\n"
            f"— The Stayvora Team"
        ),
    )
    return schemas.MessageResponse(message="Verification email sent")


@router.get("/verify-email", response_model=schemas.MessageResponse)
def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(
        models.User.email_verification_token == token
    ).first()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid or expired verification token")
    if user.email_verification_expires_at and user.email_verification_expires_at < utc_now():
        raise HTTPException(status_code=400, detail="Verification token has expired. Request a new one.")
    user.is_email_verified = True
    user.email_verification_token = None
    user.email_verification_expires_at = None
    db.commit()
    return schemas.MessageResponse(message="Email verified successfully")
