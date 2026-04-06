import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="sqlite:///./stayvora_dev.db",
        validation_alias=AliasChoices("DATABASE_URL", "database_url"),
    )
    supabase_url: str = Field(
        default="",
        validation_alias=AliasChoices("SUPABASE_URL", "supabase_url"),
    )
    supabase_service_key: str = Field(
        default="",
        validation_alias=AliasChoices("SUPABASE_SERVICE_KEY", "supabase_service_key"),
    )
    stripe_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("STRIPE_SECRET_KEY", "stripe_secret_key"),
    )
    stripe_publishable_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "STRIPE_PUBLISHABLE_KEY", "stripe_publishable_key"
        ),
    )
    stripe_webhook_secret: str = Field(
        default="",
        validation_alias=AliasChoices(
            "STRIPE_WEBHOOK_SECRET", "stripe_webhook_secret"
        ),
    )
    secret_key: str = Field(
        default="change-this-in-production",
        validation_alias=AliasChoices("SECRET_KEY", "secret_key"),
    )
    app_env: str = Field(
        default="development",
        validation_alias=AliasChoices(
            "APP_ENV", "app_env", "ENVIRONMENT", "environment"
        ),
    )
    allowed_origins: str = Field(
        default=(
            "http://localhost:4200,http://localhost:4201,"
            "http://localhost:4202,http://localhost:4203,"
            "http://127.0.0.1:4200,http://127.0.0.1:4201,"
            "http://127.0.0.1:4202,http://127.0.0.1:4203,"
            "https://stayvora.co.in,https://www.stayvora.co.in,"
            "https://stayease-booking-app.vercel.app,"
            "https://stayease-booking-app-git-main-athitthiyans-projects.vercel.app,"
            "https://payflow-payment-app.vercel.app,"
            "https://insightboard-admin.vercel.app,"
            "https://partner-portal.vercel.app,"
            "https://stayease-partner-portal.vercel.app"
        ),
        validation_alias=AliasChoices("ALLOWED_ORIGINS", "allowed_origins"),
    )
    access_token_exp_minutes: int = Field(
        default=30,
        validation_alias=AliasChoices(
            "ACCESS_TOKEN_EXP_MINUTES", "access_token_exp_minutes"
        ),
    )
    refresh_token_exp_days: int = Field(
        default=7,
        validation_alias=AliasChoices(
            "REFRESH_TOKEN_EXP_DAYS", "refresh_token_exp_days"
        ),
    )
    seed_admin_email: str = Field(
        default="ops@stayvora.co.in",
        validation_alias=AliasChoices("SEED_ADMIN_EMAIL", "seed_admin_email"),
    )
    seed_admin_password: str = Field(
        default="AdminPass123",
        validation_alias=AliasChoices("SEED_ADMIN_PASSWORD", "seed_admin_password"),
    )
    seed_admin_name: str = Field(
        default="InsightBoard Admin",
        validation_alias=AliasChoices("SEED_ADMIN_NAME", "seed_admin_name"),
    )
    seed_partner_email: str = Field(
        default="reservations@stayvora-partners.co.in",
        validation_alias=AliasChoices("SEED_PARTNER_EMAIL", "seed_partner_email"),
    )
    seed_partner_password: str = Field(
        default="PartnerPass123",
        validation_alias=AliasChoices("SEED_PARTNER_PASSWORD", "seed_partner_password"),
    )
    seed_partner_name: str = Field(
        default="Stayvora Partner Owner",
        validation_alias=AliasChoices("SEED_PARTNER_NAME", "seed_partner_name"),
    )
    seed_partner_hotel_name: str = Field(
        default="Stayvora Marina Suites",
        validation_alias=AliasChoices(
            "SEED_PARTNER_HOTEL_NAME", "seed_partner_hotel_name"
        ),
    )
    auto_create_schema: bool = Field(
        default=False,
        validation_alias=AliasChoices("AUTO_CREATE_SCHEMA", "auto_create_schema"),
    )
    # ── Email delivery (Resend) ───────────────────────────────────────────────
    resend_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("RESEND_API_KEY", "resend_api_key"),
    )
    email_from_address: str = Field(
        default="noreply@stayvora.co.in",
        validation_alias=AliasChoices("EMAIL_FROM_ADDRESS", "email_from_address"),
    )
    email_from_name: str = Field(
        default="Stayvora",
        validation_alias=AliasChoices("EMAIL_FROM_NAME", "email_from_name"),
    )
    # ── Razorpay ──────────────────────────────────────────────────────────────
    razorpay_key_id: str = Field(
        default="",
        validation_alias=AliasChoices("RAZORPAY_KEY_ID", "razorpay_key_id"),
    )
    razorpay_key_secret: str = Field(
        default="",
        validation_alias=AliasChoices("RAZORPAY_KEY_SECRET", "razorpay_key_secret"),
    )
    razorpay_webhook_secret: str = Field(
        default="",
        validation_alias=AliasChoices("RAZORPAY_WEBHOOK_SECRET", "razorpay_webhook_secret"),
    )
    # ── Google Maps ───────────────────────────────────────────────────────────
    google_maps_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("GOOGLE_MAPS_API_KEY", "google_maps_api_key"),
    )
    # ── Apple Sign-In ─────────────────────────────────────────────────────────
    apple_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("APPLE_CLIENT_ID", "apple_client_id"),
    )
    # ── Microsoft Azure AD SSO ────────────────────────────────────────────────
    microsoft_tenant_id: str = Field(
        default="common",
        validation_alias=AliasChoices("MICROSOFT_TENANT_ID", "microsoft_tenant_id"),
    )
    microsoft_client_id: str = Field(
        default="",
        validation_alias=AliasChoices("MICROSOFT_CLIENT_ID", "microsoft_client_id"),
    )

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql://", 1)
        return value


def select_settings_env_file() -> str:
    """Select the safest env file for the current runtime."""
    explicit_env_file = os.getenv("STAYVORA_ENV_FILE") or os.getenv("ENV_FILE")
    if explicit_env_file:
        return explicit_env_file

    if os.getenv("APP_ENV", "").lower() == "production" and Path(".env.prod").exists():
        return ".env.prod"

    if Path(".env.local").exists():
        return ".env.local"

    return ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings(_env_file=select_settings_env_file())


def validate_runtime_configuration(config: Settings) -> None:
    """Raise RuntimeError for insecure production configurations."""
    if config.app_env.lower() != "production":
        return
    insecure_default = config.secret_key == "change-this-in-production"
    too_short = len(config.secret_key) < 32
    if insecure_default or too_short:
        raise RuntimeError(
            "Production SECRET_KEY must be set and at least 32 characters long"
        )


settings = get_settings()


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _build_engine(database_url: str):
    """
    Build a SQLAlchemy engine tuned for the target database.

    - PostgreSQL via Supabase pgbouncer (port 6543, transaction mode):
        pool_size=3, max_overflow=7  — pgbouncer manages the real pool;
        SQLAlchemy's pool should be tiny.
    - SQLite (local dev / tests):
        NullPool equivalent — each thread gets its own connection.
    """
    if _is_sqlite(database_url):
        eng = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
        )
        # Enable WAL mode for concurrent reads in tests
        @event.listens_for(eng, "connect")
        def set_sqlite_pragma(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return eng

    # PostgreSQL / Supabase pgbouncer
    return create_engine(
        database_url,
        pool_size=3,
        max_overflow=7,
        pool_pre_ping=True,
        pool_recycle=300,
    )


engine = _build_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
