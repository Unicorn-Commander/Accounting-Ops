"""AO-UP-4: fitz must parse untrusted PDFs under a page cap AND a time cap.

The renderer rasterized ``doc[:max_pages]`` (page cap) but with no wall-clock
bound: a single pathological page (a crafted content stream / a huge XObject)
could hang the extraction worker forever. Each page is now rasterized in a
worker thread joined with a deadline; a page that overruns it is dropped and
the render stops.

Offline: a real one-page PDF (built with pymupdf) + a fake pathological page;
no network, no LLM call.
"""

from __future__ import annotations

import threading
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
    """A 3-page PDF must rasterize only the first max_pages."""
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_text((72, 100), f"page {i}")
    data = doc.tobytes()
    doc.close()

    out = mod._render_pdf(data, max_pages=2, dpi=150)
    assert len(out) == 2
    # Each page is a real PNG.
    for png in out:
        assert png.startswith(b"\x89PNG")


def test_render_stops_at_time_cap(monkeypatch) -> None:
    """A page that overruns the per-page budget must be dropped, not hang.

    The fake page blocks its rasterizer inside the worker thread; the render
    must return (with zero pages) instead of waiting for the page to finish.
    The old code had no join deadline and would have blocked until the page
    finished.
    """
    data = _one_page_pdf()

    def _stall(page: Any, matrix: Any) -> bytes:
        # Block well past the per-page budget inside the worker thread.
        threading.Event().wait(60)
        return b""

    monkeypatch.setattr(mod, "_rasterize", _stall)

    out = mod._render_pdf(data, max_pages=2, dpi=150)
    # The stalled page produced no PNG; the render RETURNED (in ~10s, the
    # budget) instead of hanging. A pathological page cannot hang the worker.
    assert out == []


def test_render_returns_pngs_for_good_pages(monkeypatch) -> None:
    """Control: normal pages still rasterize to PNG bytes."""
    data = _one_page_pdf()
    out = mod._render_pdf(data, max_pages=1, dpi=150)
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")
