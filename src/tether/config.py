"""Runtime configuration, read from the environment (or a .env file in dev).

Secrets (the encryption key, OAuth client secret, session secret, later the
ICS URL) live only here, never in the database or the repo.
"""

from functools import lru_cache
from pathlib import Path

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

    initial_sync_days: int = 14
    gmail_poll_seconds: int = 60

    # Directory holding the built PWA (web/dist). Served at / when present.
    static_dir: Path = Path("web/dist")

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/auth/google/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
