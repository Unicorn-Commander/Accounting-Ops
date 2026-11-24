"""AO-UP-4: fitz must parse untrusted PDFs under a page cap, a time cap, and a
concurrency cap, and a partial render must be visible (never silently
"complete").

The renderer rasterized ``doc[:max_pages]`` (page cap) but with no wall-clock
or concurrency bound: a single pathological page (a crafted content stream / a
huge XObject) could hang the extraction worker forever, and unbounded page
threads thrash the GIL. Each page is now rasterized in a worker thread joined
with a deadline under a module-level semaphore; a page that overruns the budget
(or cannot get a slot in time) stops the render and the result carries
``error="render timeout (partial)"`` so a truncated tax document is visible.

Offline: a real one-page PDF (built with pymupdf) + a fake pathological page;
no network, no LLM call.
"""

from __future__ import annotations

import logging
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

    The fake page blocks its rasterizer inside the worker thread; the render
    must return (with zero pages + the error) instead of waiting for the page
    to finish.
    """
    data = _one_page_pdf()

    def _stall(page: Any, matrix: Any) -> bytes:
        # Block well past the per-page budget inside the worker thread.
        threading.Event().wait(60)
        return b""

    monkeypatch.setattr(mod, "_rasterize", _stall)

    with caplog.at_level(logging.WARNING, logger="accounting_ops.tax_doc_vlm"):
        out, error = mod._render_pdf(data, max_pages=2, dpi=150)

    # The stalled page produced no PNG; the render RETURNED (in ~10s, the
    # budget) instead of hanging. A pathological page cannot hang the worker.
    assert out == []
    # A truncated render must be visible, never silently "complete".
    assert error == "render timeout (partial)"
    # The warning carries the page count rendered (0 here) so the truncation is
    # diagnosable.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a partial render must log a WARNING"
    assert any("partial" in r.getMessage() for r in warnings)


def test_render_slot_limits_concurrency(monkeypatch) -> None:
    """The semaphore bounds concurrent page renders to _RENDER_CONCURRENCY.

    With the slot timeout set to 0, a third render that cannot get a slot must
    fail fast with the partial-timeout error instead of queuing.
    """
    data = _one_page_pdf()
    monkeypatch.setattr(mod, "_RENDER_SLOT_TIMEOUT_S", 0.0)

    # Hold both slots with two blocked rasterizers, then try a third page.
    held = [threading.Event() for _ in range(mod._RENDER_CONCURRENCY)]

    def _block(page: Any, matrix: Any) -> bytes:
        # Block until released; simulates a slow page holding a slot.
        held[0].wait(60)
        return b""

    monkeypatch.setattr(mod, "_rasterize", _block)
    # Acquire the slots directly so the third render cannot get one.
    for _ in range(mod._RENDER_CONCURRENCY):
        mod._render_slot.acquire()

    try:
        out, error = mod._render_pdf(data, max_pages=1, dpi=150)
    finally:
        for _ in range(mod._RENDER_CONCURRENCY):
            mod._render_slot.release()

    # No slot was available within the (zero) timeout: the render failed fast
    # with the partial-timeout error and produced no pages.
    assert out == []
    assert error == "render timeout (partial)"


def test_render_returns_pngs_for_good_pages() -> None:
    """Control: normal pages still rasterize to PNG bytes, no error."""
    data = _one_page_pdf()
    out, error = mod._render_pdf(data, max_pages=1, dpi=150)
    assert error is None
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")
