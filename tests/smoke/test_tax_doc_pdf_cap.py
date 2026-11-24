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

    # The caller must NOT have closed the doc or released the slot: the worker
    # still owns both (it is blocked in get_pixmap). The semaphore's value is
    # 1 (one of two slots held) rather than 2 (both free).
    assert closed["count"] == 0, "caller must not close the doc while the worker holds it"
    assert mod._render_slot._value == mod._RENDER_CONCURRENCY - 1, (
        f"the slot must still be held by the worker (value={mod._render_slot._value})"
    )

    # Unblock the worker: it finishes get_pixmap, then its own finally closes
    # the doc (once) and releases the slot. Wait until the worker has closed the
    # doc (its finally runs close() before release(), so close count 1 implies
    # the slot is released too).
    release.set()
    deadline = time.monotonic() + 10
    while closed["count"] < 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert closed["count"] == 1, "the worker must close the doc exactly once"
    # The worker's finally released the slot after close(): all slots are free.
    assert mod._render_slot._value == mod._RENDER_CONCURRENCY, (
        f"the worker must have released the slot (value={mod._render_slot._value})"
    )
