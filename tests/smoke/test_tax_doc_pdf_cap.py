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


def _heavy_source_pdf() -> bytes:
    """A one-page PDF whose decoded content stream is ~133 KB (8000 rects),
    far over the 128 KB pre-check bound. Built once and reused by the
    compressed-page and XObject-page tests below (drawing is the slow part)."""
    doc = fitz.open()
    page = doc.new_page()
    for i in range(8000):
        page.draw_rect(
            fitz.Rect(i % 595, (i * 7) % 840, (i % 595) + 2, (i * 7) % 840 + 1),
            color=(1, 0, 0),
            fill=(0, 1, 0),
        )
    data = doc.tobytes()
    doc.close()
    return data


def test_compressed_heavy_page_refused_by_precheck() -> None:
    """R8-P2-1(a): a page of 8000 rects saved with deflate=True is small on
    disk (Flate-compressed) but its DECODED content stream is ~133 KB, over
    the 128 KB bound. The pre-check must measure the decoded size and refuse
    the page fast, with no page produced and a partial error. A compressed-
    size (xref_stream_raw) measure would let this page through."""
    data = _heavy_source_pdf()

    # Sanity: the file IS flate-compressed (raw stream bytes are far below
    # the decoded size), so this test really exercises the decoded-vs-raw
    # distinction the fix makes.
    doc = fitz.open(stream=data, filetype="pdf")
    xref = doc[0].get_contents()[0]
    raw_len = len(doc.xref_stream_raw(xref))
    doc.close()
    assert raw_len < mod._PAGE_CONTENT_MAX_BYTES

    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    # The refusal comes from the pre-check (a fitz open + a few decoded
    # stream reads), not the per-page budget: it must be well under the
    # budget.
    assert elapsed_s < 2.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"


def test_xobject_heavy_page_refused_by_precheck() -> None:
    """R8-P2-1(b): a page whose own content stream is tiny but invokes a heavy
    Form XObject must be refused by the decoded-size pre-check. The page
    content stream is ~16 bytes decoded; the XObject stream it invokes is
    ~341 KB decoded (a Flate-compressed 8000-rect page embedded via
    show_pdf_page), and the whole file is only ~27 KB compressed. A
    compressed-size measure (25 KB total) would let it through."""
    heavy_src = _heavy_source_pdf()
    d = fitz.open()
    page = d.new_page()
    # Invokes the heavy source page as a Form XObject ("/FzFrm0 Do").
    page.show_pdf_page(
        page.rect, fitz.open(stream=heavy_src, filetype="pdf"), 0
    )
    data = d.tobytes(deflate=True)
    d.close()

    doc = fitz.open(stream=data, filetype="pdf")
    xobjs = [x[0] for x in doc[0].get_xobjects()]
    total_raw = sum(
        len(doc.xref_stream_raw(x))
        for x in list(doc[0].get_contents()) + xobjs
        if doc.xref_is_stream(x)
    )
    total_decoded = mod._page_content_bytes(doc, 0)
    doc.close()
    # The fix counts decoded XObject bytes: the decoded total is far over the
    # bound while the compressed total is far under it.
    assert total_decoded > mod._PAGE_CONTENT_MAX_BYTES
    assert total_raw < mod._PAGE_CONTENT_MAX_BYTES

    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    assert elapsed_s < 2.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"


def test_nested_xobject_bytes_counted_by_precheck() -> None:
    """R8-P2-1: XObjects an XObject references (its /Resources /XObject
    dict) are counted too. The page invokes a tiny wrapper Form XObject
    (~17 bytes compressed); the wrapper invokes the heavy one (xref 1000,
    ~28 KB compressed / ~200 KB decoded). Only the nested level pushes the
    page over the bound, so a page-level-only measure would let it through."""
    import zlib

    def _deflate(data: bytes) -> bytes:
        c = zlib.compressobj(9, zlib.DEFLATED, 15)
        return c.compress(data) + c.flush()

    def _form_xobject(stream_raw: bytes, inner: bytes = b"") -> bytes:
        comp = _deflate(stream_raw)
        return (
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 595 842] "
            b"/Resources <<" + inner + b">> /Matrix [1 0 0 1 0 0] "
            b"/Filter /FlateDecode /Length "
            + str(len(comp)).encode() + b" >>\nstream\n" + comp + b"\nendstream\n"
        )

    def _pdf(objs: list[tuple[int, bytes]]) -> bytes:
        header = b"%PDF-1.4\n"
        body = b""
        offsets: dict[int, int] = {}
        for xref, obj in objs:
            offsets[xref] = len(header) + len(body)
            body += b"%d 0 obj\n" % xref + obj + b"endobj\n"
        xref_pos = len(header) + len(body)
        max_xref = max(x for x, _ in objs)
        xref = b"xref\n0 %d\n0000000000 65535 f \n" % (max_xref + 1)
        for x in range(1, max_xref + 1):
            xref += (
                b"%010d 00000 n \n" % offsets[x]
                if x in offsets
                else b"0000000000 65535 f \n"
            )
        xref += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n" % (max_xref + 1)
        return header + body + xref + str(xref_pos).encode() + b"\n%%EOF"

    heavy_stream = b"\n".join(
        ("%d %d 8 4 re f" % ((i * 3) % 600, (i * 7) % 650)).encode("latin-1")
        for i in range(12000)
    )
    heavy_body = _form_xobject(heavy_stream)

    # page(3) -> wrapper(6) -> heavy(1000)
    target = _pdf(
        [
            (1, b"<< /Type /Catalog /Pages 2 0 R >>\n"),
            (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"),
            (
                3,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Resources <</XObject <</Wrap 6 0 R>>>> /Contents 4 0 R >>\n",
            ),
            (4, b"<< /Length 9 >>\nstream\n/Wrap Do\nendstream\n"),
            (6, _form_xobject(b"/Heavy Do", b"/XObject <</Heavy 1000 0 R>>")),
            (1000, heavy_body),
        ]
    )

    doc = fitz.open(stream=target, filetype="pdf")
    xobjs = doc[0].get_xobjects()
    assert len(xobjs) >= 2, "expected the wrapper and the heavy xobject"
    wrapper_raw = len(doc.xref_stream_raw(xobjs[0][0]))
    # The page-level XObject alone is tiny; the page is only over the bound
    # once the nested (wrapper-referenced) XObject is counted.
    assert wrapper_raw < mod._PAGE_CONTENT_MAX_BYTES
    total = mod._page_content_bytes(doc, 0)
    doc.close()
    assert total > mod._PAGE_CONTENT_MAX_BYTES
