from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.tutor.extract import UnsupportedFileType
from app.tutor.materials import add_material


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _fake_embeddings(monkeypatch):
    # Deterministic, fast — unit tests shouldn't depend on a real model's
    # exact output; the real pipeline is verified separately (see
    # docs/DESIGN.md) against real files and a real chat backend.
    monkeypatch.setattr(
        "app.tutor.materials.embed_texts",
        lambda texts: [[float(i), 0.0, 0.0] for i in range(len(texts))],
    )


def test_add_material_stores_row_and_chunks(tmp_path, conn):
    f = tmp_path / "notes.txt"
    f.write_text("Some notes about photosynthesis.")

    result = add_material(conn, f, title="Bio Notes", kind="notes")
    assert result.title == "Bio Notes"
    assert result.chunk_count == 1

    material = conn.execute("SELECT * FROM materials WHERE id = ?", (result.material_id,)).fetchone()
    assert material["title"] == "Bio Notes"
    assert material["kind"] == "notes"

    chunks = conn.execute("SELECT * FROM chunks WHERE material_id = ?", (result.material_id,)).fetchall()
    assert len(chunks) == 1
    assert "photosynthesis" in chunks[0]["text"]
    assert chunks[0]["embedding"]  # non-empty blob


def test_add_material_defaults_title_to_filename(tmp_path, conn):
    f = tmp_path / "my-notes.txt"
    f.write_text("Text.")
    result = add_material(conn, f)
    assert result.title == "my-notes"


def test_add_material_associates_course(tmp_path, conn):
    conn.execute("INSERT INTO courses (id, name, source, raw_json, fetched_at) VALUES (1,'C','s','{}','t')")
    conn.commit()
    f = tmp_path / "notes.txt"
    f.write_text("Text.")
    result = add_material(conn, f, course_id=1)
    material = conn.execute("SELECT course_id FROM materials WHERE id = ?", (result.material_id,)).fetchone()
    assert material["course_id"] == 1


def test_add_material_unsupported_type_raises(tmp_path, conn):
    f = tmp_path / "notes.xyz"
    f.write_text("Text.")
    with pytest.raises(UnsupportedFileType):
        add_material(conn, f)


def test_add_material_empty_file_produces_zero_chunks(tmp_path, conn):
    f = tmp_path / "empty.txt"
    f.write_text("   ")  # whitespace only
    result = add_material(conn, f)
    assert result.chunk_count == 0
    # material row still exists, just with no searchable content
    assert conn.execute("SELECT * FROM materials WHERE id = ?", (result.material_id,)).fetchone()
