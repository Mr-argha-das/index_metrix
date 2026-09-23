"""Application configuration.

Two layers:

1. Environment / .env driven settings (this module) — the source of truth for
   secrets and unsafe-in-production defaults.
2. Runtime overrides stored in the Feather ``settings`` table (admin Settings
   page). Only a whitelist of non-secret keys can be overridden at runtime;
   see :mod:`app.database.repositories`.

Environment variables always win over unsafe defaults: when ``APP_ENV`` is
``production`` the app refuses to boot with the placeholder ``CHANGE_ME``
session secret and requires a non-default admin password setup.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VERSION = "1.0.0"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # -- application ------------------------------------------------------
    app_name: str = "INDEX MATRIX"
    app_env: str = "development"  # development | production | test
    # Canonical public origin for generated pages, sitemap, RSS and robots.txt.
    # Local development can override this through PUBLIC_BASE_URL in .env.
    public_base_url: str = "https://v1.indexmetrix.com"
    version: str = VERSION

    # -- auth -------------------------------------------------------------
    session_secret: str = "CHANGE_ME"
    session_lifetime_hours: int = Field(default=12, ge=1, le=24 * 30)
    admin_email: str = "admin@example.com"
    admin_password: str = ""  # only used on first startup to seed the admin
    # brute-force protection for /api/auth/login (per IP, sliding window)
    login_rate_limit: int = Field(default=5, ge=1, le=1000000)
    login_rate_window_seconds: int = Field(default=300, ge=5, le=3600)
    # When the app is served over a reverse proxy that hides the https scheme
    # (e.g. a live-preview tunnel), set SECURE_CLIENT=true so session/CSRF
    # cookies are issued as `Secure; SameSite=None` — required for the app to
    # work inside cross-site iframes.
    secure_client: bool = False
    # Force cookie mode: "" = auto-detect, "lax", or "none" (none implies Secure)
    cookie_samesite: str = ""
    # generic per-IP API protection (rolling 60s)
    api_rate_limit_per_minute: int = Field(default=300, ge=10, le=1000000)
    api_mutation_rate_limit_per_minute: int = Field(default=120, ge=10, le=1000000)

    # -- storage ----------------------------------------------------------
    data_dir: str = "data"

    # -- fetching / SSRF ----------------------------------------------------
    max_pdf_size_mb: int = Field(default=35, ge=1, le=200)
    http_timeout: int = Field(default=30, ge=5, le=300)  # read timeout (s)
    connect_timeout: int = Field(default=10, ge=2, le=60)  # connect timeout (s)
    max_redirects: int = Field(default=5, ge=0, le=10)
    max_upload_mb: int = Field(default=2, ge=1, le=10)
    fetch_user_agent: str = "BOT-INDEXER/1.0 (PDF discovery service)"
    # Test/dev convenience: allow loopback/private fetch targets. Never enable
    # in production — it defeats the SSRF protection.
    allow_private_targets: bool = False
    # Per-host outbound fetch rate limit (requests / minute)
    fetch_rate_per_minute: int = Field(default=10, ge=1, le=1000)
    # Max concurrent outbound fetches per host
    fetch_host_delay_seconds: float = Field(default=1.0, ge=0, le=60)
    fetch_concurrency_per_host: int = Field(default=2, ge=1, le=5)

    # -- queue ------------------------------------------------------------
    indexer_concurrency: int = Field(default=2, ge=1, le=5)
    max_retries: int = Field(default=3, ge=0, le=10)
    polling_interval_ms: int = Field(default=5000, ge=1000, le=60000)
    # base delay (seconds) for exponential retry backoff: base * 2^(attempt-1)
    retry_backoff_base: float = Field(default=5.0, ge=0.05, le=60.0)

    # -- publishing ---------------------------------------------------------
    rss_enabled: bool = True
    sitemap_enabled: bool = True
    rss_max_items: int = Field(default=50, ge=1, le=1000)

    # -- integrations -------------------------------------------------------
    google_search_console_enabled: bool = False
    # Path to a service-account JSON file, or the inline JSON document itself.
    google_service_account_json: str = ""
    # Conservative local rolling limits; do not exceed approved Google quota.
    google_indexing_daily_limit: int = Field(default=200, ge=1, le=200)
    google_indexing_minute_limit: int = Field(default=10, ge=1, le=60)
    bing_webmaster_enabled: bool = False
    bing_api_key: str = ""
    # Search-visibility observation (non-authoritative) — off by default.
    observation_enabled: bool = False

    @model_validator(mode="after")
    def _normalize(self) -> "Settings":
        self.public_base_url = (
            self.public_base_url or "https://v1.indexmetrix.com"
        ).rstrip("/")
        from urllib.parse import urlsplit
        origin = urlsplit(self.public_base_url)
        if origin.scheme not in ("http", "https") or not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ("", "/"):
            raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin without credentials, path, query or fragment.")
        self.app_env = (self.app_env or "development").lower()
        if self.app_env == "test":
            # Automated tests spin up a local PDF server; allow loopback there.
            self.allow_private_targets = True
        self.indexer_concurrency = max(1, min(5, self.indexer_concurrency))
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def secure_cookies(self) -> bool:
        return self.is_production or self.public_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


def validate_production(settings: Settings) -> list[str]:
    """Return a list of fatal production misconfigurations (empty = OK)."""
    problems: list[str] = []
    if not settings.is_production:
        return problems
    if settings.session_secret in ("", "CHANGE_ME", "secret", "dev"):
        problems.append(
            "SESSION_SECRET must be set to a long random value in production "
            "(e.g. `openssl rand -hex 32`)."
        )
    if not settings.public_base_url.startswith("https://"):
        problems.append(
            "PUBLIC_BASE_URL should be an https:// URL in production "
            "(secure cookies + HSTS)."
        )
    if settings.admin_password and settings.admin_password == "CHANGE_ME":
        problems.append(
            "ADMIN_PASSWORD must be changed from the placeholder before "
            "starting in production."
        )
    if settings.allow_private_targets:
        problems.append("ALLOW_PRIVATE_TARGETS must be false in production.")
    return problems
