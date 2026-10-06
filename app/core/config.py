"""Application settings, loaded from environment variables (with a .env
file read for local dev). All local-dev defaults below are safe fallbacks;
override every one of them with real values outside local development.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env relative to this file's location (backend/.env), not the
# process's current working directory — a bare relative path silently finds
# nothing (falling back to defaults) whenever this app is launched from
# somewhere other than the backend/ folder, e.g. via a tool that starts
# uvicorn with --app-dir from a different cwd.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    # SQLite by default so the project runs with zero external services;
    # point DATABASE_URL at Postgres for anything beyond local smoke tests, e.g.
    #   postgresql+psycopg2://payroll_user:payroll_pass@localhost:5432/staunch_payroll
    DATABASE_URL: str = "sqlite:///./payroll_dev.db"

    JWT_SECRET_KEY: str = "dev-only-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 480

    # Comma-separated origins, e.g. "http://localhost:3000,https://payroll.staunchtec.com"
    CORS_ORIGINS: str = "http://localhost:3000"

    API_V1_PREFIX: str = "/api/v1"

    # --- ICICI Composite Pay API (bulk salary disbursement) -----------------
    # All default to empty/safe values so nothing breaks until the user's
    # ICICI relationship manager provides real onboarding credentials. See
    # app/services/icici_api/client.py's module docstring — every one of
    # these (except ICICI_BASE_URL/ICICI_ENCRYPTION_ENABLED) MUST be set to a
    # real value from ICICI before any live or UAT-sandbox call can succeed.
    ICICI_API_KEY: str = ""
    ICICI_CORP_ID: str = ""
    ICICI_USER_ID: str = ""
    ICICI_AGGR_ID: str = ""
    ICICI_AGGR_NAME: str = ""
    ICICI_URN: str = ""
    # UAT sandbox by default — LIVE must never be the accidental default.
    ICICI_BASE_URL: str = "https://apibankingonesandbox.icicibank.com"
    # Whether to wrap request/response bodies in the documented hybrid
    # RSA+AES encryption envelope (PDF "ENCRYPTION-DECRYPTION PROCESS",
    # pages 76-78). Defaults OFF: plain JSON is a common sandbox convenience,
    # but confirm with the RM whether UAT actually requires encryption before
    # relying on this default. Flipping this on with no key material below
    # configured raises a clear config error rather than failing silently.
    ICICI_ENCRYPTION_ENABLED: bool = False
    # PEM-encoded keys for the encryption envelope above (only needed once
    # ICICI_ENCRYPTION_ENABLED=True). ICICI_RSA_PUBLIC_KEY_PEM encrypts the
    # outgoing session key; ICICI_RSA_PRIVATE_KEY_PEM decrypts ICICI's
    # encrypted responses. Both come from the RSA key-exchange step in
    # onboarding — not available yet.
    ICICI_RSA_PUBLIC_KEY_PEM: str = ""
    ICICI_RSA_PRIVATE_KEY_PEM: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]


settings = Settings()
