from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        validation_alias=AliasChoices("DATABASE_URL", "database_url")
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
        validation_alias=AliasChoices("APP_ENV", "app_env", "ENVIRONMENT", "environment"),
    )
    allowed_origins: str = Field(
        default="http://localhost:4200,http://localhost:4201,http://localhost:4202,http://localhost:4203,https://stayvora.co.in,https://www.stayvora.co.in,https://stayease-booking-app.vercel.app,https://stayease-booking-app-git-main-athitthiyans-projects.vercel.app,https://payflow-payment-app.vercel.app,https://insightboard-admin.vercel.app,https://stayease-partner-portal.vercel.app",
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
        default="admin@example.com",
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
        default="partner@example.com",
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
        validation_alias=AliasChoices("SEED_PARTNER_HOTEL_NAME", "seed_partner_hotel_name"),
    )
    auto_create_schema: bool = Field(
        default=False,
        validation_alias=AliasChoices("AUTO_CREATE_SCHEMA", "auto_create_schema"),
    )

    @field_validator("database_url")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql://", 1)
        return value


@lru_cache()
def get_settings():
    return Settings()


def validate_runtime_configuration(config: Settings) -> None:
    if config.app_env.lower() != "production":
        return

    insecure_default = config.secret_key == "change-this-in-production"
    too_short = len(config.secret_key) < 32
    if insecure_default or too_short:
        raise RuntimeError(
            "Production SECRET_KEY must be set and at least 32 characters long"
        )


settings = get_settings()

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
