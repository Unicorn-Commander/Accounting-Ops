"""AO-UP-4: untrusted PDFs must parse under a page cap, a bounded content-size
pre-check, a per-page process-kill time cap, and a module-level concurrency
cap, and a partial render must be visible (never silently "complete").

The renderer now:
  - caps the page count at ``min(max_pages, doc.page_count)`` (a one-page W-2
    renders exactly one page, never an IndexError);
  - runs a BOUNDED decoded content-size pre-check (zlib max_length) that refuses
    a page over the bound before it can burn a render process, including a
    Flate-compressed page and a compression bomb;
  - rasterizes each surviving page in its own SPAWNED process (never fork) and
    the parent does ``proc.join(budget)``: a page still rasterizing past the
    budget is KILLED by the OS and counted as a visible
    ``render timeout (partial)`` (a thread join cannot interrupt the
    GIL-holding C rasterizer);
  - bounds how many render processes are live at once with a MODULE-level
    ``threading.BoundedSemaphore`` (across all concurrent ``_render_pdf``
    calls, not per call).

Offline: real PDFs built with pymupdf, a fake heavy/slow/dying render child;
no network, no LLM call. The render path is the real one (a spawned process);
the fake is injected only for the heavy/slow/dying pages the real MuPDF
rasterizer would not produce in a test.
"""

from __future__ import annotations

import logging
import os
import resource
import time
import zlib
from typing import Any

import fitz  # pymupdf (fitz is its deprecated alias)

import app.categorization.tax_doc_vlm as mod

# ---------------------------------------------------------------------------
# Fake render children (module-level so they are picklable by the spawn
# context; spawn re-imports the module and looks them up by qualified name).
# ---------------------------------------------------------------------------


def _slow_render_child(pdf_bytes: Any, page_index: Any, zoom: Any, conn: Any) -> None:
    """Burn CPU past the per-page budget (a CPU-bound rasterizer holds the GIL
    and is the thing the kill must interrupt), then keep the process alive so
    the parent's join(budget) times out and the parent must kill it."""
    x = 0
    for _ in range(10**7):
        x += 1
    time.sleep(60)


def _dying_render_child(pdf_bytes: Any, page_index: Any, zoom: Any, conn: Any) -> None:
    """Exit without a result (os._exit(9)): the parent must see a dead child
    with no pipe item and report a visible ``render failed``."""
    os._exit(9)


# How long the fake render child holds its page past the pixmap (seconds). A
# module-level global so the spawned child can read it (spawn re-imports this
# module as __mp_main__); the concurrency test sets it to a short value so the
# test stays fast while still overlapping the renders.
_HOLD_S = 0.6


def _slow_success_render_child(
    pdf_bytes: Any, page_index: Any, zoom: Any, conn: Any
) -> None:
    """Render the page (real MuPDF) but hold it live _HOLD_S past the pixmap
    so concurrent renders overlap and the module-level semaphore is saturated.
    The parent holds the slot for start + join(budget); with 6 concurrent calls
    and a bound of 2, at most 2 children are live at once, so total wall time
    is ~3 batches of _HOLD_S (the bound), not 1 batch (no bound)."""
    import pymupdf

    matrix = pymupdf.Matrix(zoom, zoom)
    wdoc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = wdoc[page_index].get_pixmap(matrix=matrix, alpha=False)
    wdoc.close()
    conn.send(pix.tobytes("png"))
    # Hold the process (and thus its semaphore slot) live past the send so the
    # concurrent calls overlap; the parent's join reaps it on exit.
    time.sleep(_HOLD_S)


def _one_page_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "W-2 sample")
    data = doc.tobytes()
    doc.close()
    return data


def _three_page_pdf() -> bytes:
    doc = fitz.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_text((72, 100), f"page {i}")
    data = doc.tobytes()
    doc.close()
    return data


def test_render_respects_page_cap() -> None:
    """A 3-page PDF must rasterize only the first max_pages, no error."""
    data = _three_page_pdf()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    assert error is None
    assert len(out) == 2
    # Each page is a real PNG.
    for png in out:
        assert png.startswith(b"\x89PNG")


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


def test_render_returns_pngs_for_good_pages() -> None:
    """Control: normal pages still rasterize to PNG bytes, no error."""
    data = _one_page_pdf()
    out, error = mod._render_pdf(data, max_pages=1, dpi=150)
    assert error is None
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")


# ---------------------------------------------------------------------------
# AO108-R10-P2-1: the production render path (a spawned process per page)
# ---------------------------------------------------------------------------


