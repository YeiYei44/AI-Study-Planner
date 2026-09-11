import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.planner.llm_estimate import (
    BackendConfigError,
    content_hash,
    make_backend,
    run_llm_estimates,
)


def test_content_hash_stable_for_same_content():
    row = {"name": "HW1", "description_html": "<p>x</p>", "points_possible": 10, "submission_types": "[]"}
    assert content_hash(row) == content_hash(dict(row))


def test_content_hash_changes_when_content_changes():
    row = {"name": "HW1", "description_html": "<p>x</p>", "points_possible": 10, "submission_types": "[]"}
    changed = {**row, "points_possible": 20}
    assert content_hash(row) != content_hash(changed)


# -- make_backend() config validation --------------------------------------


def test_make_backend_claude_requires_api_key():
    with pytest.raises(BackendConfigError, match="ASP_ANTHROPIC_API_KEY"):
        make_backend(Settings(llm_backend="claude", anthropic_api_key=None))


def test_make_backend_claude_constructs_with_key():
    backend = make_backend(Settings(llm_backend="claude", anthropic_api_key="sk-fake"))
    assert backend.__class__.__name__ == "ClaudeBackend"


def test_make_backend_openai_compat_requires_base_url():
    with pytest.raises(BackendConfigError, match="ASP_OPENAI_COMPAT_BASE_URL"):
        make_backend(Settings(llm_backend="openai_compat", openai_compat_base_url=None))


def test_make_backend_openai_compat_constructs_with_base_url():
    backend = make_backend(
        Settings(llm_backend="openai_compat", openai_compat_base_url="https://api.groq.com/openai/v1")
    )
    assert backend.__class__.__name__ == "OpenAICompatBackend"


def test_make_backend_local_requires_model_path():
    with pytest.raises(BackendConfigError, match="ASP_LLAMACPP_MODEL_PATH"):
        make_backend(Settings(llm_backend="local", llamacpp_model_path=None))


def test_make_backend_unknown_backend_rejected():
    with pytest.raises(BackendConfigError, match="nonsense"):
        make_backend(Settings(llm_backend="nonsense"))


# -- run_llm_estimates(), against a trivial fake backend --------------------


class _FakeBackend:
    default_max_concurrency = 4

    def __init__(self, p50=42, p80=70, raise_error=False):
        self.p50, self.p80, self.raise_error = p50, p80, raise_error
        self.calls: list[str] = []

    async def estimate(self, course_name, a, types):
        self.calls.append(a["name"])
        if self.raise_error:
            raise RuntimeError("boom")
        return self.p50, self.p80


class _RateLimitError(Exception):
    status_code = 429


class _FlakyBackend:
    """Fails with a 429-shaped error N times, then succeeds."""

    default_max_concurrency = 4

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.attempts = 0

    async def estimate(self, course_name, a, types):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise _RateLimitError("rate limited")
        return 42, 70


