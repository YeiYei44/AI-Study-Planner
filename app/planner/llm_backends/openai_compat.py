"""Generic backend for any OpenAI-compatible chat-completions endpoint.

One implementation covers Groq, Mistral's La Plateforme, OpenRouter,
GitHub Models, and a local Ollama server if you'd rather run that than
llama-cpp-python — they all speak the same tools/tool_choice wire
format. Only base_url/api_key/model differ per provider; see
.env.example for the values each one wants.

Groq is the default recommendation: a genuinely generous free tier
(thousands of requests/day on an 8B model), fast LPU inference, and
proper OpenAI-compatible tool calling.
"""

from __future__ import annotations

import json
import sqlite3

import openai

from app.planner.llm_backends.schema import ESTIMATE_SCHEMA, TOOL_DESCRIPTION, TOOL_NAME, build_prompt


class OpenAICompatBackend:
    default_max_concurrency = 4

    def __init__(self, base_url: str, api_key: str, model: str):
        # A local server (Ollama) generally ignores the key but the SDK
        # requires a non-empty string.
        self._client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed")
        self._model = model

    async def estimate(
        self, course_name: str, a: sqlite3.Row, types: list[str]
    ) -> tuple[int, int]:
        resp = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=500,  # room for a verbose model's `reasoning` field without truncating mid-JSON
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "parameters": ESTIMATE_SCHEMA,
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
            messages=[{"role": "user", "content": build_prompt(course_name, a, types)}],
        )
        call = resp.choices[0].message.tool_calls[0]
        data = json.loads(call.function.arguments)
        return int(data["p50_minutes"]), int(data["p80_minutes"])

    async def answer(self, prompt: str) -> str:
        """Free-form completion for the tutor — no forced tool schema,
        just prose (with inline citations, per the prompt's instructions)."""
        resp = await self._client.chat.completions.create(
            model=self._model,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.choices[0].message.content or ""
