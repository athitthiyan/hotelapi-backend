import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
import httpx as _httpx
from jose import JWTError, jwt
from passlib.context import CryptContext
from passlib.exc import PasswordValueError, UnknownHashError
from sqlalchemy import or_
from sqlalchemy.orm import Session

import models
import schemas
from database import get_db, settings
from services.rate_limit_service import enforce_rate_limit

router = APIRouter(prefix="/auth", tags=["Auth"])

# Keep pbkdf2_sha256 as the preferred scheme for newly created passwords,
# while still accepting older/manual bcrypt hashes already stored in production.
pwd_context = CryptContext(schemes=["pbkdf2_sha256", "bcrypt"], deprecated="auto")
ALGORITHM = "HS256"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().lower()


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


# ─── Profile ──────────────────────────────────────────────────────────────────

@router.put("/me", response_model=schemas.UserDetailResponse)
def update_profile(
    payload: schemas.UserProfileUpdate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.full_name is not None:
        user.full_name = payload.full_name
    if payload.phone is not None:
        user.phone = payload.phone
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


# ─── Social Login (Google) ────────────────────────────────────────────────────

GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


@router.post("/social-login", response_model=schemas.TokenResponse)
async def social_login(
    payload: schemas.SocialLoginRequest,
    db: Session = Depends(get_db),
):
    if payload.provider != "google":
        raise HTTPException(status_code=400, detail="Only 'google' provider is supported")
    async with _httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {payload.id_token}"},
            )
        except _httpx.RequestError as exc:
            raise HTTPException(status_code=502, detail="Failed to verify Google token") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="Google token verification failed")
    google_data = resp.json()
    google_id: str = google_data.get("sub", "")
    email: str = google_data.get("email", "")
    full_name: str = google_data.get("name", email.split("@")[0])
    avatar_url: str | None = google_data.get("picture")
    if not google_id or not email:
        raise HTTPException(status_code=400, detail="Insufficient data from Google")
    normalized_email = normalize_email(email)
    # Find or create user
    user = db.query(models.User).filter(models.User.google_id == google_id).first()
    if not user:
        user = db.query(models.User).filter(models.User.email == normalized_email).first()
        if user:
            user.google_id = google_id
            if avatar_url and not user.avatar_url:
                user.avatar_url = avatar_url
        else:
            user = models.User(
                email=normalized_email,
                full_name=full_name,
                google_id=google_id,
                avatar_url=avatar_url,
                hashed_password=None,
                is_admin=False,
                is_partner=False,
                is_active=True,
            )
            db.add(user)
        db.commit()
        db.refresh(user)
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is inactive")
    return build_token_response(user)


# ─── My Bookings ──────────────────────────────────────────────────────────────

@router.get("/me/bookings", response_model=schemas.MyBookingsResponse)
def my_bookings(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from sqlalchemy.orm import joinedload
    bookings = (
        db.query(models.Booking)
        .options(joinedload(models.Booking.room))
        .filter(
            or_(
                models.Booking.user_id == user.id,
                models.Booking.email == user.email,
            )
        )
        .order_by(models.Booking.created_at.desc())
        .all()
    )
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)
    upcoming = sum(
        1 for b in bookings
        if b.status == models.BookingStatus.CONFIRMED
        and b.check_out.replace(tzinfo=_tz.utc if b.check_out.tzinfo is None else b.check_out.tzinfo) >= now
    )
    past = sum(
        1 for b in bookings
        if b.status == models.BookingStatus.COMPLETED
        or (
            b.status == models.BookingStatus.CONFIRMED
            and b.check_out.replace(tzinfo=_tz.utc if b.check_out.tzinfo is None else b.check_out.tzinfo) < now
        )
    )
    cancelled = sum(
        1 for b in bookings
        if b.status in (models.BookingStatus.CANCELLED, models.BookingStatus.EXPIRED)
    )
    return schemas.MyBookingsResponse(
        bookings=bookings,
        total=len(bookings),
        upcoming=upcoming,
        past=past,
        cancelled=cancelled,
    )