class _FlakyNonRateLimitBackend:
    """Fails with a plain (non-429) error N times, then succeeds."""

    default_max_concurrency = 4

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.attempts = 0

    async def estimate(self, course_name, a, types):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise RuntimeError("malformed tool call")
        return 42, 70


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def _insert_assignment(conn, aid, name="HW", points=50, desc="<p>do it</p>"):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'C', 's', '{}', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, description_html, points_possible, workflow_state,
             submission_types, source, raw_json, fetched_at)
        VALUES (?, 1, ?, ?, ?, 'published', '["online_upload"]', 's', '{}', 't')
        """,
        (aid, name, desc, points),
    )
    conn.commit()


def test_new_assignment_gets_estimated(conn):
    _insert_assignment(conn, 1)
    backend = _FakeBackend(p50=42, p80=70)
    counts = asyncio.run(run_llm_estimates(conn, backend))
    assert counts == {"estimated": 1, "skipped_cached": 0, "skipped_user": 0, "errors": 0}
    row = conn.execute("SELECT * FROM estimates WHERE assignment_id = 1").fetchone()
    assert (row["minutes"], row["minutes_p80"], row["basis"]) == (42, 70, "llm")
    assert row["content_hash"]


def test_unchanged_assignment_skipped_on_rerun(conn):
    _insert_assignment(conn, 1)
    backend = _FakeBackend()
    asyncio.run(run_llm_estimates(conn, backend))
    counts = asyncio.run(run_llm_estimates(conn, backend))
    assert counts["estimated"] == 0
    assert counts["skipped_cached"] == 1
    assert backend.calls == ["HW"]  # only called once across both runs


def test_changed_assignment_gets_reestimated(conn):
    _insert_assignment(conn, 1, points=50)
    backend = _FakeBackend()
    asyncio.run(run_llm_estimates(conn, backend))
    conn.execute("UPDATE assignments SET points_possible = 999 WHERE id = 1")
    conn.commit()
    counts = asyncio.run(run_llm_estimates(conn, backend))
    assert counts["estimated"] == 1
    assert counts["skipped_cached"] == 0


def test_user_estimate_never_overwritten(conn):
    from app.planner.estimate import set_estimate

    _insert_assignment(conn, 1)
    set_estimate(conn, 1, 999, basis="user")
    counts = asyncio.run(run_llm_estimates(conn, _FakeBackend()))
    assert counts == {"estimated": 0, "skipped_cached": 0, "skipped_user": 1, "errors": 0}
    row = conn.execute("SELECT minutes, basis FROM estimates WHERE assignment_id = 1").fetchone()
    assert (row["minutes"], row["basis"]) == (999, "user")


def test_limit_caps_how_many_get_processed(conn):
    for i in range(1, 6):
        _insert_assignment(conn, i, name=f"HW{i}")
    counts = asyncio.run(run_llm_estimates(conn, _FakeBackend(), limit=2))
    assert counts["estimated"] == 2


def test_backend_error_counted_not_raised(conn):
    _insert_assignment(conn, 1)
    counts = asyncio.run(run_llm_estimates(conn, _FakeBackend(raise_error=True)))
    assert counts == {"estimated": 0, "skipped_cached": 0, "skipped_user": 0, "errors": 1}


# -- rate-limit retry (discovered against the real Groq API under bulk load) --


def test_rate_limited_call_retries_and_succeeds(conn, monkeypatch):
    async def _noop_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("app.planner.llm_estimate.asyncio.sleep", _noop_sleep)
    _insert_assignment(conn, 1)
    backend = _FlakyBackend(fail_times=2)  # fails twice, succeeds on the 3rd attempt
    counts = asyncio.run(run_llm_estimates(conn, backend))
    assert counts == {"estimated": 1, "skipped_cached": 0, "skipped_user": 0, "errors": 0}
    assert backend.attempts == 3


def test_rate_limited_call_gives_up_after_max_retries(conn, monkeypatch):
    async def _noop_sleep(*_a, **_k):
        return None

    monkeypatch.setattr("app.planner.llm_estimate.asyncio.sleep", _noop_sleep)
    _insert_assignment(conn, 1)
    backend = _FlakyBackend(fail_times=99)  # never recovers
    counts = asyncio.run(run_llm_estimates(conn, backend))
    assert counts["errors"] == 1
    assert backend.attempts == 4  # _MAX_RETRIES, no more


def test_non_rate_limit_error_retried_then_gives_up(conn):
    _insert_assignment(conn, 1)
    backend = _FakeBackend(raise_error=True)  # a plain RuntimeError, not 429-shaped
    counts = asyncio.run(run_llm_estimates(conn, backend))
    assert counts["errors"] == 1
    assert len(backend.calls) == 3  # _MAX_RETRIES_OTHER, not the 429 count (4)


def test_transient_non_rate_limit_error_recovers_on_retry(conn):
    # Observed for real: a smaller free model occasionally emits a
    # malformed tool call for no rate-limit-related reason; retrying the
    # identical request succeeded every time this was checked against
    # Groq. Worth one quick, undelayed retry rather than giving up.
    backend = _FlakyNonRateLimitBackend(fail_times=1)
    _insert_assignment(conn, 1)
    counts = asyncio.run(run_llm_estimates(conn, backend))
    assert counts == {"estimated": 1, "skipped_cached": 0, "skipped_user": 0, "errors": 0}
    assert backend.attempts == 2
