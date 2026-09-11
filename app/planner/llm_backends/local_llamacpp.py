"""Fully local backend via llama-cpp-python — no network at all, which
matters specifically on a locked-down device: it means this can run
directly there with no Codespace and no question of whether some
provider's domain is blocked.

Deliberately not Ollama, despite Ollama's nicer tool-calling ergonomics:
Ollama ships as a new standalone binary plus a background server
process, and this project has already watched this exact device kill or
flag every new executable it's been handed (Playwright's Chromium, the
`asp.exe` console-script wrapper — see docs/DESIGN.md). llama-cpp-python
is a Python package with a compiled extension, loaded in-process by the
same python.exe that's already trusted here — nothing new for anything
to flag. If Ollama turns out to be fine on your machine, `openai_compat`
also happens to work against it (it exposes an OpenAI-compatible
endpoint) — no separate backend needed for that.

Point ASP_LLAMACPP_MODEL_PATH at a local .gguf file — an 8B-class
instruct model with decent JSON-following (Llama 3.1 8B Instruct and
Qwen2.5 7B Instruct both work well for this). Uses
`response_format={"type": "json_object", "schema": ...}`, which
llama-cpp-python compiles into a GBNF grammar internally and enforces at
the sampler level — the output is *guaranteed* syntactically valid JSON
matching the schema, not just "usually."
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

from app.planner.llm_backends.schema import build_prompt

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "p50_minutes": {"type": "integer"},
        "p80_minutes": {"type": "integer"},
    },
    "required": ["p50_minutes", "p80_minutes"],
}

_SYSTEM_PROMPT = (
    'Respond with only a JSON object: {"p50_minutes": <int>, "p80_minutes": <int>}. '
    "No other text."
)


class LocalLlamaCppBackend:
    # Local inference is compute-bound, not I/O-bound — running several
    # "concurrent" calls on typical laptop hardware contends for the same
    # CPU/GPU rather than speeding anything up.
    default_max_concurrency = 1

    def __init__(self, model_path: str, n_ctx: int = 4096):
        import llama_cpp  # lazy: only this backend needs it, and it's a heavy optional install

        self._llm = llama_cpp.Llama(model_path=model_path, n_ctx=n_ctx, verbose=False)

    def _estimate_sync(self, course_name: str, a: sqlite3.Row, types: list[str]) -> tuple[int, int]:
        resp = self._llm.create_chat_completion(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(course_name, a, types)},
            ],
            response_format={"type": "json_object", "schema": _RESPONSE_SCHEMA},
            max_tokens=400,  # room for a verbose model's `reasoning` field without truncating mid-JSON
        )
        data = json.loads(resp["choices"][0]["message"]["content"])
        return int(data["p50_minutes"]), int(data["p80_minutes"])

    async def estimate(
        self, course_name: str, a: sqlite3.Row, types: list[str]
    ) -> tuple[int, int]:
        # create_chat_completion is a blocking, CPU/GPU-bound call with no
        # native asyncio support — off the event loop, onto a thread.
        return await asyncio.to_thread(self._estimate_sync, course_name, a, types)

    def _answer_sync(self, prompt: str) -> str:
        resp = self._llm.create_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=800,
        )
        return resp["choices"][0]["message"]["content"] or ""

    async def answer(self, prompt: str) -> str:
        """Free-form completion for the tutor — no grammar constraint,
        just prose (with inline citations, per the prompt's instructions)."""
        return await asyncio.to_thread(self._answer_sync, prompt)
