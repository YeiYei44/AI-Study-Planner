"""Wire-format tests for each backend — verifying request shape and
response parsing against fake SDK clients, no real network calls."""

import asyncio
import json
import sys

import pytest

from app.planner.llm_backends.schema import build_prompt, strip_html


def test_strip_html_removes_tags_and_unescapes_entities():
    assert strip_html("<p>Read <b>chapter&nbsp;3</b> &amp; answer.</p>") == "Read chapter 3 & answer."


def test_build_prompt_includes_course_and_assignment_fields():
    row = {"name": "Essay 1", "description_html": "<p>Write 500 words</p>", "points_possible": 100}
    prompt = build_prompt("AP English", row, ["online_upload"])
    assert "AP English" in prompt
    assert "Essay 1" in prompt
    assert "100" in prompt
    assert "Write 500 words" in prompt
    assert "online_upload" in prompt


def test_build_prompt_handles_missing_description():
    row = {"name": "Quiz", "description_html": None, "points_possible": 10}
    prompt = build_prompt("Math", row, [])
    assert "no description provided" in prompt


# -- ClaudeBackend ------------------------------------------------------


class _FakeToolBlock:
    type = "tool_use"

    def __init__(self, input_):
        self.input = input_


class _FakeAnthropicResponse:
    def __init__(self, p50, p80):
        self.content = [_FakeToolBlock({"p50_minutes": p50, "p80_minutes": p80})]


class _FakeAnthropicMessages:
    def __init__(self):
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeAnthropicResponse(42, 70)


class _FakeAsyncAnthropic:
    def __init__(self, api_key=None):
        self.messages = _FakeAnthropicMessages()


def test_claude_backend(monkeypatch):
    monkeypatch.setattr("app.planner.llm_backends.claude.anthropic.AsyncAnthropic", _FakeAsyncAnthropic)
    from app.planner.llm_backends.claude import ClaudeBackend

    backend = ClaudeBackend(api_key="fake", model="claude-haiku-4-5-20251001")
    row = {"name": "HW", "description_html": "<p>x</p>", "points_possible": 10}
    p50, p80 = asyncio.run(backend.estimate("Course", row, ["online_upload"]))
    assert (p50, p80) == (42, 70)
    assert backend._client.messages.last_kwargs["tool_choice"] == {
        "type": "tool",
        "name": "report_estimate",
    }


# -- OpenAICompatBackend --------------------------------------------------


class _FakeToolCallFunction:
    def __init__(self, arguments):
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, arguments):
        self.function = _FakeToolCallFunction(arguments)


class _FakeChoice:
    def __init__(self, arguments):
        self.message = type("M", (), {"tool_calls": [_FakeToolCall(arguments)]})()


class _FakeOpenAIResponse:
    def __init__(self, p50, p80):
        self.choices = [_FakeChoice(json.dumps({"p50_minutes": p50, "p80_minutes": p80}))]


class _FakeChatCompletions:
    def __init__(self):
        self.last_kwargs = None

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeOpenAIResponse(33, 55)


class _FakeAsyncOpenAI:
    def __init__(self, base_url=None, api_key=None):
        self.base_url = base_url
        self.api_key = api_key
        self.chat = type("Chat", (), {"completions": _FakeChatCompletions()})()


def test_openai_compat_backend(monkeypatch):
    monkeypatch.setattr("app.planner.llm_backends.openai_compat.openai.AsyncOpenAI", _FakeAsyncOpenAI)
    from app.planner.llm_backends.openai_compat import OpenAICompatBackend

    backend = OpenAICompatBackend(
        base_url="https://api.groq.com/openai/v1", api_key="fake", model="llama-3.1-8b-instant"
    )
    row = {"name": "HW", "description_html": "<p>x</p>", "points_possible": 10}
    p50, p80 = asyncio.run(backend.estimate("Course", row, ["online_upload"]))
    assert (p50, p80) == (33, 55)
    kwargs = backend._client.chat.completions.last_kwargs
    assert kwargs["tool_choice"] == {"type": "function", "function": {"name": "report_estimate"}}
    assert kwargs["tools"][0]["function"]["name"] == "report_estimate"


def test_openai_compat_backend_defaults_api_key_when_empty(monkeypatch):
    monkeypatch.setattr("app.planner.llm_backends.openai_compat.openai.AsyncOpenAI", _FakeAsyncOpenAI)
    from app.planner.llm_backends.openai_compat import OpenAICompatBackend

    backend = OpenAICompatBackend(base_url="http://localhost:11434/v1", api_key="", model="llama3.1:8b")
    assert backend._client.api_key == "not-needed"  # local servers ignore it, but the SDK wants a string


# -- LocalLlamaCppBackend -------------------------------------------------


class _FakeLlama:
    def __init__(self, model_path, n_ctx, verbose):
        self.model_path = model_path
        self.last_kwargs = None

    def create_chat_completion(self, **kwargs):
        self.last_kwargs = kwargs
        return {"choices": [{"message": {"content": json.dumps({"p50_minutes": 25, "p80_minutes": 45})}}]}


def test_local_llamacpp_backend(monkeypatch):
    # local_llamacpp.py imports llama_cpp lazily inside __init__ (it's a
    # heavy optional dependency, not installed here) — so the fake needs
    # to be in sys.modules *before* construction, not patched onto the
    # already-imported module the way the other backends' SDKs are.
    fake_module = type("FakeLlamaCppModule", (), {"Llama": _FakeLlama})
    monkeypatch.setitem(sys.modules, "llama_cpp", fake_module)
    from app.planner.llm_backends.local_llamacpp import LocalLlamaCppBackend

    backend = LocalLlamaCppBackend(model_path="/fake/model.gguf")
    row = {"name": "HW", "description_html": "<p>x</p>", "points_possible": 10}
    p50, p80 = asyncio.run(backend.estimate("Course", row, ["online_upload"]))
    assert (p50, p80) == (25, 45)
    assert backend._llm.last_kwargs["response_format"]["type"] == "json_object"


def test_local_llamacpp_backend_default_concurrency_is_one():
    from app.planner.llm_backends.local_llamacpp import LocalLlamaCppBackend

    assert LocalLlamaCppBackend.default_max_concurrency == 1
