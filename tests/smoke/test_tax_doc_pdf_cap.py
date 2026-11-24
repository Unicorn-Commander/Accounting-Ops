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


def test_slow_page_finishing_after_timeout_does_not_change_returned(monkeypatch) -> None:
    """R4-2: a page that overruns its budget but finishes shortly after the
    caller has timed out appends to its OWN local list; the caller has already
    returned its partial result and never reads that list, so the late page
    cannot mutate the returned value."""
    data = _one_page_pdf()
    monkeypatch.setattr(mod, "_RENDER_PAGE_BUDGET_S", 0.1)

    release = threading.Event()

    def _slow_rasterize(page: Any, matrix: Any) -> bytes:
        # Block just past the caller's (0.1s) join, then produce a marker PNG.
        release.wait(0.5)
        return b"\x89PNG-LATE"

    monkeypatch.setattr(mod, "_rasterize", _slow_rasterize)
    returned = mod._render_pdf(data, max_pages=1, dpi=72)
    assert returned == ([], "render timeout (partial)")

    # The worker is still blocked; unblock it so it finishes and appends to its
    # own list. Wait for the worker thread to finish (its join budget is 0.1s,
    # the worker takes ~0.5s, so it outlives the join by ~0.4s).
    release.set()
    deadline = time.monotonic() + 10
    while threading.active_count() > 1 and time.monotonic() < deadline:
        time.sleep(0.01)

    # The returned value is a plain list; the late append went to the worker's
    # local list, not to it. The returned list must still be empty + partial.
    assert list(returned[0]) == []
    assert returned[1] == "render timeout (partial)"


def test_thread_start_raising_releases_slot(monkeypatch) -> None:
    """R4-3: if thread.start() raises, the caller must release the slot it
    acquired and re-raise, so the slot count is unchanged and the worker never
    holds a slot it will never release (the worker's own finally is the normal
    release path; it does not run if the thread never started)."""
    data = _one_page_pdf()

    real_thread = threading.Thread

    def _start_raises(self) -> None:
        raise RuntimeError("thread spawn refused")

    monkeypatch.setattr(real_thread, "start", _start_raises)
    before = mod._render_slot._value
    try:
        mod._render_pdf(data, max_pages=1, dpi=150)
        raise AssertionError("thread.start raised; _render_pdf must re-raise")
    except RuntimeError:
        pass
    # The slot acquired for the page was released on the start() failure: the
    # semaphore is back at its full count (no leaked slot).
    assert mod._render_slot._value == before


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

        def get_contents(self) -> list[int]:
            # The pre-check (R5-P2-2) measures the page's content stream via
            # get_contents() before rasterizing. The fake stands in for the
            # real page: report no content streams so the pre-check passes
            # (the test is about the join budget / doc ownership, not the
            # pre-check).
            return []

    class _FakeDoc:
        # The caller opens the doc to read page_count for the loop bound
        # (R4-1) before the workers open their own docs; the fake stands in
        # for that open too, so it must expose page_count (one page).
        page_count = 1

        def __getitem__(self, i: int) -> _FakePage:
            return _FakePage()

        def close(self) -> None:
            # The test shares one real doc between the caller's page_count open
            # and the worker's open (both go through the same fitz.open mock),
            # so the second close gets a ValueError from the already-closed real
            # doc. Increment the counter (the unit under test) and tolerate the
            # double-close of the shared real doc.
            closed["count"] += 1
            try:
                doc.close()
            except ValueError:
                pass

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
