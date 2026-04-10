from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

import models
import schemas
from services.audit_service import write_audit_log
from services.notification_service import enqueue_notification
from services.rate_limit_service import enforce_rate_limit

logger = logging.getLogger(__name__)

OTP_LENGTH = 6
OTP_TTL_SECONDS = 300
OTP_RESEND_COOLDOWN_SECONDS = 30
OTP_MAX_ATTEMPTS = 5
OTP_MAX_RESENDS = 3
OTP_REQUEST_LIMIT_15M = "auth:otp-request-15m"
OTP_REQUEST_LIMIT_1H = "auth:otp-request-1h"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def normalize_recipient(channel: schemas.OtpChannel, recipient: str) -> str:
    cleaned = recipient.strip()
    if channel == schemas.OtpChannel.EMAIL:
        return cleaned.lower()
    return " ".join(cleaned.split())


def hash_otp(challenge_id: str, otp: str, secret_key: str) -> str:
    raw = f"{challenge_id}:{otp}:{secret_key}"
    return hashlib.sha256(raw.encode()).hexdigest()


def request_meta(request: Request) -> dict[str, str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",")[0].strip() if forwarded else ""
    if not client_ip and request.client:
        client_ip = request.client.host
    return {
        "ip": client_ip or "unknown",
        "user_agent": request.headers.get("user-agent", ""),
    }


def enforce_otp_request_limits(
    request: Request,
    *,
    recipient: str,
    user_id: int | None,
    device_fingerprint: str | None,
) -> None:
    for scope in (OTP_REQUEST_LIMIT_15M, OTP_REQUEST_LIMIT_1H):
        enforce_rate_limit(scope, request, subject=recipient)
        if user_id is not None:
            enforce_rate_limit(scope, request, subject=f"user:{user_id}")
        if device_fingerprint:
            enforce_rate_limit(scope, request, subject=f"device:{device_fingerprint}")


def find_active_challenge(
    db: Session,
    *,
    flow: schemas.OtpFlow,
    channel: schemas.OtpChannel,
    recipient: str,
    user_id: int | None,
) -> models.OtpChallenge | None:
    query = db.query(models.OtpChallenge).filter(
        models.OtpChallenge.flow == flow.value,
        models.OtpChallenge.channel == channel.value,
        models.OtpChallenge.recipient == recipient,
        models.OtpChallenge.consumed_at.is_(None),
    )
    if user_id is None:
        query = query.filter(models.OtpChallenge.user_id.is_(None))
    else:
        query = query.filter(models.OtpChallenge.user_id == user_id)
    return query.order_by(models.OtpChallenge.created_at.desc()).first()


def enforce_abuse_lock(
    db: Session,
    *,
    flow: schemas.OtpFlow,
    channel: schemas.OtpChannel,
    recipient: str,
    user_id: int | None,
) -> datetime | None:
    now = utc_now()
    recent_cutoff = now - timedelta(hours=1)
    query = db.query(models.OtpChallenge).filter(
        models.OtpChallenge.flow == flow.value,
        models.OtpChallenge.channel == channel.value,
        models.OtpChallenge.recipient == recipient,
        models.OtpChallenge.created_at >= recent_cutoff,
    )
    if user_id is None:
        query = query.filter(models.OtpChallenge.user_id.is_(None))
    else:
        query = query.filter(models.OtpChallenge.user_id == user_id)
    challenges = query.all()
    total_attempts = sum(challenge.attempts for challenge in challenges)
    locked_recently = [challenge for challenge in challenges if challenge.locked_at and challenge.locked_at >= now - timedelta(minutes=15)]
    if total_attempts >= 10 or len(locked_recently) >= 2:
        blocked_until = max(
            [challenge.locked_at for challenge in locked_recently if challenge.locked_at] + [now]
        ) + timedelta(minutes=15)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "otp_temporarily_blocked",
                "message": "Too many invalid OTP attempts. Try again later.",
                "blocked_until": blocked_until.isoformat(),
            },
        )
    return None


