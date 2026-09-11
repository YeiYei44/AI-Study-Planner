import sys
from pathlib import Path

import pytest

from app.tutor.extract import UnsupportedFileType, extract_sections


def test_unsupported_extension_raises():
    with pytest.raises(UnsupportedFileType):
        extract_sections(Path("notes.xyz"))


def test_txt_extraction(tmp_path):
    p = tmp_path / "notes.txt"
    p.write_text("Line one.\nLine two.")
    sections = extract_sections(p)
    assert sections == [("", "Line one.\nLine two.")]


def test_md_extraction_same_as_txt(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Heading\n\nSome text.")
    sections = extract_sections(p)
    assert len(sections) == 1
    assert "Heading" in sections[0][1]


def test_docx_extraction_real_file(tmp_path):
    docx = pytest.importorskip("docx")
    p = tmp_path / "notes.docx"
    doc = docx.Document()
    for i in range(20):
        doc.add_paragraph(f"Paragraph number {i}.")
    doc.save(p)

    sections = extract_sections(p)
    # group_size=15 in extract.py -> 20 paragraphs become 2 sections
    assert [ref for ref, _ in sections] == ["section 1", "section 2"]
    assert "Paragraph number 0." in sections[0][1]
    assert "Paragraph number 19." in sections[1][1]


def test_pptx_extraction_real_file(tmp_path):
    pptx_mod = pytest.importorskip("pptx")
    p = tmp_path / "slides.pptx"
    prs = pptx_mod.Presentation()
    for title in ("First Slide", "Second Slide"):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = title
    prs.save(p)

    sections = extract_sections(p)
    assert [ref for ref, _ in sections] == ["slide 1", "slide 2"]
    assert "First Slide" in sections[0][1]
    assert "Second Slide" in sections[1][1]


def test_pdf_extraction_uses_pypdf(tmp_path, monkeypatch):
    class _FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _FakeReader:
        def __init__(self, _path):
            self.pages = [_FakePage("Page one text."), _FakePage("Page two text.")]

    fake_pypdf = type("FakePypdf", (), {"PdfReader": _FakeReader})
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-fake")  # extract.py only checks the suffix, never the bytes
    sections = extract_sections(p)
    assert sections == [("page 1", "Page one text."), ("page 2", "Page two text.")]


def test_pdf_extraction_drops_blank_pages(tmp_path, monkeypatch):
    class _FakePage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _FakeReader:
        def __init__(self, _path):
            self.pages = [_FakePage("Real text."), _FakePage("   ")]

    fake_pypdf = type("FakePypdf", (), {"PdfReader": _FakeReader})
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-fake")
    sections = extract_sections(p)
    assert sections == [("page 1", "Real text.")]
