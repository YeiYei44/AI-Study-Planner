"""Shared estimate schema + prompt building.

Every backend wants the same conceptual thing (p50/p80 minutes) but
speaks a different wire format for it (Claude's tool `input_schema`,
OpenAI-style function `parameters`, a JSON-schema for grammar-constrained
local decoding). Defining it once here means the three provider
integrations can't quietly drift out of sync with each other.
"""

from __future__ import annotations

import html
import re
import sqlite3

TOOL_NAME = "report_estimate"
TOOL_DESCRIPTION = "Report an effort-time estimate for a student assignment."

ESTIMATE_SCHEMA = {
    "type": "object",
    "properties": {
        "p50_minutes": {
            "type": "integer",
            "description": "Typical (median) minutes a diligent student needs, start to finish.",
        },
        "p80_minutes": {
            "type": "integer",
            "description": "A conservative estimate covering a slower-than-typical attempt.",
        },
        "reasoning": {
            "type": "string",
            "description": "Why, in at most 12 words. Brevity matters more than completeness here.",
        },
    },
    "required": ["p50_minutes", "p80_minutes"],
}


def strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def build_prompt(course_name: str, a: sqlite3.Row, types: list[str]) -> str:
    description = strip_html(a["description_html"] or "")[:2000]
    return (
        f"Course: {course_name}\n"
        f"Assignment: {a['name']}\n"
        f"Points possible: {a['points_possible']}\n"
        f"Submission type(s): {', '.join(types) or 'unspecified'}\n"
        f"Description:\n{description or '(no description provided)'}\n\n"
        "Estimate how long a diligent high-school student needs to "
        "complete this, start to finish."
    )
