from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.tutor.assignment_sync import sync_assignment_materials


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    monkeypatch.setattr(
        "app.tutor.assignment_sync.embed_texts",
        lambda texts: [[float(i), 0.0, 0.0] for i in range(len(texts))],
    )


def _insert_assignment(conn, aid=1, name="HW1", description="<p>Read chapter 1.</p>", due_at="2026-01-05T23:59:00Z",
                        points=50, workflow_state="published"):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'Chem', 's', '{}', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, description_html, due_at, points_possible,
             workflow_state, source, raw_json, fetched_at)
        VALUES (?, 1, ?, ?, ?, ?, ?, 's', '{}', 't')
        """,
        (aid, name, description, due_at, points, workflow_state),
    )
    conn.commit()


def test_embeds_a_published_assignment(conn):
    _insert_assignment(conn)
    result = sync_assignment_materials(conn)
    assert result.embedded == 1
    assert result.skipped_cached == 0

    material = conn.execute(
        "SELECT * FROM materials WHERE kind = 'assignment' AND assignment_id = 1"
    ).fetchone()
    assert material is not None
    assert material["title"] == "HW1"

    chunks = conn.execute("SELECT text FROM chunks WHERE material_id = ?", (material["id"],)).fetchall()
    assert chunks
    assert "Read chapter 1" in chunks[0]["text"]
    assert "HW1" in chunks[0]["text"]


def test_unpublished_assignment_not_embedded(conn):
    _insert_assignment(conn, workflow_state="unpublished")
    result = sync_assignment_materials(conn)
    assert result.embedded == 0
    assert conn.execute("SELECT * FROM materials WHERE kind = 'assignment'").fetchone() is None


def test_second_sync_with_no_change_is_cached(conn):
    _insert_assignment(conn)
    sync_assignment_materials(conn)
    result = sync_assignment_materials(conn)
    assert result.embedded == 0
    assert result.skipped_cached == 1


def test_changed_description_re_embeds(conn):
    _insert_assignment(conn)
    first = sync_assignment_materials(conn)
    assert first.embedded == 1

    conn.execute("UPDATE assignments SET description_html = '<p>New content.</p>' WHERE id = 1")
    conn.commit()

    second = sync_assignment_materials(conn)
    assert second.embedded == 1
    assert second.skipped_cached == 0

    # Still exactly one materials row for this assignment (old one replaced, not duplicated)
    rows = conn.execute(
        "SELECT id FROM materials WHERE kind = 'assignment' AND assignment_id = 1"
    ).fetchall()
    assert len(rows) == 1
    chunks = conn.execute("SELECT text FROM chunks WHERE material_id = ?", (rows[0]["id"],)).fetchall()
    assert any("New content" in c["text"] for c in chunks)


def test_assignment_becoming_unpublished_removes_its_material(conn):
    _insert_assignment(conn)
    sync_assignment_materials(conn)
    assert conn.execute("SELECT * FROM materials WHERE kind = 'assignment'").fetchone() is not None

    conn.execute("UPDATE assignments SET workflow_state = 'unpublished' WHERE id = 1")
    conn.commit()

    result = sync_assignment_materials(conn)
    assert result.removed == 1
    assert conn.execute("SELECT * FROM materials WHERE kind = 'assignment'").fetchone() is None
    # its chunks go with it
    assert conn.execute("SELECT * FROM chunks").fetchone() is None


def test_reappearing_as_unpublished_after_deletion_from_source_is_removed(conn):
    # A Canvas-side deletion normally surfaces as the assignment simply
    # missing from the next sync's payload, not an actual DELETE against
    # this table (sync.py's own upsert never deletes rows — see
    # docs/DESIGN.md's "removed-but-not-deleted" note) — the realistic
    # case here is workflow_state flipping away from 'published', already
    # covered above. This checks the belt-and-suspenders path: an
    # assignment material orphaned some other way (row already gone) is
    # still cleaned up rather than left dangling forever.
    _insert_assignment(conn, aid=1)
    _insert_assignment(conn, aid=2, name="HW2")
    sync_assignment_materials(conn)
    assert conn.execute("SELECT COUNT(*) c FROM materials WHERE kind='assignment'").fetchone()["c"] == 2

    conn.execute("UPDATE assignments SET workflow_state = 'deleted' WHERE id = 1")
    conn.commit()

    result = sync_assignment_materials(conn)
    assert result.removed == 1
    remaining = conn.execute("SELECT assignment_id FROM materials WHERE kind='assignment'").fetchall()
    assert [r["assignment_id"] for r in remaining] == [2]
