"""Split extracted section text into overlapping chunks for embedding.

Chunks *within* each (page_ref, text) section rather than across a whole
document's concatenated text — a section boundary is a real page/slide
boundary, and blurring across it would make citations point at the wrong
page for text near the seam.
"""

from __future__ import annotations

TARGET_CHARS = 800
OVERLAP_CHARS = 100


def chunk_text(text: str, target_chars: int = TARGET_CHARS, overlap_chars: int = OVERLAP_CHARS) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= target_chars:
        return [text]

    chunks = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + target_chars, n)
        # Prefer ending on whitespace near the boundary rather than
        # mid-word, when there's a reasonable place to do it.
        if end < n:
            space = text.rfind(" ", start, end)
            if space > start + target_chars // 2:
                end = space
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        # The next chunk starts ~overlap_chars before this one ended —
        # snapped forward to the next word boundary, so it never *begins*
        # mid-word either (the failure mode a naive fixed-offset overlap
        # has: trimming the chunk's end to a word boundary says nothing
        # about where the next one starts).
        overlap_start = max(end - overlap_chars, start + 1)
        space = text.find(" ", overlap_start)
        start = (space + 1) if (space != -1 and space < end) else end
    return chunks


def chunk_sections(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """[(page_ref, section_text), ...] -> [(page_ref, chunk_text), ...],
    one output row per chunk, page_ref repeated for every chunk from that
    section."""
    out: list[tuple[str, str]] = []
    for page_ref, text in sections:
        for piece in chunk_text(text):
            out.append((page_ref, piece))
    return out