def test_render_timeout_kills_child(monkeypatch, caplog) -> None:
    """R10-P2-1(a): a page that overruns the per-page budget is KILLED by the
    OS, the render returns with a visible ``render timeout (partial)`` and zero
    pages, and the render child is DEAD right after the timeout returns.

    The fake render child (injected for the heavy page, standing in for a
    pathological rasterize) burns CPU past the budget; the parent's
    ``proc.join(budget)`` must time out and ``proc.kill()`` must reap it.
    """
    data = _one_page_pdf()
    monkeypatch.setattr(mod, "_RENDER_PAGE_BUDGET_S", 2.0)
    # The fake heavy render child (module-level so spawn can pickle it) burns
    # CPU past the budget then stays alive: the parent must join(budget), see
    # it still alive, and kill it.
    monkeypatch.setattr(mod, "_render_one_page", _slow_render_child)

    with caplog.at_level(logging.WARNING, logger="accounting_ops.tax_doc_vlm"):
        t0 = time.monotonic()
        out, error = mod._render_pdf(data, max_pages=2, dpi=150)
        elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "render timeout (partial)"
    # The parent did not wait out the 60s fake page: the budget (2s) + kill
    # bookkeeping is the only time spent.
    assert elapsed_s < 10.0, f"render took {elapsed_s:.1f}s (kill did not fire)"
    # A partial render must log a WARNING.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a partial render must log a WARNING"
    assert any("partial" in r.getMessage() for r in warnings)


def test_render_child_exit_gives_render_failed(monkeypatch) -> None:
    """R10-P2-1(c): a render child that dies without a result (os._exit(9))
    gives a visible ``render failed`` error without hanging the render."""
    data = _one_page_pdf()
    # The fake dying render child (module-level so spawn can pickle it) exits
    # with no result: the parent must see a dead child with no pipe item.
    monkeypatch.setattr(mod, "_render_one_page", _dying_render_child)

    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=1, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    # A child that died without a result is a visible failure (not a timeout).
    assert "render failed" in (error or "")
    # No hang: the dead child is reaped immediately.
    assert elapsed_s < 5.0, f"render took {elapsed_s:.1f}s (should not hang)"


def test_render_concurrency_bounded_across_calls() -> None:
    """R10-P2-1(b): N concurrent _render_pdf calls never have more than
    _RENDER_CONCURRENCY live render processes at once. The module-level
    semaphore is acquired in the parent around each page render (start + join),
    so a call that cannot get a slot blocks (its process is not started) until
    one frees up."""
    import concurrent.futures

    data = _one_page_pdf()
    # The fake render child (module-level so spawn can pickle it) holds its
    # page _HOLD_S past the pixmap so the 6 concurrent calls overlap. The
    # parent holds the module-level semaphore slot for start + join(budget),
    # so a call that cannot get a slot blocks on acquire() until an earlier
    # render frees one. With a bound of 2, the 6 renders take ~3 batches of
    # _HOLD_S; with no bound they take ~1 batch. Total wall time is the
    # behavioral proof the bound is active. _HOLD_S is a true module constant
    # (spawn re-imports the module fresh, so a monkeypatched value is NOT seen
    # in the child -- it must be set at import time).
    hold = _HOLD_S
    mod._render_one_page = _slow_success_render_child
    t0 = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = [
                pool.submit(mod._render_pdf, data, max_pages=1, dpi=72)
                for _ in range(6)
            ]
            results = [f.result() for f in futs]
    finally:
        total = time.monotonic() - t0

    for out, error in results:
        assert error is None
        assert len(out) == 1
        assert out[0].startswith(b"\x89PNG")
    # 6 renders, bound 2: ~3 sequential batches of `hold` s = ~3*hold if the
    # bound is honored; ~1 batch = ~hold if all 6 ran in parallel (no bound).
    # Allow spawn + pre-check overhead on top of 3 batches.
    bound_floor = 3 * hold
    assert total >= bound_floor, (
        f"6 concurrent renders finished in {total:.2f}s: the concurrency bound "
        f"({mod._RENDER_CONCURRENCY}) was not exercised (expected >= "
        f"{bound_floor:.1f}s for 3 batches of {hold}s)"
    )
    assert total <= bound_floor + 3.0, (
        f"6 concurrent renders took {total:.2f}s: bound honored but renders "
        f"overlapped less than expected (expected ~{bound_floor:.1f}s)"
    )


