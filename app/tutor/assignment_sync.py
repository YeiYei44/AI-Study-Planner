"""Keep one searchable `materials` row (kind='assignment') per published
assignment, so the tutor can answer questions about assignment content —
descriptions, due dates, points — through the exact same retrieval
pipeline as uploaded files, rather than a second parallel system with its
own citation format. See app/tutor/qa.py for the other half: an
always-included upcoming-assignments digest, since embeddings alone are
known-weak at exact date/number lookups (docs/DESIGN.md).
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.planner.llm_backends.schema import strip_html
from app.tutor.chunk import chunk_text
from app.tutor.embed import embed_texts, serialize_embedding


def _content_hash(a: sqlite3.Row) -> str:
    """Changes iff anything the synthesized material text depends on
    changes — same pattern as llm_estimate.py's content_hash."""
    key = "|".join(
        [
            a["name"] or "",
            a["description_html"] or "",
            a["due_at"] or "",
            str(a["points_possible"] or ""),
        ]
    )
    return hashlib.sha256(key.encode()).hexdigest()


def _material_text(course_name: str, a: sqlite3.Row) -> str:
    due = a["due_at"] or "no due date set"
    points = a["points_possible"]
    points_str = f"{points:g} points possible" if points is not None else "points not specified"
    description = strip_html(a["description_html"] or "") or "(no description provided)"
    return (
        f"Assignment: {a['name']}\n"
        f"Course: {course_name}\n"
        f"Due: {due}\n"
        f"{points_str}\n\n"
        f"{description}"
    )


@dataclass
class AssignmentSyncResult:
    embedded: int
    skipped_cached: int
    removed: int


def sync_assignment_materials(conn: sqlite3.Connection) -> AssignmentSyncResult:
    """Re-chunk/embed only assignments whose content actually changed
    (content_hash), and drop materials for assignments that are no
    longer published or no longer exist — called after every `sync`,
    since it's local-only (fastembed, no network/API cost), unlike
    estimate-llm which stays a separate opt-in step."""
    rows = conn.execute(
        "SELECT a.*, c.name AS course_name FROM assignments a "
        "JOIN courses c ON c.id = a.course_id WHERE a.workflow_state = 'published'"
    ).fetchall()

    result = AssignmentSyncResult(embedded=0, skipped_cached=0, removed=0)
    now = datetime.now(timezone.utc).isoformat()
    live_ids = {a["id"] for a in rows}

    for a in rows:
        chash = _content_hash(a)
        existing = conn.execute(
            "SELECT id, content_hash FROM materials WHERE assignment_id = ? AND kind = 'assignment'",
            (a["id"],),
        ).fetchone()
        if existing and existing["content_hash"] == chash:
            result.skipped_cached += 1
            continue

        if existing:
            conn.execute("DELETE FROM chunks WHERE material_id = ?", (existing["id"],))
            conn.execute("DELETE FROM materials WHERE id = ?", (existing["id"],))

        pieces = chunk_text(_material_text(a["course_name"], a))
        cur = conn.execute(
            "INSERT INTO materials (course_id, kind, title, source_path, added_at, assignment_id, content_hash) "
            "VALUES (?, 'assignment', ?, NULL, ?, ?, ?)",
            (a["course_id"], a["name"], now, a["id"], chash),
        )
        material_id = cur.lastrowid
        if pieces:
            embeddings = embed_texts(pieces)
            conn.executemany(
                "INSERT INTO chunks (material_id, ord, text, page_ref, embedding) "
                "VALUES (?, ?, ?, NULL, ?)",
                [
                    (material_id, i, piece, serialize_embedding(vec))
                    for i, (piece, vec) in enumerate(zip(pieces, embeddings))
                ],
            )
        result.embedded += 1

    stale = conn.execute(
        "SELECT id, assignment_id FROM materials WHERE kind = 'assignment'"
    ).fetchall()
    for m in stale:
        if m["assignment_id"] not in live_ids:
            conn.execute("DELETE FROM chunks WHERE material_id = ?", (m["id"],))
            conn.execute("DELETE FROM materials WHERE id = ?", (m["id"],))
            result.removed += 1

    conn.commit()
    return result
