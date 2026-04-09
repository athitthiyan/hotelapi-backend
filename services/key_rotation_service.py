"""
Key Rotation Service — Stayvora
================================
Utilities for rotating API credentials without downtime.
Supports dual-key validation during rotation window.

Usage:
  1. Set new key in env (e.g. STRIPE_SECRET_KEY_NEXT)
  2. Deploy — both old and new keys are valid during overlap
  3. Verify traffic is using new key
  4. Remove old key env var

Environment variables (per provider):
  {PROVIDER}_KEY           — current active key
  {PROVIDER}_KEY_NEXT      — new key (set during rotation)
  {PROVIDER}_KEY_ROTATED_AT — ISO timestamp of last rotation
"""

import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class RotationStatus:
    provider: str
    has_current: bool
    has_next: bool
    rotated_at: str | None
    status: str  # "active", "rotating", "missing"


# Providers and their env var prefixes
PROVIDERS = {
    "stripe": {
        "keys": ["STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET"],
        "description": "Stripe payment gateway",
    },
    "razorpay": {
        "keys": ["RAZORPAY_KEY_ID", "RAZORPAY_KEY_SECRET", "RAZORPAY_WEBHOOK_SECRET"],
        "description": "Razorpay payment gateway",
    },
    "resend": {
        "keys": ["RESEND_API_KEY"],
        "description": "Resend email delivery",
    },
    "google": {
        "keys": ["GOOGLE_CLIENT_ID", "GOOGLE_MAPS_API_KEY"],
        "description": "Google OAuth & Maps",
    },
    "microsoft": {
        "keys": ["MICROSOFT_CLIENT_ID"],
        "description": "Microsoft SSO",
    },
    "supabase": {
        "keys": ["SUPABASE_SERVICE_KEY"],
        "description": "Supabase service role",
    },
    "jwt": {
        "keys": ["SECRET_KEY"],
        "description": "JWT signing secret",
    },
}


def get_rotation_status() -> list[RotationStatus]:
    """Check rotation status for all configured providers."""
    statuses = []
    for provider, config in PROVIDERS.items():
        for key_name in config["keys"]:
            current = os.getenv(key_name, "")
            next_key = os.getenv(f"{key_name}_NEXT", "")
            rotated_at = os.getenv(f"{key_name}_ROTATED_AT", "")

            if not current and not next_key:
                status = "missing"
            elif next_key:
                status = "rotating"
            else:
                status = "active"

            statuses.append(RotationStatus(
                provider=f"{provider}:{key_name}",
                has_current=bool(current),
                has_next=bool(next_key),
                rotated_at=rotated_at or None,
                status=status,
            ))
    return statuses


def get_active_key(key_name: str) -> str:
    """
    Get the active key for a given env var name.
    During rotation, returns the NEXT key (new key takes precedence).
    """
    next_key = os.getenv(f"{key_name}_NEXT", "")
    if next_key:
        return next_key
    return os.getenv(key_name, "")


def validate_key_pair(key_name: str, provided_key: str) -> bool:
    """
    Validate a key against both current and next keys.
    Used during rotation window to accept both old and new keys.
    """
    import hmac
    current = os.getenv(key_name, "")
    next_key = os.getenv(f"{key_name}_NEXT", "")

    if current and hmac.compare_digest(current, provided_key):
        return True
    if next_key and hmac.compare_digest(next_key, provided_key):
        return True
    return False


def confirm_rotation(key_name: str) -> bool:
    """
    Confirm a rotation is complete.
    Logs the rotation timestamp. The actual env var swap should be done
    in the deployment pipeline (remove _NEXT, update primary).
    """
    next_key = os.getenv(f"{key_name}_NEXT", "")
    if not next_key:
        logger.warning("No pending rotation for %s", key_name)
        return False

    timestamp = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Key rotation confirmed for %s at %s. "
        "Remove %s_NEXT and update %s in your deployment config.",
        key_name, timestamp, key_name, key_name,
    )
    return True


def get_rotation_report() -> dict:
    """Generate a full rotation status report."""
    statuses = get_rotation_status()
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_keys": len(statuses),
        "rotating": sum(1 for s in statuses if s.status == "rotating"),
        "active": sum(1 for s in statuses if s.status == "active"),
        "missing": sum(1 for s in statuses if s.status == "missing"),
        "keys": [
            {
                "provider": s.provider,
                "status": s.status,
                "has_current": s.has_current,
                "has_next": s.has_next,
                "rotated_at": s.rotated_at,
            }
            for s in statuses
        ],
    }