def build_challenge_response(
    challenge: models.OtpChallenge,
    *,
    message: str,
    dev_code: str | None = None,
    blocked_until: datetime | None = None,
) -> schemas.OtpChallengeResponse:
    now = utc_now()
    expires_at = ensure_aware_utc(challenge.expires_at)
    resend_available_at = ensure_aware_utc(challenge.resend_available_at)
    return schemas.OtpChallengeResponse(
        message=message,
        challenge_id=challenge.id,
        flow=schemas.OtpFlow(challenge.flow),
        channel=schemas.OtpChannel(challenge.channel),
        recipient=challenge.recipient,
        expires_in_seconds=max(0, int((expires_at - now).total_seconds())),
        resend_available_in_seconds=max(0, int((resend_available_at - now).total_seconds())),
        resends_remaining=max(0, challenge.max_resends - challenge.resend_count),
        attempts_remaining=max(0, challenge.max_attempts - challenge.attempts),
        max_resends=challenge.max_resends,
        max_attempts=challenge.max_attempts,
        dev_code=dev_code,
        blocked_until=blocked_until,
    )


def send_otp_notification(
    db: Session,
    *,
    channel: schemas.OtpChannel,
    recipient: str,
    otp: str,
    flow: schemas.OtpFlow,
) -> None:
    if channel == schemas.OtpChannel.EMAIL:
        enqueue_notification(
            db,
            event_type=f"otp_{flow.value}",
            recipient_email=recipient,
            subject="Your Stayvora verification code",
            body=(
                f"Your Stayvora verification code is {otp}. "
                "It expires in 5 minutes. Do not share this code."
            ),
        )
        return
    logger.info("OTP SMS dispatch requested for %s %s", flow.value, recipient)


def issue_otp_challenge(
    db: Session,
    request: Request,
    *,
    flow: schemas.OtpFlow,
    channel: schemas.OtpChannel,
    recipient: str,
    secret_key: str,
    user_id: int | None = None,
    device_fingerprint: str | None = None,
) -> tuple[models.OtpChallenge, str | None]:
    normalized_recipient = normalize_recipient(channel, recipient)
    enforce_otp_request_limits(
        request,
        recipient=f"{flow.value}:{channel.value}:{normalized_recipient}",
        user_id=user_id,
        device_fingerprint=device_fingerprint,
    )
    enforce_abuse_lock(
        db,
        flow=flow,
        channel=channel,
        recipient=normalized_recipient,
        user_id=user_id,
    )
    now = utc_now()
    challenge = find_active_challenge(
        db,
        flow=flow,
        channel=channel,
        recipient=normalized_recipient,
        user_id=user_id,
    )
    if challenge and challenge.verified_at:
        challenge.consumed_at = now
        db.flush()
        challenge = None
    if challenge and challenge.locked_at:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "otp_attempts_exceeded",
                "message": "This OTP challenge is locked. Start again to receive a new code.",
            },
        )
    if challenge and challenge.resend_count >= challenge.max_resends:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "otp_resend_limit_reached",
                "message": "Resend limit reached. Restart the flow to get a new OTP.",
            },
        )
    if challenge and ensure_aware_utc(challenge.resend_available_at) > now:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "otp_resend_cooldown",
                "message": "Please wait before requesting another OTP.",
                "resend_available_in_seconds": int((ensure_aware_utc(challenge.resend_available_at) - now).total_seconds()),
                "challenge_id": challenge.id,
            },
        )

    otp = f"{secrets.randbelow(10 ** OTP_LENGTH):0{OTP_LENGTH}d}"
    if challenge is None:
        challenge = models.OtpChallenge(
            id=str(uuid.uuid4()),
            user_id=user_id,
            flow=flow.value,
            channel=channel.value,
            recipient=normalized_recipient,
            otp_hash="",
            attempts=0,
            max_attempts=OTP_MAX_ATTEMPTS,
            resend_count=0,
            max_resends=OTP_MAX_RESENDS,
            resend_available_at=now + timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS),
            expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
            device_fingerprint=device_fingerprint,
        )
        db.add(challenge)
        db.flush()
    else:
        challenge.resend_count += 1
        challenge.attempts = 0
        challenge.locked_at = None
        challenge.verified_at = None
        challenge.resend_available_at = now + timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)
        challenge.expires_at = now + timedelta(seconds=OTP_TTL_SECONDS)
        challenge.device_fingerprint = device_fingerprint

    challenge.otp_hash = hash_otp(challenge.id, otp, secret_key)
    send_otp_notification(db, channel=channel, recipient=normalized_recipient, otp=otp, flow=flow)
    write_audit_log(
        db,
        user_id,
        "auth.otp.sent",
        "otp_challenge",
        challenge.id,
        {
            **request_meta(request),
            "flow": flow.value,
            "channel": channel.value,
            "recipient": normalized_recipient,
            "resend_count": challenge.resend_count,
        },
    )
    dev_code = otp if channel == schemas.OtpChannel.PHONE or True else None
    return challenge, dev_code


