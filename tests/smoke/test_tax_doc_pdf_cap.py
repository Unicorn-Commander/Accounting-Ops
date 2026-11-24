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


# ---------------------------------------------------------------------------
# AO108-R10-P2-2: the bounded decoded content-size pre-check
# ---------------------------------------------------------------------------


def _heavy_content_stream() -> bytes:
    """A decoded content stream of ~200 KB of rect ops, far over the 128 KB
    bound. Used as the payload of the Flate-compressed heavy page below."""
    return b"\n".join(
        ("%d %d 8 4 re f" % ((i * 3) % 600, (i * 7) % 650)).encode("latin-1")
        for i in range(12000)
    )


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


def _minimal_pdf(objs: list[tuple[int, bytes]]) -> bytes:
    """A minimal valid single-page PDF with the given objects (explicit xrefs)."""
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


def _flate_heavy_page_pdf() -> bytes:
    """A one-page PDF whose content stream is a SINGLE Flate stream: tiny raw
    (deflate of ~200 KB of rect ops), huge decoded (over the 128 KB bound)."""
    ops = _heavy_content_stream()
    comp = _deflate(ops)
    return _minimal_pdf(
        [
            (1, b"<< /Type /Catalog /Pages 2 0 R >>\n"),
            (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"),
            (
                3,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Contents 4 0 R >>\n",
            ),
            (
                4,
                b"<< /Length "
                + str(len(comp)).encode()
                + b" /Filter /FlateDecode >>\nstream\n"
                + comp
                + b"\nendstream\n",
            ),
        ]
    )


def test_flate_heavy_page_refused_by_bounded_precheck() -> None:
    """R10-P2-2(a): a page whose content stream is ONE Flate stream with a RAW
    size under the bound but a DECODED size over it must be refused by the
    bounded pre-check. The test asserts raw < bound < decoded in itself: a
    raw-size (compressed) measure would let this page through, and an unbounded
    decode would inflate the whole stream. The bounded measure refuses it fast.
    """
    data = _flate_heavy_page_pdf()
    doc = fitz.open(stream=data, filetype="pdf")
    xref = doc[0].get_contents()[0]
    raw_len = len(doc.xref_stream_raw(xref))
    # The test itself proves the raw/decoded distinction: the compressed
    # stream is small, the decoded operator list is over the bound.
    assert raw_len < mod._PAGE_CONTENT_MAX_BYTES, (
        f"test precondition failed: raw {raw_len} not under the bound"
    )
    decoded_len = len(zlib.decompress(doc.xref_stream_raw(xref)))
    assert decoded_len > mod._PAGE_CONTENT_MAX_BYTES, (
        f"test precondition failed: decoded {decoded_len} not over the bound"
    )
    doc.close()

    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    # The refusal comes from the bounded pre-check (a fitz open + a bounded
    # Flate decode that stops at the bound), not the per-page budget: it must
    # be well under the budget.
    assert elapsed_s < 2.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"


def test_compression_bomb_page_refused_without_inflate() -> None:
    """R10-P2-2(b): a Flate compression bomb (a content stream that is tiny raw
    but decodes to hundreds of MB) must be refused by the bounded pre-check,
    and the test process's RSS must grow by less than 50 MB (the bounded
    decoder never inflates the whole stream)."""
    # A bomb: 512 MiB of spaces compresses to ~0.5 MiB raw but decodes to
    # 512 MiB. Far over the 128 KB bound; an unbounded decode would balloon
    # the process.
    bomb = b" " * (512 * 1024 * 1024)
    comp = zlib.compress(bomb, 9)
    data = _minimal_pdf(
        [
            (1, b"<< /Type /Catalog /Pages 2 0 R >>\n"),
            (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"),
            (
                3,
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                b"/Contents 4 0 R >>\n",
            ),
            (
                4,
                b"<< /Length "
                + str(len(comp)).encode()
                + b" /Filter /FlateDecode >>\nstream\n"
                + comp
                + b"\nendstream\n",
            ),
        ]
    )

    ru_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0
    ru_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

    assert out == []
    assert error == "page too large (partial)"
    # The bounded decoder refused the bomb fast (no multi-GB inflate).
    assert elapsed_s < 5.0, f"bomb refusal took {elapsed_s:.1f}s (not fast)"
    # The process must not have inflated the bomb: RSS growth < 50 MB.
    # ru_maxrss is in KB on Linux.
    growth_kb = ru_after - ru_before
    assert growth_kb < 50 * 1024, (
        f"RSS grew by {growth_kb / 1024:.1f} MiB (the bomb was inflated)"
    )


def test_normal_page_passes_bounded_precheck() -> None:
    """R10-P2-2(c) control: a normal one-page W-2-like PDF (a few KB of
    decoded content) is under the bound and still rasterizes to a PNG."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "W-2 sample")
    for i in range(5):
        page.draw_rect(
            fitz.Rect(10, 10 + i, 50, 20 + i), color=(1, 0, 0), fill=(0, 1, 0)
        )
    data = doc.tobytes(deflate=True)
    doc.close()

    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    assert error is None
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")


def test_xobject_heavy_page_refused_by_precheck() -> None:
    """R10-P2-2: a page whose own content stream is tiny but invokes a heavy
    Form XObject must be refused by the decoded-size pre-check. The page
    content stream is ~9 bytes decoded; the XObject stream it invokes is a
    Flate stream whose decoded size is over the bound. A page-level-only
    measure would let it through."""
    # Build the page by invoking the heavy source page as a Form XObject
    # ("/FzFrm0 Do"): the page content stream is tiny, the XObject is heavy.
    d = fitz.open()
    page = d.new_page()
    # A heavy source page (its decoded content is over the bound).
    src = fitz.open()
    spage = src.new_page()
    for i in range(20000):
        spage.draw_rect(
            fitz.Rect(i % 595, (i * 7) % 840, (i % 595) + 2, (i * 7) % 840 + 1),
            color=(1, 0, 0),
            fill=(0, 1, 0),
        )
    src_bytes = src.tobytes()
    src.close()
    page.show_pdf_page(page.rect, fitz.open(stream=src_bytes, filetype="pdf"), 0)
    data = d.tobytes(deflate=True)
    d.close()

    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    assert elapsed_s < 3.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"
