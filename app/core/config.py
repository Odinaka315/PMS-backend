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

    # Every account HR creates gets its own random one-time password, shown to HR once. The user must choose their own
    # at first sign-in, and an unused one-time password stops working after INITIAL_PASSWORD_DAYS.
    initial_password_days: int = 14
    auto_create_accounts: bool = True  # create a login automatically when HR creates a staff record

    # PLACEHOLDER policy values, to be confirmed by the hospital.
    retirement_age: int = 60
    retirement_service_years: int = 35

    # Document storage. "local" keeps files in a folder (development/tests); "r2" uses Cloudflare R2.
    storage_backend: str = "local"  # local | r2
    local_storage_dir: str = "./.storage"
    api_public_url: str = "http://localhost:8000"  # where the local backend's signed links point
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""  # the bucket's name exactly as shown in the Cloudflare dashboard
    r2_endpoint_url: str = ""  # optional override (tests / other S3-compatible stores); default is derived from the account id
    upload_url_seconds: int = 300  # how long a signed upload link works
    download_url_seconds: int = 60  # how long a signed download link works
    max_upload_bytes: int = 10 * 1024 * 1024
    max_documents_per_staff: int = 100

    @model_validator(mode="after")
    def _production_guard(self):
        if self.storage_backend not in ("local", "r2"):
            raise ValueError("STORAGE_BACKEND must be 'local' or 'r2'")
        if self.storage_backend == "r2":
            missing = [n for n in ("r2_account_id", "r2_access_key_id", "r2_secret_access_key", "r2_bucket")
                       if not getattr(self, n)]
            if missing:
                raise ValueError("STORAGE_BACKEND=r2 needs: " + ", ".join(m.upper() for m in missing))
        if self.environment == "production":
            if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
                raise ValueError("SECRET_KEY must be set to a strong value (>=32 chars) in production")
            if not self.field_encryption_key:
                raise ValueError("FIELD_ENCRYPTION_KEY must be set in production")
            if self.storage_backend != "r2":
                raise ValueError("STORAGE_BACKEND must be 'r2' in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