def verify_otp_challenge(
    db: Session,
    request: Request,
    *,
    challenge_id: str,
    otp: str,
    secret_key: str,
    user_id: int | None = None,
) -> models.OtpChallenge:
    challenge = db.query(models.OtpChallenge).filter(models.OtpChallenge.id == challenge_id).first()
    if not challenge:
        raise HTTPException(status_code=404, detail={"code": "otp_not_found", "message": "OTP challenge not found."})
    if user_id is not None and challenge.user_id not in (None, user_id):
        raise HTTPException(status_code=403, detail="This OTP challenge does not belong to the current user.")

    now = utc_now()
    if challenge.consumed_at:
        raise HTTPException(status_code=400, detail={"code": "otp_consumed", "message": "This OTP challenge has already been used."})
    if challenge.locked_at:
        raise HTTPException(
            status_code=429,
            detail={"code": "otp_attempts_exceeded", "message": "Maximum verification attempts reached. Request a new OTP."},
        )
    expires_at = ensure_aware_utc(challenge.expires_at)
    if expires_at < now:
        raise HTTPException(
            status_code=400,
            detail={"code": "otp_expired", "message": "OTP expired. Request a new one."},
        )
    if challenge.verified_at:
        return challenge
    if challenge.attempts >= challenge.max_attempts:
        challenge.locked_at = now
        db.flush()
        raise HTTPException(
            status_code=429,
            detail={"code": "otp_attempts_exceeded", "message": "Maximum verification attempts reached. Request a new OTP."},
        )

    expected = hash_otp(challenge.id, otp, secret_key)
    if not secrets.compare_digest(challenge.otp_hash, expected):
        challenge.attempts += 1
        if challenge.attempts >= challenge.max_attempts:
            challenge.locked_at = now
        write_audit_log(
            db,
            challenge.user_id,
            "auth.otp.failed",
            "otp_challenge",
            challenge.id,
            {
                **request_meta(request),
                "flow": challenge.flow,
                "channel": challenge.channel,
                "attempts": challenge.attempts,
            },
        )
        db.flush()
        raise HTTPException(
            status_code=400 if challenge.locked_at is None else 429,
            detail={
                "code": "otp_invalid" if challenge.locked_at is None else "otp_attempts_exceeded",
                "message": "Invalid OTP." if challenge.locked_at is None else "Maximum verification attempts reached. Request a new OTP.",
                "attempts_remaining": max(0, challenge.max_attempts - challenge.attempts),
            },
        )

    challenge.verified_at = now
    write_audit_log(
        db,
        challenge.user_id,
        "auth.otp.verified",
        "otp_challenge",
        challenge.id,
        {
            **request_meta(request),
            "flow": challenge.flow,
            "channel": challenge.channel,
        },
    )
    db.flush()
    return challenge
