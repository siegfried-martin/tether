"""Runtime configuration, read from the environment (or a .env file in dev).

Secrets (the encryption key, OAuth client secret, session secret, later the
ICS URL) live only here, never in the database or the repo.
"""

from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TETHER_", env_file=".env", extra="ignore")

    db_path: Path = Path("tether.db")
    # Public base URL, used to build the OAuth redirect URI.
    base_url: str = "http://localhost:8000"

    # Where `python -m tether serve` listens. In production this sits behind a
    # reverse proxy, so the default bind is loopback only.
    host: str = "127.0.0.1"
    port: int = 8000

    # Fernet key that encrypts credential.ciphertext.
    encryption_key: str
    # Signs the session cookie.
    session_secret: str

    google_client_id: str
    google_client_secret: str

    # Single-user login: only this Google identity may sign in.
    owner_email: str
    # Google account ID ("sub") of the owner. When set, login must match it too.
    owner_sub: str | None = None

    # Hosts allowed as OAuth redirect targets for the MCP connector: Claude's
    # callbacks, plus loopback for Claude Code. Comma-separated.
    mcp_redirect_hosts: str = "claude.ai,claude.com,localhost,127.0.0.1"

    # The Android app: its package name, and the SHA-256 fingerprints of the keys it is signed with
    # (comma-separated, AA:BB:...). The fingerprints go in /.well-known/assetlinks.json, which lets the
    # app open tether.marstol.com full screen. Until they are set, that file is a 404.
    android_package: str = "com.marstol.tether"
    android_cert_sha256: str = ""

    # The owner's time zone (IANA name). Decides what "today" means for due dates
    # and the agenda, and how the work digest's times are read.
    timezone: str = "UTC"

    # Label of the account that holds work-digest items (created on the first ingest).
    digest_label: str = "Work"

    # Calendar hub: the Google account (its mailbox address) and the calendar ID on it that
    # the hub writes to. Create that calendar by hand; until both are set, the agenda
    # still works inside Tether but nothing is written to Google.
    hub_account: str | None = None
    hub_calendar_id: str | None = None
    # A published ICS feed (e.g. a work Outlook calendar). A secret: anyone with it can read the calendar.
    ics_url: str | None = None
    ics_label: str = "Work calendar"
    calendar_google_minutes: int = 5
    calendar_ics_minutes: int = 15

    initial_sync_days: int = 14
    gmail_poll_seconds: int = 60

    # Directory holding the built PWA (web/dist). Served at / when present.
    static_dir: Path = Path("web/dist")

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        ZoneInfo(value)  # raises for an unknown zone name
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def today(self) -> date:
        return datetime.now(self.tz).date()

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/auth/google/callback"

    @property
    def app_redirect_uri(self) -> str:
        """Where sign-in codes for the Android app go: the app's own URL scheme (RFC 8252)."""
        return f"{self.android_package}:/oauth2redirect"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
