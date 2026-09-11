from app.tutor.chunk import TARGET_CHARS, chunk_sections, chunk_text


def test_empty_text_yields_no_chunks():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


def test_short_text_is_one_chunk():
    assert chunk_text("A short paragraph.") == ["A short paragraph."]


def test_long_text_splits_with_overlap():
    text = " ".join(f"word{i}" for i in range(500))  # well over TARGET_CHARS
    chunks = chunk_text(text, target_chars=100, overlap_chars=20)
    assert len(chunks) > 1
    assert all(len(c) <= 100 for c in chunks)
    # consecutive chunks should share some trailing/leading words (the overlap)
    assert any(w in chunks[1] for w in chunks[0].split()[-3:])


def test_prefers_breaking_on_whitespace_not_mid_word():
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta", "iota", "kappa"]
    chunks = chunk_text(" ".join(words), target_chars=20, overlap_chars=5)
    # every "word" in every chunk should be a real, whole word from the
    # original list — never a fragment left over from a hard char-index cut
    for c in chunks:
        for token in c.split():
            assert token in words, f"{token!r} looks like a mid-word cut"


def test_chunk_sections_preserves_page_ref_per_section():
    sections = [("page 1", "short text"), ("page 2", "more short text")]
    out = chunk_sections(sections)
    assert out == [("page 1", "short text"), ("page 2", "more short text")]


def test_chunk_sections_drops_nothing_empty_stays_empty():
    assert chunk_sections([("page 1", "")]) == []


def test_target_chars_default_is_reasonable():
    assert 200 <= TARGET_CHARS <= 2000
