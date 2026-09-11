"""Claude backend. Not the default (cost — see ASP_LLM_BACKEND) but kept
fully working: same tool-forced structured output as before, just moved
here so it can sit alongside the other backends behind one interface.
Still the natural choice once the project's ready to spend on it, and
the tutor (a harder task than effort estimation) will likely want
Sonnet-tier quality regardless of what estimation ends up using.
"""

from __future__ import annotations

import sqlite3

import anthropic

from app.planner.llm_backends.schema import ESTIMATE_SCHEMA, TOOL_DESCRIPTION, TOOL_NAME, build_prompt


class ClaudeBackend:
    default_max_concurrency = 4

    def __init__(self, api_key: str, model: str):
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def estimate(
        self, course_name: str, a: sqlite3.Row, types: list[str]
    ) -> tuple[int, int]:
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=500,  # room for a verbose model's `reasoning` field without truncating mid-JSON
            tools=[
                {
                    "name": TOOL_NAME,
                    "description": TOOL_DESCRIPTION,
                    "input_schema": ESTIMATE_SCHEMA,
                }
            ],
            tool_choice={"type": "tool", "name": TOOL_NAME},
            messages=[{"role": "user", "content": build_prompt(course_name, a, types)}],
        )
        block = next(b for b in resp.content if b.type == "tool_use")
        data = block.input
        return int(data["p50_minutes"]), int(data["p80_minutes"])

    async def answer(self, prompt: str) -> str:
        """Free-form completion for the tutor — no forced tool schema,
        just prose (with inline citations, per the prompt's instructions)."""
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in resp.content if b.type == "text")
