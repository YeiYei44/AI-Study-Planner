import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.tutor.embed import serialize_embedding
from app.tutor.qa import _fts_query, ask, assignment_digest, retrieve


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


def _insert_assignment(conn, aid, name, due_at, points=50, workflow_state="published"):
    conn.execute(
        "INSERT INTO courses (id, name, source, raw_json, fetched_at) "
        "VALUES (1, 'Chem', 's', '{}', 't') ON CONFLICT(id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO assignments
            (id, course_id, name, due_at, points_possible, workflow_state, source, raw_json, fetched_at)
        VALUES (?, 1, ?, ?, ?, ?, 's', '{}', 't')
        """,
        (aid, name, due_at, points, workflow_state),
    )
    conn.commit()


def _insert_material_with_chunk(conn, mid, title, text, page_ref, embedding):
    conn.execute(
        "INSERT INTO materials (id, kind, title, added_at) VALUES (?, 'notes', ?, 't')",
        (mid, title),
    )
    conn.execute(
        "INSERT INTO chunks (material_id, ord, text, page_ref, embedding) VALUES (?, 0, ?, ?, ?)",
        (mid, text, page_ref, serialize_embedding(embedding)),
    )
    conn.commit()


class _FakeAnswerBackend:
    def __init__(self, response="An answer."):
        self.response = response
        self.last_prompt = None

    async def answer(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response


def test_retrieve_ranks_by_similarity(conn, monkeypatch):
    monkeypatch.setattr("app.tutor.qa.embed_query", lambda text: [1.0, 0.0, 0.0])
    _insert_material_with_chunk(conn, 1, "Close Match", "relevant text", "page 1", [1.0, 0.0, 0.0])
    _insert_material_with_chunk(conn, 2, "Far Match", "irrelevant text", "page 1", [0.0, 1.0, 0.0])

    results = retrieve(conn, "query", top_k=5)
    assert results[0].material_title == "Close Match"
    assert results[0].score > results[1].score


def test_retrieve_respects_top_k(conn, monkeypatch):
    monkeypatch.setattr("app.tutor.qa.embed_query", lambda text: [1.0, 0.0, 0.0])
    for i in range(5):
        _insert_material_with_chunk(conn, i, f"Mat{i}", "text", "", [1.0, 0.0, 0.0])
    assert len(retrieve(conn, "q", top_k=2)) == 2


def test_ask_with_no_materials_short_circuits(conn):
    backend = _FakeAnswerBackend()
    result = asyncio.run(ask(conn, backend, "any question?"))
    assert "no materials" in result.answer.lower() or "material add" in result.answer.lower()
    assert result.sources == []
    assert backend.last_prompt is None  # never even called the model


def test_ask_includes_citation_label_in_prompt(conn, monkeypatch):
    monkeypatch.setattr("app.tutor.qa.embed_query", lambda text: [1.0, 0.0, 0.0])
    _insert_material_with_chunk(conn, 1, "Periodic Trends", "atomic radius shrinks", "page 3", [1.0, 0.0, 0.0])
    backend = _FakeAnswerBackend(response="It shrinks. (Periodic Trends, page 3)")

    result = asyncio.run(ask(conn, backend, "what happens to atomic radius?"))
    assert result.answer == "It shrinks. (Periodic Trends, page 3)"
    assert "[Periodic Trends, page 3]" in backend.last_prompt
    assert "atomic radius shrinks" in backend.last_prompt
    assert result.sources[0].material_title == "Periodic Trends"


def test_ask_omits_page_ref_in_label_when_absent(conn, monkeypatch):
    monkeypatch.setattr("app.tutor.qa.embed_query", lambda text: [1.0, 0.0, 0.0])
    _insert_material_with_chunk(conn, 1, "General Notes", "some text", "", [1.0, 0.0, 0.0])
    backend = _FakeAnswerBackend()

    asyncio.run(ask(conn, backend, "question?"))
    assert "[General Notes]" in backend.last_prompt
    assert "[General Notes," not in backend.last_prompt


# -- hybrid (lexical + semantic) retrieval — the real "Document 3" bug ------
# Found against real data: a small dense embedding model alone ranked the
# one chunk containing the literal text "Document 3" 16th of 19 for a
# question asking specifically about "document 3" — the number gets
# diluted into an average against generic recurring words. These lock the
# fix in as a regression test.


def test_fts_query_strips_stopwords_and_ors_terms():
    # "about" is itself in the stopword list — deliberately: it's a filler
    # word in nearly every question ("what is X about"), not a search term.
    assert _fts_query("What is document 3 about?") == '"document" OR "3"'


def test_fts_query_empty_when_only_stopwords():
    assert _fts_query("what is the") == ""


def test_lexical_match_surfaces_despite_weak_dense_score(conn, monkeypatch):
    # The query embeds identically regardless of text (a simplification —
    # dense scoring itself is already covered elsewhere); what's under
    # test is that a strong *lexical* match can still win the fused
    # ranking even when it's the dense-orthogonal worst-scoring chunk.
    monkeypatch.setattr("app.tutor.qa.embed_query", lambda text: [1.0, 0.0])

    # Several chunks that dense-match perfectly but never mention the
    # thing actually being asked about — mirrors the real material, where
    # every chunk is generically "about" the same DBQ.
    for i in range(1, 6):
        _insert_material_with_chunk(conn, i, f"Filler{i}", "generic related discussion", "", [1.0, 0.0])
    # The one chunk that's dense-orthogonal (worst possible cosine score)
    # but contains the exact thing asked about.
    _insert_material_with_chunk(
        conn, 6, "Specific", "the target item is described in detail here", "", [0.0, 1.0]
    )

    results = retrieve(conn, "what is the target item?", top_k=3)
    assert "Specific" in [r.material_title for r in results]


def test_pure_dense_query_still_works_when_no_lexical_terms(conn, monkeypatch):
    # A query that's entirely stopwords has no FTS terms at all — must
    # degrade to dense-only ranking, not break.
    monkeypatch.setattr("app.tutor.qa.embed_query", lambda text: [1.0, 0.0, 0.0])
    _insert_material_with_chunk(conn, 1, "Close", "text", "", [1.0, 0.0, 0.0])
    _insert_material_with_chunk(conn, 2, "Far", "text", "", [0.0, 1.0, 0.0])

    results = retrieve(conn, "is the", top_k=5)  # all stopwords
    assert results[0].material_title == "Close"


# -- assignment digest — always-included, not retrieval-based --------------
# Deliberately separate from chunk retrieval: a "what's due" question has
# no lexical/semantic reason to prefer one assignment's material chunk
# over another's, so this can't rely on ranking at all.

_NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


def test_digest_lists_future_assignments_soonest_first(conn):
    _insert_assignment(conn, 1, "Later", "2026-01-20T23:59:00Z", points=10)
    _insert_assignment(conn, 2, "Sooner", "2026-01-12T23:59:00Z", points=20)
    digest = assignment_digest(conn, now=_NOW)
    assert digest.index("Sooner") < digest.index("Later")


def test_digest_excludes_past_due(conn):
    _insert_assignment(conn, 1, "AlreadyDue", "2026-01-01T23:59:00Z")
    digest = assignment_digest(conn, now=_NOW)
    assert "AlreadyDue" not in digest


def test_digest_excludes_unpublished(conn):
    _insert_assignment(conn, 1, "Draft", "2026-01-20T23:59:00Z", workflow_state="unpublished")
    digest = assignment_digest(conn, now=_NOW)
    assert "Draft" not in digest


def test_digest_includes_course_due_date_and_points(conn):
    _insert_assignment(conn, 1, "Essay", "2026-01-20T23:59:00Z", points=90)
    digest = assignment_digest(conn, now=_NOW)
    assert "Essay" in digest
    assert "Chem" in digest
    assert "90 pts" in digest


def test_digest_respects_limit(conn):
    for i in range(5):
        _insert_assignment(conn, i, f"A{i}", f"2026-01-{12+i}T23:59:00Z")
    digest = assignment_digest(conn, now=_NOW, limit=2)
    assert len(digest.splitlines()) == 2


def test_ask_includes_digest_in_prompt_even_with_no_chunks(conn):
    _insert_assignment(conn, 1, "Lab Report", "2099-01-01T23:59:00Z", points=30)
    backend = _FakeAnswerBackend()

    result = asyncio.run(ask(conn, backend, "what's due soon?"))
    assert backend.last_prompt is not None
    assert "Lab Report" in backend.last_prompt
    assert result.sources == []  # no materials/chunks were retrieved


def test_ask_includes_digest_alongside_retrieved_chunks(conn, monkeypatch):
    monkeypatch.setattr("app.tutor.qa.embed_query", lambda text: [1.0, 0.0, 0.0])
    _insert_assignment(conn, 1, "Lab Report", "2099-01-01T23:59:00Z", points=30)
    _insert_material_with_chunk(conn, 1, "Notes", "some notes text", "page 1", [1.0, 0.0, 0.0])
    backend = _FakeAnswerBackend()

    asyncio.run(ask(conn, backend, "question?"))
    assert "Lab Report" in backend.last_prompt
    assert "[Notes, page 1]" in backend.last_prompt
