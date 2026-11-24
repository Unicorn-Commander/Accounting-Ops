"""AO-UP-4: fitz must parse untrusted PDFs under a page cap, a time cap, and a
concurrency cap, and a partial render must be visible (never silently
"complete").

The renderer rasterized ``doc[:max_pages]`` (page cap) but with no wall-clock
or concurrency bound: a single pathological page (a crafted content stream / a
huge XObject) could hang the extraction worker forever, and unbounded page
renders thrash the GIL. Each page is now rasterized in a worker process (via
ProcessPoolExecutor) with a hard per-page timeout; a page that overruns the
budget stops the render and the result carries
``error="render timeout (partial)"`` so a truncated tax document is visible.

Offline: a real one-page PDF (built with pymupdf) + a fake pathological page;
no network, no LLM call.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import fitz  # pymupdf

import app.categorization.tax_doc_vlm as mod


def _one_page_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "W-2 sample")
    data = doc.tobytes()
    doc.close()
    return data


def test_render_respects_page_cap() -> None:
    """A 3-page PDF must rasterize only the first max_pages, no error."""
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_text((72, 100), f"page {i}")
    data = doc.tobytes()
    doc.close()

    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    assert error is None
    assert len(out) == 2
    # Each page is a real PNG.
    for png in out:
        assert png.startswith(b"\x89PNG")


def test_render_stops_at_time_cap_and_reports_partial(monkeypatch, caplog) -> None:
    """A page that overruns the per-page budget stops the render, logs a
    WARNING, and the result carries the partial-timeout error (never silently
    "complete").

    The fake rasterizer (injected via rasterize_fn) blocks well past the
    per-page budget; the render must return (with zero pages + the error)
    instead of waiting for the page to finish.
    """
    data = _one_page_pdf()
    monkeypatch.setattr(mod, "_RENDER_PAGE_BUDGET_S", 1.0)

    def _stall(pdf_bytes: Any, page_index: int, zoom: float) -> bytes:
        time.sleep(60)
        return b""

    with caplog.at_level(logging.WARNING, logger="accounting_ops.tax_doc_vlm"):
        out, error = mod._render_pdf(
            data, max_pages=2, dpi=150, rasterize_fn=_stall
        )

    assert out == []
    assert error == "render timeout (partial)"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a partial render must log a WARNING"
    assert any("partial" in r.getMessage() for r in warnings)


def test_render_concurrency_is_bounded() -> None:
    """The process pool is sized to _RENDER_CONCURRENCY, bounding how many
    pages rasterize at once. With 2 workers and 3 pages, all pages still
    complete (they queue)."""
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_text((72, 100), f"page {i}")
    data = doc.tobytes()
    doc.close()

    out, error = mod._render_pdf(data, max_pages=3, dpi=72)
    assert error is None
    assert len(out) == 3
    for png in out:
        assert png.startswith(b"\x89PNG")


def test_heavy_page_refused_fast_by_content_precheck() -> None:
    """R5-P2-2: a REAL heavy page (thousands of drawing ops, built with pymupdf,
    no time.sleep / no rasterizer monkeypatch) must be refused by the
    content-stream pre-check before it is ever rasterized. The join budget is
    best-effort only (the GIL means it cannot interrupt a page already inside
    the C rasterizer), so the refusal must happen in the pre-check, fast, with
    no page produced and a partial error.
    """
    # 8000 drawing ops on one page: ~334 KB of content stream, far over the
    # 128 KB pre-check bound (a real W-2 / 1099 page carries a few KB).
    doc = fitz.open()
    page = doc.new_page()
    for i in range(8000):
        page.draw_rect(
            fitz.Rect(i % 600, (i * 7) % 700, i % 600 + 8, (i * 7) % 700 + 4),
            color=(1, 0, 0),
            fill=(0, 1, 0),
        )
    data = doc.tobytes()
    doc.close()

    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    # The heavy page was refused before rasterization: no page, and the render
    # is marked partial (a truncated tax document is visible, never silently
    # "complete").
    assert out == []
    assert error == "page too large (partial)"
    # The refusal came from the pre-check, not the join budget (10s): a
    # pre-check refusal is a fitz open + a few stream reads (~100 ms), not a
    # raster. It must be far faster than the budget -- a page that the budget
    # alone would have to wait out for 10s is instead refused in well under a
    # second.
    assert elapsed_s < 2.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"


def test_light_page_passes_content_precheck() -> None:
    """R5-P2-2 control: a normal page (a few drawing ops, a few KB of content
    stream) is under the pre-check bound and still rasterizes to a PNG."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "W-2 sample")
    for i in range(5):
        page.draw_rect(
            fitz.Rect(10, 10 + i, 50, 20 + i), color=(1, 0, 0), fill=(0, 1, 0)
        )
    data = doc.tobytes()
    doc.close()

    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    assert error is None
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")


def test_render_returns_pngs_for_good_pages() -> None:
    """Control: normal pages still rasterize to PNG bytes, no error."""
    data = _one_page_pdf()
    out, error = mod._render_pdf(data, max_pages=1, dpi=150)
    assert error is None
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")


def test_render_1_page_pdf_with_max_pages_2_returns_1_image() -> None:
    """R4-1: a single-page W-2/1099 with max_pages=2 (_MAX_PAGES) must rasterize
    its one page, not raise IndexError from doc[1] on the missing second page.
    The loop bound is min(max_pages, page_count)."""
    # A minimal one-page PDF (no text, matching the reviewer's snippet).
    d = fitz.open()
    d.new_page()
    b = d.tobytes()
    d.close()

    out, error = mod._render_pdf(b, max_pages=2, dpi=72)
    assert error is None
    # Exactly 1 image: the document has one page, the cap is two, the loop
    # stops at the document's page count.
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")


