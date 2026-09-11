"""Effort estimation orchestration: caching and the bulk run loop.

Backend-agnostic — doesn't know or care whether estimates are coming
from Claude, a free OpenAI-compatible API, or a fully local model (see
llm_backends/). Content-hash caching means an unchanged assignment costs
nothing on the next run regardless of which backend is configured.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import sqlite3
from typing import Any, Protocol

from app.config import Settings

# Two retry classes, found by actually running this against the real
# Groq API at bulk scale rather than assumed upfront:
#
# * Rate limiting (HTTP 429) — free-tier providers like Groq have real
#   per-minute token limits that a bulk run at any real concurrency will
#   hit. Same shape as CanvasClient's own backoff: exponential, several
#   attempts, since the fix is genuinely "wait for the window to clear."
# * Everything else gets one quick, undelayed extra attempt. Observed
#   directly against Groq's free `openai/gpt-oss-20b`: it occasionally
#   emits a malformed tool call ("Failed to parse tool call arguments as
#   JSON") that has nothing to do with token budget — a fresh attempt on
#   the exact same request succeeded every time this was checked. Smaller
#   free models don't follow structured-output format 100% of the time.
#   A permanent failure (bad auth, a real schema mismatch) just costs one
#   cheap extra call before still being correctly reported as an error.
_MAX_RETRIES_RATE_LIMIT = 4
_MAX_RETRIES_OTHER = 3
_BASE_DELAY_S = 3.0
_MAX_DELAY_S = 30.0


def _is_rate_limited(e: Exception) -> bool:
    return getattr(e, "status_code", None) == 429


class BackendConfigError(RuntimeError):
    """The configured ASP_LLM_BACKEND is missing something it needs."""


class EstimateBackend(Protocol):
    """What a backend provides — both the estimation role this was
    originally built for and the tutor's free-form ``answer()``. One
    protocol, since every real backend implements both; the name's kept
    for the sake of not touching every existing reference to it."""

    default_max_concurrency: int

    async def estimate(
        self, course_name: str, assignment_row: sqlite3.Row, types: list[str]
    ) -> tuple[int, int]: ...

    async def answer(self, prompt: str) -> str: ...


def make_backend(settings: Settings, *, tutor: bool = False) -> EstimateBackend:
    """Construct the configured backend. Lazy imports throughout — a
    command that never touches the LLM shouldn't need anthropic, openai,
    or llama-cpp-python installed at all.

    ``tutor=True`` picks each provider's tutor-tier model instead of its
    estimation-tier one (Sonnet vs Haiku for Claude; a configurable
    bigger Groq model, falling back to the same one, for openai_compat;
    local has no second tier without a second model file, so it's
    unaffected).
    """
    backend = settings.llm_backend

    if backend == "claude":
        if not settings.anthropic_api_key:
            raise BackendConfigError(
                "ASP_LLM_BACKEND=claude needs ASP_ANTHROPIC_API_KEY set."
            )
        from app.planner.llm_backends.claude import ClaudeBackend

        model = settings.llm_tutor_model if tutor else settings.llm_estimate_model
        return ClaudeBackend(settings.anthropic_api_key, model)

    if backend == "openai_compat":
        if not settings.openai_compat_base_url:
            raise BackendConfigError(
                "ASP_LLM_BACKEND=openai_compat needs ASP_OPENAI_COMPAT_BASE_URL set "
                "(e.g. https://api.groq.com/openai/v1)."
            )
        from app.planner.llm_backends.openai_compat import OpenAICompatBackend

        model = settings.openai_compat_model
        if tutor and settings.openai_compat_tutor_model:
            model = settings.openai_compat_tutor_model
        return OpenAICompatBackend(
            settings.openai_compat_base_url,
            settings.openai_compat_api_key or "",
            model,
        )

    if backend == "local":
        if not settings.llamacpp_model_path:
            raise BackendConfigError(
                "ASP_LLM_BACKEND=local needs ASP_LLAMACPP_MODEL_PATH set to a "
                ".gguf file. Install the optional dependency first: "
                'pip install -e ".[local]"'
            )
        from app.planner.llm_backends.local_llamacpp import LocalLlamaCppBackend

        return LocalLlamaCppBackend(settings.llamacpp_model_path, settings.llamacpp_n_ctx)

    raise BackendConfigError(
        f"Unknown ASP_LLM_BACKEND={backend!r}. Expected claude, openai_compat, or local."
    )


def content_hash(assignment_row: sqlite3.Row) -> str:
    """Changes iff anything the estimate actually depends on changes."""
    key = "|".join(
        [
            assignment_row["name"] or "",
            assignment_row["description_html"] or "",
            str(assignment_row["points_possible"]),
            assignment_row["submission_types"] or "",
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()


def _upsert(conn: sqlite3.Connection, assignment_id: int, p50: int, p80: int, chash: str) -> None:
    conn.execute(
        """
        INSERT INTO estimates (assignment_id, minutes, minutes_p80, basis, content_hash)
        VALUES (?, ?, ?, 'llm', ?)
        ON CONFLICT(assignment_id) DO UPDATE SET
            minutes=excluded.minutes, minutes_p80=excluded.minutes_p80,
            basis=excluded.basis, content_hash=excluded.content_hash
        """,
        (assignment_id, p50, p80, chash),
    )


async def run_llm_estimates(
    conn: sqlite3.Connection,
    backend: EstimateBackend,
    max_concurrency: int | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    """Estimate every eligible published assignment. Skips ``basis='user'``
    rows (your call always wins) and anything already estimated since it
    last changed (matching content_hash). Returns counts."""
    rows = conn.execute(
        "SELECT a.*, c.name AS course_name FROM assignments a "
        "JOIN courses c ON c.id = a.course_id WHERE a.workflow_state = 'published'"
    ).fetchall()

    counts = {"estimated": 0, "skipped_cached": 0, "skipped_user": 0, "errors": 0}
    todo: list[tuple[sqlite3.Row, str]] = []
    for a in rows:
        existing = conn.execute(
            "SELECT basis, content_hash FROM estimates WHERE assignment_id = ?", (a["id"],)
        ).fetchone()
        if existing and existing["basis"] == "user":
            counts["skipped_user"] += 1
            continue
        chash = content_hash(a)
        if existing and existing["content_hash"] == chash:
            counts["skipped_cached"] += 1
            continue
        todo.append((a, chash))

    if limit is not None:
        todo = todo[:limit]

    concurrency = max_concurrency or getattr(backend, "default_max_concurrency", 4)
    sem = asyncio.Semaphore(concurrency)

    async def _worker(a: sqlite3.Row, chash: str) -> tuple[str, int, Any]:
        types = json.loads(a["submission_types"] or "[]")
        async with sem:
            attempt = 0
            while True:
                attempt += 1
                try:
                    p50, p80 = await backend.estimate(a["course_name"], a, types)
                    return ("ok", a["id"], (p50, p80, chash))
                except Exception as e:
                    rate_limited = _is_rate_limited(e)
                    limit = _MAX_RETRIES_RATE_LIMIT if rate_limited else _MAX_RETRIES_OTHER
                    if attempt >= limit:
                        return ("error", a["id"], None)
                    if rate_limited:
                        delay = min(_BASE_DELAY_S * (2 ** (attempt - 1)), _MAX_DELAY_S)
                        await asyncio.sleep(delay + random.uniform(0, 1))
                    # else: a likely generation glitch — retry immediately, no
                    # backoff, since waiting doesn't make the model more careful.

    results = await asyncio.gather(*(_worker(a, chash) for a, chash in todo))
    for status, aid, payload in results:
        if status == "error":
            counts["errors"] += 1
        else:
            p50, p80, chash = payload
            _upsert(conn, aid, p50, p80, chash)
            counts["estimated"] += 1
    conn.commit()
    return counts
