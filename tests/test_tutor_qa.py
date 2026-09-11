import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.db.connection import connect
from app.tutor.embed import serialize_embedding
from app.tutor.qa import _fts_query, ask, retrieve


@pytest.fixture
def conn(tmp_path: Path):
    c = connect(Settings(data_dir=tmp_path))
    yield c
    c.close()


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
