"""All settings in one place.

Values come from environment variables first, then `backend/.env`, then the
defaults written here. Nothing secret has a real default: you supply it in .env.

APP_ENV=production turns on strict checks: the API refuses to start with a
weak setup (no JWT secret, demo users on, SQLite...) rather than run unsafely.
"""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The backend/ folder, so finding .env does not depend on where uvicorn was started.
BACKEND_DIR = Path(__file__).resolve().parents[1]

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    # "development" is forgiving; "production" refuses unsafe settings at startup.
    app_env: Literal["development", "production"] = "development"

    # MySQL 8: mysql+pymysql://USER:PASSWORD@HOST:3306/marketing_ops?charset=utf8mb4
    # Required. (The tests point this at in-memory SQLite.)
    database_url: str = Field(default="", validate_default=True)
    db_pool_size: int = 10        # connections kept open per API process
    db_max_overflow: int = 10     # extra connections allowed under bursts

    # Signs the login tokens. Required in production. In development an empty value
    # gets a random one per run (so everyone is signed out whenever the API restarts).
    jwt_secret: str = ""
    jwt_hours: int = 12

    # Login protection: this many wrong passwords locks the account for a while.
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # Creates the demo team (one user per role) on first start. Never in production.
    seed_demo_data: bool = False
    demo_password: str = ""   # required when seed_demo_data is on

    # The AI drafter: Groq, authenticated with an API key from the environment.
    # Disabled or unconfigured = template fallback.
    llm_enabled: bool = True
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"

    # Public sign-up creates active Creator accounts. Elevated roles remain admin-only.
    registration_enabled: bool = True

    # LinkedIn: your app from linkedin.com/developers. An admin clicks "Connect LinkedIn"
    # in the console; the resulting token is stored encrypted in the database, never here.
    linkedin_client_id: str = ""
    linkedin_client_secret: str = ""
    linkedin_redirect_uri: str = "http://localhost:8787/api/channels/linkedin/callback"
    linkedin_api_version: str = "202607"   # YYYYMM; LinkedIn supports each version for about a year
    # Company Page to post as, e.g. urn:li:organization:12345. Used only once LinkedIn has
    # approved your app for company posting (w_organization_social); until then, posts go
    # to the connected person's own profile.
    linkedin_organization_urn: str = ""

    cms_webhook_url: str = ""            # your CMS endpoint (WordPress, Strapi, Sanity...)

    # Live post watch: after publishing, re-read the live post to confirm it still says
    # what was approved. Pages are only fetched from these hosts (and their subdomains),
    # e.g. ["meridianwealth.com"]; empty = website pages are not checked.
    watch_allowed_hosts: list[str] = []
    watch_interval_hours: float = 6
    watch_days: int = 30   # how long after publishing to keep watching

    # Where the browser is sent back to after connecting a channel.
    frontend_url: str = "http://localhost:3000"

    # Encrypts channel tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Required in production. In development it is derived from JWT_SECRET.
    token_encryption_key: str = ""

    # The publish scheduler. Safe to run in every API process (jobs are claimed
    # atomically), but you can switch it off in all but one if you prefer.
    scheduler_enabled: bool = True
    scheduler_seconds: float = 30

    cors_origins: list[str] = ["http://localhost:3000"]

    @model_validator(mode="after")
    def _check(self) -> "Settings":
        if not self.database_url:
            raise ValueError(
                "DATABASE_URL is not set. Add it to backend/.env, for example "
                "mysql+pymysql://USER:PASSWORD@localhost:3306/marketing_ops?charset=utf8mb4")

        if self.app_env == "production":
            problems = []
            if len(self.jwt_secret) < 32:
                problems.append("JWT_SECRET must be at least 32 random characters")
            if self.seed_demo_data:
                problems.append("SEED_DEMO_DATA must be false")
            if not (self.database_url.startswith("postgresql") or self.database_url.startswith("mysql")):
                problems.append("DATABASE_URL must point at PostgreSQL or MySQL")
            if any(o == "*" or "localhost" in o for o in self.cors_origins):
                problems.append("CORS_ORIGINS must list your real frontend URL(s) only")
            if not self.token_encryption_key:
                problems.append("TOKEN_ENCRYPTION_KEY must be set")
            if problems:
                raise ValueError("Unsafe production settings: " + "; ".join(problems) + ".")

        if not self.jwt_secret:
            log.warning("JWT_SECRET is empty: using a random one for this run (development only)")
            self.jwt_secret = secrets.token_urlsafe(48)
        return self


@lru_cache
def get_settings() -> Settings:
    """Build the settings once and reuse them everywhere."""
    return Settings()
