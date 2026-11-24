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


def test_worker_owns_doc_close_and_slot_on_timeout(monkeypatch) -> None:
    """R3-4: on a per-page timeout the WORKER thread owns the document handle
    and the slot. The caller must NOT close the doc or release the slot while
    the daemon thread is still inside get_pixmap (use-after-close / double
    release). So:
      * after the timeout, close() has NOT been called and the slot is held;
      * after the blocked page unblocks, close() is called exactly once and
        the slot is released.
    A fake page blocks get_pixmap on an Event; the per-page budget is shrunk
    to 0.2s so the timeout fires fast.
    """
    data = _one_page_pdf()
    # The join deadline must fire BEFORE the worker's event wait ends, so the
    # worker is still blocked (alive) when _render_pdf checks is_alive().
    monkeypatch.setattr(mod, "_RENDER_PAGE_BUDGET_S", 0.5)

    release = threading.Event()
    closed = {"count": 0}
    doc = fitz.open(stream=data, filetype="pdf")

    class _FakePage:
        def get_pixmap(self, matrix: Any, alpha: bool) -> Any:
            # Block in the worker thread until the test releases us.
            release.wait(60)
            pix = doc[0].get_pixmap(matrix=matrix, alpha=alpha)
            return pix

    class _FakeDoc:
        # The caller opens the doc to read page_count for the loop bound
        # (R4-1) before the workers open their own docs; the fake stands in
        # for that open too, so it must expose page_count (one page).
        page_count = 1

        def __getitem__(self, i: int) -> _FakePage:
            return _FakePage()

        def close(self) -> None:
            closed["count"] += 1
            doc.close()

    monkeypatch.setattr(fitz, "open", lambda *a, **kw: _FakeDoc())

    out, error = mod._render_pdf(data, max_pages=1, dpi=150)

    # The page overran the (0.2s) budget: the render returned early, marked
    # partial, and produced no page.
    assert out == []
    assert error == "render timeout (partial)"

    # The caller opened one doc to read page_count (R4-1) and closed it before
    # the loop; that is the only close so far. The WORKER's doc is still held
    # (it is blocked in get_pixmap) and must NOT have been closed by the caller
    # (use-after-close). The semaphore's value is 1 (one of two slots held)
    # rather than 2 (both free).
    assert closed["count"] == 1, (
        f"only the caller's page_count doc may be closed; the worker's doc must "
        f"still be held (closed={closed['count']})"
    )
    assert mod._render_slot._value == mod._RENDER_CONCURRENCY - 1, (
        f"the slot must still be held by the worker (value={mod._render_slot._value})"
    )

    # Unblock the worker: it finishes get_pixmap, then its own finally closes
    # its doc (once) and releases the slot. Wait until the worker has closed its
    # doc (closed count goes 1 -> 2; its finally runs close() before release(),
    # so the second close implies the slot is released too).
    release.set()
    deadline = time.monotonic() + 10
    while closed["count"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert closed["count"] == 2, (
        "the worker must close its own doc exactly once (plus the caller's "
        f"page_count doc = 2 total; closed={closed['count']})"
    )
    # The worker's finally released the slot after close(): all slots are free.
    assert mod._render_slot._value == mod._RENDER_CONCURRENCY, (
        f"the worker must have released the slot (value={mod._render_slot._value})"
    )