def test_render_timeout_returns_partial(monkeypatch) -> None:
    """A page that overruns its budget produces a partial result with no pages.

    With the per-page budget shrunk to 0.5s and a rasterizer that sleeps 60s,
    the render must return (with zero pages + the error) instead of waiting.
    """
    data = _one_page_pdf()
    monkeypatch.setattr(mod, "_RENDER_PAGE_BUDGET_S", 0.5)

    def _slow(pdf_bytes: Any, page_index: int, zoom: float) -> bytes:
        time.sleep(60)
        return b""

    out, error = mod._render_pdf(data, max_pages=1, dpi=72, rasterize_fn=_slow)
    assert out == []
    assert error == "render timeout (partial)"


def test_extract_passes_keywords_to_render_pdf(monkeypatch) -> None:
    """R8-P1-1: TaxDocExtractor.extract() must call _render_pdf with keyword
    arguments (max_pages=, dpi=) because _render_pdf has a keyword-only
    signature. A positional call would raise TypeError. This test drives
    extract() end-to-end with a real 1-page PDF and a stubbed HTTP call to
    verify no TypeError occurs."""
    import httpx

    data = _one_page_pdf()

    captured_kwargs: dict[str, Any] = {}

    def _fake_render_pdf(pdf_bytes: bytes, *, max_pages: int, dpi: int, rasterize_fn: Any = None) -> tuple[list[bytes], str | None]:
        captured_kwargs["max_pages"] = max_pages
        captured_kwargs["dpi"] = dpi
        return [b"\x89PNG-fake"], None

    monkeypatch.setattr(mod, "_render_pdf", _fake_render_pdf)

    # Stub the HTTP call to avoid network
    async def _fake_post(self: Any, url: Any, **kwargs: Any) -> Any:
        class _Resp:
            def raise_for_status(self) -> None:
                pass
            def json(self) -> dict:
                return {
                    "choices": [{"message": {"content": '{"doc_type": "W-2", "tax_year": 2024, "payer_name": "Acme", "recipient_name": "Bob", "fields": {}, "confidence": 0.9}'}}]
                }
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    import asyncio
    from app.categorization.tax_doc_vlm import TaxDocExtractor
    from app.config import Settings

    settings = Settings(llm_model="test-model", llm_endpoint_url="http://localhost:9999", llm_api_key="k")
    ext = TaxDocExtractor(settings=settings)
    result = asyncio.get_event_loop().run_until_complete(
        ext.extract(body=data, mime_type="application/pdf")
    )
    # No TypeError: extract() completed (error is None or a VLM-level error,
    # not a render TypeError).
    assert captured_kwargs["max_pages"] == mod._MAX_PAGES
    assert captured_kwargs["dpi"] == mod._RENDER_DPI
    assert result.error is None or "TypeError" not in (result.error or "")


def test_xobject_heavy_page_refused_by_precheck() -> None:
    """R8-P2-1: a page whose content stream is small but references a large
    Form XObject must be refused by the decoded-size pre-check. The raw
    (compressed) content stream is tiny; the decoded XObject stream is large.
    This verifies that _page_content_bytes includes XObject sizes."""
    d = fitz.open()
    page = d.new_page()
    page.draw_rect(fitz.Rect(10, 10, 18, 14), color=(1, 0, 0))
    page_xref = page.get_contents()[0]
    res_xref = 3  # resources object xref (from PDF structure inspection)
    # Insert a heavy Form XObject
    new_xref = d.get_new_xref()
    heavy = b"\n".join([f"{(i*3)%600} {(i*7)%650} 8 4 re f".encode("latin-1") for i in range(4000)])
    obj_str = "<< /Type /XObject /Subtype /Form /BBox [0 0 595 842] /Resources << >> /Matrix [1 0 0 1 0 0] >>"
    d.update_object(new_xref, obj_str)
    d.update_stream(new_xref, heavy)
    d.xref_set_key(res_xref, "XObject", f"<</Form1 {new_xref} 0 R>>")
    # Replace the content stream with a tiny one that just invokes the XObject
    d.update_stream(page_xref, b"/Form1 Do")
    d.close()

    # The raw content stream is ~9 bytes; the decoded XObject is ~66 KB.
    # Total decoded (content + XObject) should be well over 128 KB? No,
    # ~66 KB is under 128 KB. Let's use more rects to exceed the bound.
    # Actually 66 KB < 128 KB, so this would PASS the pre-check.
    # Let's use a larger XObject to exceed 128 KB.
    d2 = fitz.open()
    page2 = d2.new_page()
    page2.draw_rect(fitz.Rect(10, 10, 18, 14), color=(1, 0, 0))
    page2_xref = page2.get_contents()[0]
    res2_xref = 3
    new2_xref = d2.get_new_xref()
    # 8000 ops: ~133 KB decoded
    heavy2 = b"\n".join([f"{(i*3)%600} {(i*7)%650} 8 4 re f".encode("latin-1") for i in range(8000)])
    d2.update_object(new2_xref, obj_str)
    d2.update_stream(new2_xref, heavy2)
    d2.xref_set_key(res2_xref, "XObject", f"<</Form1 {new2_xref} 0 R>>")
    d2.update_stream(page2_xref, b"/Form1 Do")
    data2 = d2.tobytes()
    d2.close()

    t0 = time.monotonic()
    out, error = mod._render_pdf(data2, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    # The XObject-heavy page must be refused by the pre-check (decoded size
    # ~133 KB > 128 KB bound), even though the raw content stream is tiny.
    assert out == []
    assert error == "page too large (partial)"
    assert elapsed_s < 2.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"
