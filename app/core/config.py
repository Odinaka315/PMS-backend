from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-only-change-me"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"  # development | test | production
    # Local default is SQLite. For Neon use the *pooled* URL here and the direct URL in MIGRATION_DATABASE_URL.
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    migration_database_url: str = ""

    secret_key: str = DEV_SECRET
    # Fernet key for field-level encryption (bank account, BVN). Derived from secret_key in development only.
    field_encryption_key: str = ""

    access_token_minutes: int = 15
    refresh_token_days: int = 7
    cors_origins: list[str] = ["http://localhost:5173"]

    max_failed_logins: int = 5
    lockout_minutes: int = 15
    min_password_length: int = 8

    # PLACEHOLDER policy values, to be confirmed by the hospital.
    retirement_age: int = 60
    retirement_service_years: int = 35

    @model_validator(mode="after")
    def _production_guard(self):
        if self.environment == "production":
            if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must be set to a strong value (>=32 chars) in production")
            if not self.field_encryption_key:
                raise ValueError("FIELD_ENCRYPTION_KEY must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
