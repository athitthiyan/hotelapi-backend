import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
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
from services.payment_state_service import attach_bookings_lifecycle_state
from services.rate_limit_service import enforce_rate_limit


class MyBookingsResponse(BaseModel):
    total: int
    upcoming: int
    past: int
    cancelled: int
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
        "exp": utc_now() + expires_delta,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def build_token_response(user: models.User) -> schemas.TokenResponse:
    access_token = create_token(
        user, "access", timedelta(minutes=settings.access_token_exp_minutes)
    )
    refresh_token = create_token(
        user, "refresh", timedelta(days=settings.refresh_token_exp_days)
    )
    return schemas.TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        user=user,
    )


def hash_phone_otp(phone: str, otp: str) -> str:
    raw = f"{normalize_phone(phone)}:{otp}:{settings.secret_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def clear_phone_otp(user: models.User) -> None:
    user.pending_phone = None
    user.phone_otp_hash = None
    user.phone_otp_expires_at = None
    user.phone_otp_attempts = 0


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


@router.post("/signup", response_model=schemas.TokenResponse, status_code=201)
def signup(payload: schemas.UserSignup, request: Request, db: Session = Depends(get_db)):
    normalized_email = normalize_email(payload.email)
    enforce_rate_limit("auth:signup", request, subject=normalized_email)
    existing_user = db.query(models.User).filter(models.User.email == normalized_email).first()
    if existing_user:
        raise HTTPException(status_code=409, detail="Email already registered")

    user = models.User(
        email=normalized_email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        is_admin=False,
        is_partner=False,
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return build_token_response(user)


@router.post("/login", response_model=schemas.TokenResponse)
def login(payload: schemas.UserLogin, request: Request, db: Session = Depends(get_db)):
    normalized_email = normalize_email(payload.email)
    enforce_rate_limit("auth:login", request, subject=normalized_email)
    user = db.query(models.User).filter(models.User.email == normalized_email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")
    return build_token_response(user)


@router.post("/refresh", response_model=schemas.TokenResponse)
def refresh_token(payload: schemas.RefreshTokenRequest, db: Session = Depends(get_db)):
    token_payload = decode_token(payload.refresh_token, "refresh")
    user = get_user_from_payload(db, token_payload)
    return build_token_response(user)


@router.get("/me", response_model=schemas.UserResponse)
def read_me(user: models.User = Depends(get_current_user)):
    return user


@router.get("/me/bookings")
def my_bookings(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from datetime import timezone as _tz

    def _aware(dt: datetime) -> datetime:
        return dt.replace(tzinfo=_tz.utc) if dt.tzinfo is None else dt

    now = datetime.now(_tz.utc)
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
    upcoming = sum(
        1 for b in bookings
        if b.status in (
            models.BookingStatus.CONFIRMED,
            models.BookingStatus.PROCESSING,
            models.BookingStatus.PENDING,
        ) and _aware(b.check_in) >= now
    )
    cancelled = sum(
        1 for b in bookings
        if b.status in (models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED)
    )
    past = sum(
        1 for b in bookings
        if b.status == models.BookingStatus.COMPLETED
        or (
            b.status not in (models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED)
            and _aware(b.check_in) < now
        )
    )
    return MyBookingsResponse(
        total=len(bookings),
        upcoming=upcoming,
        past=past,
        cancelled=cancelled,
        bookings=[schemas.BookingResponse.model_validate(b) for b in bookings],
    )


# ─── Profile ──────────────────────────────────────────────────────────────────

@router.post("/phone/request-otp", response_model=schemas.PhoneOtpResponse)
def request_phone_otp(
    payload: schemas.PhoneOtpRequest,
    request: Request,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    phone = normalize_phone(payload.phone)
    enforce_rate_limit("auth:phone-otp", request, subject=f"{user.id}:{phone}")
    otp = f"{secrets.randbelow(1_000_000):06d}"
    user.pending_phone = phone
    user.phone_otp_hash = hash_phone_otp(phone, otp)
    user.phone_otp_expires_at = utc_now() + timedelta(minutes=5)
    user.phone_otp_attempts = 0
    if user.phone != phone:
        user.phone_verified = False
    db.commit()

    response = schemas.PhoneOtpResponse(
        message="Verification code sent to your phone.",
        phone=phone,
        expires_in_seconds=300,
    )
    if settings.app_env.lower() != "production":
        response.dev_code = otp
    return response


@router.post("/phone/verify", response_model=schemas.UserDetailResponse)
def verify_phone_otp(
    payload: schemas.PhoneOtpVerifyRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    phone = normalize_phone(payload.phone)
    now = utc_now()
    if (
        not user.pending_phone
        or user.pending_phone != phone
        or not user.phone_otp_hash
        or not user.phone_otp_expires_at
        or ensure_aware_utc(user.phone_otp_expires_at) < now
    ):
        clear_phone_otp(user)
        db.commit()
        raise HTTPException(status_code=400, detail="Phone verification code expired")
    if user.phone_otp_attempts >= 5:
        raise HTTPException(status_code=429, detail="Too many phone verification attempts")
    if not secrets.compare_digest(user.phone_otp_hash, hash_phone_otp(phone, payload.otp)):
        user.phone_otp_attempts += 1
        db.commit()
        raise HTTPException(status_code=400, detail="Invalid phone verification code")

    user.phone = phone
    user.phone_verified = True
    clear_phone_otp(user)
    db.commit()
    db.refresh(user)
    return user


@router.put("/me", response_model=schemas.UserDetailResponse)
def update_profile(
    payload: schemas.UserProfileUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.phone is not None:
        phone = normalize_phone(payload.phone)
        if not phone:
            raise HTTPException(status_code=400, detail="Phone number is required")
        if user.phone != phone or not user.phone_verified:
            raise HTTPException(
                status_code=400,
                detail="Verify this phone number with OTP first",
            )
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


@router.post("/forgot-password", response_model=schemas.MessageResponse)
def forgot_password(
    payload: schemas.ForgotPasswordRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    enforce_rate_limit("auth:forgot-password", request, subject=payload.email.lower())
    user = db.query(models.User).filter(models.User.email == normalize_email(payload.email)).first()
    # Always return 200 to prevent email enumeration
    if user and user.is_active:
        raw_token = secrets.token_urlsafe(32)
        token_hash = _hash_reset_token(raw_token)
        from datetime import timedelta
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
    db: Session = Depends(get_db),
):
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
    db: Session = Depends(get_db),
):
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
    return build_token_response(user)


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
