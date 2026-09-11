from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/config.py -> app/ -> repo root. Anchoring .env and data_dir here
# (instead of leaving them relative, which resolves against whatever the
# current working directory happens to be) means the app behaves the same
# regardless of where it's invoked from — required once `asp` is callable
# from anywhere, not just from inside the repo. Confirmed this was a real
# gap, not theoretical: before this fix, running from /tmp silently
# resolved data_dir to /tmp/data (a fresh, disconnected "database") and
# missed .env entirely (backend silently fell back to 'claude', no key).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Runtime configuration, read from environment / .env (prefix ASP_)."""

    model_config = SettingsConfigDict(
        env_file=str(_PROJECT_ROOT / ".env"), env_prefix="ASP_", extra="ignore"
    )

    canvas_base_url: str = "https://fultonschools.instructure.com"
    data_dir: Path = _PROJECT_ROOT / "data"

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

    # Which chat backend `estimate-llm` and the tutor use: "claude",
    # "openai_compat" (Groq/Mistral/OpenRouter/GitHub Models/a local
    # Ollama server — anything OpenAI-compatible), or "local"
    # (llama-cpp-python, fully offline). See app/planner/llm_backends/.
    llm_backend: str = "claude"
    # Overrides that backend's own default concurrency when set —
    # local inference should generally stay low (compute-bound, not
    # I/O-bound); cloud backends can go higher.
    llm_estimate_max_concurrency: int | None = None

    # -- claude --
    anthropic_api_key: str | None = None
    # Haiku 4.5, deliberately: effort estimation is a simple, high-volume,
    # low-stakes judgment call — the textbook case for a cheap/fast model.
    llm_estimate_model: str = "claude-haiku-4-5-20251001"
    # The tutor needs actual pedagogical reasoning, not a number guess —
    # Sonnet-tier by default, distinct from the estimation model above.
    llm_tutor_model: str = "claude-sonnet-5"

    # -- openai_compat (Groq, Mistral, OpenRouter, GitHub Models, Ollama...) --
    openai_compat_base_url: str | None = None
    openai_compat_api_key: str | None = None
    # openai/gpt-oss-20b, not llama-3.1-8b-instant: Groq deprecated the
    # latter in June 2026 and recommends this as the migration target —
    # confirmed against Groq's live /models endpoint, not assumed.
    openai_compat_model: str = "openai/gpt-oss-20b"
    # Falls back to openai_compat_model if unset — set this to a bigger
    # model (e.g. openai/gpt-oss-120b) for better tutoring quality while
    # keeping estimation on the smaller/faster one.
    openai_compat_tutor_model: str | None = None

    # -- local (llama-cpp-python) --
    llamacpp_model_path: str | None = None
    llamacpp_n_ctx: int = 4096

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
