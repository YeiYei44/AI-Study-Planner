"""Turn raw Canvas API payloads into canonical row dicts for the DB.

Pure functions — no I/O, no DB. Keeping this separate from db/sync.py
means the same transform will work unchanged once a second adapter
(ICS, manual upload) needs to produce rows in the same shape.
"""

from __future__ import annotations

import json
from typing import Any

SOURCE = "canvas_session"


def normalize_course(raw: dict[str, Any], fetched_at: str) -> dict[str, Any]:
    term = raw.get("term") or {}
    return {
        "id": raw["id"],
        "name": raw.get("name") or raw.get("course_code") or f"Course {raw['id']}",
        "course_code": raw.get("course_code"),
        "term": term.get("name"),
        "workflow_state": raw.get("workflow_state"),
        "source": SOURCE,
        "raw_json": json.dumps(raw),
        "fetched_at": fetched_at,
    }


def normalize_assignment(
    raw: dict[str, Any], course_id: int, fetched_at: str
) -> dict[str, Any]:
    return {
        "id": raw["id"],
        "course_id": course_id,
        "name": raw.get("name") or f"Assignment {raw['id']}",
        "description_html": raw.get("description"),
        "due_at": raw.get("due_at"),
        "points_possible": raw.get("points_possible"),
        "submission_types": json.dumps(raw.get("submission_types") or []),
        "html_url": raw.get("html_url"),
        "workflow_state": raw.get("workflow_state"),
        "source": SOURCE,
        "raw_json": json.dumps(raw),
        "fetched_at": fetched_at,
    }
