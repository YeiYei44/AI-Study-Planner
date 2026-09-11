"""Add a material: extract -> chunk -> embed -> store. One function that
ties extract.py, chunk.py, and embed.py to the DB.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.tutor.chunk import chunk_sections
from app.tutor.embed import embed_texts, serialize_embedding
from app.tutor.extract import extract_sections


@dataclass
class AddMaterialResult:
    material_id: int
    title: str
    chunk_count: int


def add_material(
    conn: sqlite3.Connection,
    path: Path,
    title: str | None = None,
    course_id: int | None = None,
    kind: str = "file",
) -> AddMaterialResult:
    sections = extract_sections(path)
    chunks = chunk_sections(sections)  # [(page_ref, text), ...]

    title = title or path.stem
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO materials (course_id, kind, title, source_path, added_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (course_id, kind, title, str(path), now),
    )
    material_id = cur.lastrowid

    if chunks:
        embeddings = embed_texts([text for _ref, text in chunks])
        conn.executemany(
            "INSERT INTO chunks (material_id, ord, text, page_ref, embedding) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (material_id, i, text, page_ref, serialize_embedding(vec))
                for i, ((page_ref, text), vec) in enumerate(zip(chunks, embeddings))
            ],
        )

    conn.commit()
    return AddMaterialResult(material_id=material_id, title=title, chunk_count=len(chunks))
