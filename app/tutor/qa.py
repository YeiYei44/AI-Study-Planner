"""Cited Q&A: retrieve relevant chunks, then answer grounded in only
what was retrieved.

The one rule this whole module exists to enforce: a tutor that invents
course specifics the night before an exam is worse than no tutor at all
(docs/DESIGN.md §8). The prompt requires citations and an explicit "not
covered in your materials" when the retrieved context doesn't answer the
question — that's a prompt-level constraint, not a guarantee, but it's
the whole reason retrieval + a strict prompt is the design here instead
of just asking the model to freewheel.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.tutor.embed import cosine_similarity, deserialize_embedding, embed_query

# Cap on the always-included digest so a long semester's worth of
# assignments (112, on the real account) doesn't dominate the prompt —
# soonest-due first, since those are what a "what's due" question means.
DEFAULT_DIGEST_LIMIT = 30

# 8, not 5: found against real data that a genuinely relevant chunk can
# rank just outside a tighter cutoff even after hybrid fusion (moved from
# 16th to 6th of 19 for one real query — a huge improvement, but 5 still
# missed it). More context rarely confuses a model that's explicitly told
# to answer only from what's given; the cost of a slightly wider net here
# is low.
DEFAULT_TOP_K = 8

# Reciprocal Rank Fusion constant — standard choice (e.g. Elasticsearch's
# own RRF implementation defaults to the same value); not tuned further,
# since RRF is fairly insensitive to the exact constant in practice.
_RRF_K = 60

_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "what", "which", "who", "whom",
    "this", "that", "these", "those", "in", "on", "of", "for", "to", "and", "or",
    "about", "do", "does", "did", "it", "its", "with", "as", "by", "be", "been",
    "i", "you", "he", "she", "they", "we", "my", "your", "his", "her", "their", "our",
}

_ANSWER_PROMPT = """\
You are a study tutor. Answer the student's question using ONLY the information below — \
don't use outside knowledge, even if you're confident it's correct. If it doesn't contain \
the answer, say so plainly rather than guessing.

Upcoming assignments (always current — use this for any question about what's due, when, \
or how many points something is worth; it needs no citation, it isn't an excerpt):
{assignment_digest}

Excerpts from course materials and assignment descriptions, each labeled with its source in \
brackets, e.g. [Periodic Trends Notes, page 3] or just [Tee Shirt Contest] when there's no \
page/slide number. After every claim drawn from one, cite it using that exact label in \
parentheses — for example: "(Periodic Trends Notes, page 3)". Copy the label exactly as \
shown; never write the literal words "page_ref" or "Title".

Excerpts:
{excerpts}

