from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from environment / .env (prefix ASP_)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="ASP_", extra="ignore"
    )

    canvas_base_url: str = "https://fultonschools.instructure.com"
    data_dir: Path = Path("data")

    # Browser / session
    headless: bool = True
    login_timeout_s: int = 300
    # How long our local profile keeps a session-only cookie (one with no
    # Expires/Max-Age, like Canvas's) alive across separate CLI process
    # launches. Purely a local storage detail — Canvas's server enforces
    # its own, independent session expiry regardless of this value.
    local_session_cookie_ttl_s: int = 60 * 60 * 24

    # API client
    request_max_concurrency: int = 3
    request_timeout_ms: int = 30_000
    # Slow down when Canvas's leaky-bucket quota drops below this.
    rate_limit_floor: float = 150.0

    # Filled in later, for the planner/tutor.
    anthropic_api_key: str | None = None

    @field_validator("canvas_base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "planner.db"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.browser_profile_dir, s.raw_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
