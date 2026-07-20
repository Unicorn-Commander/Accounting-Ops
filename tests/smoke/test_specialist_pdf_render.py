"""Smoke test: Markdown → PDF renderer.

Feeds a known Markdown snippet into the _html_to_pdf pipeline and asserts:
1. The result starts with the PDF magic bytes (%PDF-).
2. Key expected strings are findable in the raw PDF byte stream.

We avoid a live-stack dependency: the renderer is a pure function that takes
Markdown text and returns bytes; no DB or Keycloak connection is needed.

WeasyPrint is an optional dep (main group) so skip gracefully if not installed.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_MD = """\
# Form 1120 — 2023 Draft

**Entity:** Acme Inc.
**Tax Year:** 2023

## Revenue Summary

| Line | Description          | Amount       |
|------|----------------------|--------------|
| 1a   | Gross receipts       | $48,250.00   |
| 26   | Other deductions     | $31,400.00   |

## Notes

- Revenue recognized on a cash basis per §446.
- Contractor payments verified via account 6300 (1099-NEC threshold applied).
- NOL carryforward from TY 2022: **$0** (first revenue year).

> CPA: Please confirm the §1202 exclusion eligibility before finalizing line 28.
"""


# ---------------------------------------------------------------------------
# Helper: import renderer without touching FastAPI / DB
# ---------------------------------------------------------------------------

def _try_import_weasyprint() -> bool:
    try:
        import weasyprint  # noqa: F401
        return True
    except (ImportError, OSError):
        # OSError is raised on macOS / minimal Linux when the Pango/GObject
        # system libraries (libgobject-2.0, libpango, etc.) are not installed.
        # This is expected outside of the Docker container; tests skip gracefully.
        return False


def _try_import_markdown() -> bool:
    try:
        import markdown  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not _try_import_weasyprint(),
    reason="weasyprint not installed — add to main deps and rebuild",
)
@pytest.mark.skipif(
    not _try_import_markdown(),
    reason="markdown not installed — add to main deps and rebuild",
)
def test_pdf_magic_bytes():
    """The rendered bytes must begin with the PDF header magic."""
    from app.api.specialist_pdf import _build_html, _html_to_pdf

    html = _build_html(
        body_md=SAMPLE_MD,
        entity_name="Acme Inc.",
        tax_year=2023,
        doc_title="Form 1120 — Federal Corporate Income Tax Return",
    )
    pdf_bytes = _html_to_pdf(html)

    assert isinstance(pdf_bytes, bytes), "Expected bytes from _html_to_pdf"
    assert len(pdf_bytes) > 1_000, f"PDF is suspiciously small: {len(pdf_bytes)} bytes"
    assert pdf_bytes[:4] == b"%PDF", (
        f"Output does not start with PDF magic bytes (%PDF): {pdf_bytes[:8]!r}"
    )


@pytest.mark.skipif(
    not _try_import_weasyprint(),
    reason="weasyprint not installed",
)
@pytest.mark.skipif(
    not _try_import_markdown(),
    reason="markdown not installed",
)
def test_pdf_contains_expected_content():
    """Key text snippets from the Markdown must be findable in the raw PDF bytes.

    PDF stores text in the byte stream (often uncompressed for simple content);
    checking the raw bytes is a reliable proxy for 'the text got rendered'.
    For compressed PDFs we fall back to pdfplumber / PyPDF2 if available.
    """
    from app.api.specialist_pdf import _build_html, _html_to_pdf

    html = _build_html(
        body_md=SAMPLE_MD,
        entity_name="Acme Inc.",
        tax_year=2023,
        doc_title="Form 1120 — Federal Corporate Income Tax Return",
    )
    pdf_bytes = _html_to_pdf(html)

    # Try pdfplumber first (richest extraction), then PyPDF2, then raw scan.
    extracted_text = _extract_text(pdf_bytes)

    # Assertions on extracted or raw bytes.
    expected_fragments = [
        "Acme",
        "2023",
        "Revenue",
        "CPA REVIEW REQUIRED",  # watermark text
    ]
    for fragment in expected_fragments:
        found_in_text = fragment in extracted_text if extracted_text else False
        found_in_raw = fragment.encode("latin-1", errors="ignore") in pdf_bytes
        assert found_in_text or found_in_raw, (
            f"Expected fragment {fragment!r} not found in rendered PDF "
            f"(text_len={len(extracted_text or '')}, pdf_size={len(pdf_bytes)})"
        )


def _extract_text(pdf_bytes: bytes) -> str | None:
    """Best-effort text extraction: pdfplumber → pypdf → PyPDF2 → None.

    Order matters: pdfplumber gives the richest text (handles columns,
    spacing) when installed; ``pypdf`` is the modern maintained successor
    to ``PyPDF2`` and is what ships in the app container today; ``PyPDF2``
    is kept as a final fallback for old environments. None means "no
    extractor available" — caller falls back to a raw byte scan, which is
    only reliable for uncompressed PDFs (WeasyPrint emits FlateDecode
    streams by default, so the raw scan WILL fail for the body text).
    """
    try:
        import io
        import pdfplumber  # type: ignore[import-untyped]

        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join(
                page.extract_text() or "" for page in pdf.pages
            )
    except Exception:
        pass

    # pypdf (modern, maintained) — preferred over PyPDF2 in new environments.
    # The accounting-ops container ships with pypdf already as a transitive
    # dep, so this branch is what actually runs in CI / on midboy1.
    try:
        import io
        import pypdf  # type: ignore[import-untyped]

        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(
            (reader.pages[i].extract_text() or "")
            for i in range(len(reader.pages))
        )
    except Exception:
        pass

    try:
        import io
        import PyPDF2  # type: ignore[import-untyped]

        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        return "\n".join(
            reader.pages[i].extract_text() or ""
            for i in range(len(reader.pages))
        )
    except Exception:
        pass

    return None


def test_html_build_contains_watermark_css():
    """The HTML output must contain the DRAFT watermark CSS rule."""
    try:
        import markdown  # noqa: F401
    except ImportError:
        pytest.skip("markdown not installed")

    from app.api.specialist_pdf import _build_html

    html = _build_html(
        body_md="# Test",
        entity_name="Test Corp",
        tax_year=2023,
        doc_title="Test Draft",
    )

    assert "DRAFT" in html, "Watermark text not found in HTML"
    assert "rotate(-30deg)" in html, "Watermark CSS rotation not found in HTML"
    assert "CPA REVIEW REQUIRED" in html, "CPA disclaimer text not found in HTML"


def test_slug_meta_coverage():
    """All ordered slugs must have entries in _SLUG_META."""
    from app.api.specialist_pdf import _ORDERED_SLUGS, _SLUG_META

    for slug in _ORDERED_SLUGS:
        assert slug in _SLUG_META, f"Slug {slug!r} missing from _SLUG_META"
        meta = _SLUG_META[slug]
        assert "title" in meta
        assert "filename_prefix" in meta
        assert "draft_file" in meta
        # draft_file must be a template with {year}
        assert "{year}" in meta["draft_file"], (
            f"draft_file for {slug!r} missing {{year}} placeholder"
        )