Question: {question}
"""


@dataclass
class RetrievedChunk:
    chunk_id: int
    material_title: str
    page_ref: str
    text: str
    score: float  # cosine similarity — shown to the user; selection uses fused rank, below


@dataclass
class AnswerResult:
    answer: str
    sources: list[RetrievedChunk]


def _fts_query(text: str) -> str:
    """Bare space-separated terms mean AND in FTS5's default query syntax
    — far too strict for a natural-language question. OR'd, quoted terms
    (quoting sidesteps a term colliding with an FTS5 keyword like AND/OR/
    NOT) match any of them, with bm25() ranking doing the real work of
    favoring chunks that match more terms, and rarer ones more."""
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if t not in _STOPWORDS]
    return " OR ".join(f'"{t}"' for t in terms)


def _lexical_rank(conn: sqlite3.Connection, query: str, limit: int) -> dict[int, int]:
    """chunk id -> 1-based rank (best first) via FTS5 BM25. Empty if FTS5
    isn't available on this SQLite build, or the query has no non-stopword
    terms, or none of them match anything."""
    fts_query = _fts_query(query)
    if not fts_query:
        return {}
    try:
        rows = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? "
            "ORDER BY bm25(chunks_fts) LIMIT ?",
            (fts_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["rowid"]: i + 1 for i, row in enumerate(rows)}


def retrieve(conn: sqlite3.Connection, query: str, top_k: int = DEFAULT_TOP_K) -> list[RetrievedChunk]:
    """Hybrid retrieval: dense (embedding cosine similarity) and lexical
    (FTS5 BM25 keyword match) rankings, merged by Reciprocal Rank Fusion.

    Dense search alone is weak at exact/numbered references — a chunk
    containing the literal text "Document 3" can rank far below chunks
    that are merely topically similar, because the specific token "3"
    gets diluted into an average against generic surrounding words
    ("Unit 1 DBQ" appearing in every chunk of that material). RRF means a
    chunk doesn't have to win outright on one signal; scoring well on
    either is enough to surface it.
    """
    q_vec = embed_query(query)
    rows = conn.execute(
        "SELECT c.id, c.text, c.page_ref, c.embedding, m.title AS material_title "
        "FROM chunks c JOIN materials m ON m.id = c.material_id"
    ).fetchall()
    if not rows:
        return []

    dense_scored = sorted(
        ((cosine_similarity(q_vec, deserialize_embedding(r["embedding"])), r) for r in rows),
        key=lambda pair: -pair[0],
    )
    dense_rank = {r["id"]: i + 1 for i, (_score, r) in enumerate(dense_scored)}
    dense_score = {r["id"]: score for score, r in dense_scored}
    by_id = {r["id"]: r for r in rows}

    lexical_rank = _lexical_rank(conn, query, limit=len(rows))

    fused: list[tuple[float, int]] = []
    for cid in set(dense_rank) | set(lexical_rank):
        score = 0.0
        if cid in dense_rank:
            score += 1.0 / (_RRF_K + dense_rank[cid])
        if cid in lexical_rank:
            score += 1.0 / (_RRF_K + lexical_rank[cid])
        fused.append((score, cid))
    fused.sort(key=lambda pair: -pair[0])

    out = []
    for _fused_score, cid in fused[:top_k]:
        r = by_id[cid]
        out.append(
            RetrievedChunk(
                chunk_id=cid,
                material_title=r["material_title"],
                page_ref=r["page_ref"] or "",
                text=r["text"],
                score=dense_score[cid],
            )
        )
    return out


def _format_excerpts(chunks: list[RetrievedChunk]) -> str:
    parts = []
    for c in chunks:
        ref = f"{c.material_title}, {c.page_ref}" if c.page_ref else c.material_title
        parts.append(f"[{ref}]\n{c.text}")
    return "\n\n".join(parts)


def _format_due(due_at: str) -> str:
    try:
        dt = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError:
        return due_at
    return dt.strftime("%a %b %d, %I:%M %p UTC").replace(" 0", " ")


def assignment_digest(
    conn: sqlite3.Connection, now: datetime | None = None, limit: int = DEFAULT_DIGEST_LIMIT
) -> str:
    """Soonest-due-first plain-text list of not-yet-due published
    assignments — deliberately *not* retrieval-based. A dense embedding
    ranks a chunk by topical similarity, which is a poor match for "what's
    due this week": every assignment's material chunk is topically
    similar to that question, so nothing here should depend on the
    question's wording at all. Always computed fresh, not cached — it's a
    handful of indexed rows, not worth content-hashing like the
    assignment *materials* are.
    """
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        "SELECT a.name, a.due_at, a.points_possible, c.name AS course_name "
        "FROM assignments a JOIN courses c ON c.id = a.course_id "
        "WHERE a.workflow_state = 'published' AND a.due_at IS NOT NULL "
        "ORDER BY a.due_at ASC"
    ).fetchall()

    lines = []
    for r in rows:
        try:
            due = datetime.fromisoformat(r["due_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if due < now:
            continue
        points = (
            f"{r['points_possible']:g} pts" if r["points_possible"] is not None else "points unspecified"
        )
        lines.append(f"- {r['name']} ({r['course_name']}): due {_format_due(r['due_at'])}, {points}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


async def ask(conn: sqlite3.Connection, backend, question: str, top_k: int = DEFAULT_TOP_K) -> AnswerResult:
    chunks = retrieve(conn, question, top_k=top_k)
    digest = assignment_digest(conn)
    if not chunks and not digest:
        return AnswerResult(
            answer="No materials uploaded and no upcoming assignments synced yet — "
            "nothing to answer from. Add materials with `material add <file>`, or run "
            "`sync` to pull assignments.",
            sources=[],
        )
    prompt = _ANSWER_PROMPT.format(
        assignment_digest=digest or "(none)",
        excerpts=_format_excerpts(chunks) if chunks else "(none retrieved for this question)",
        question=question,
    )
    answer = await backend.answer(prompt)
    return AnswerResult(answer=answer, sources=chunks)
