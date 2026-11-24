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
    the parent waits on the child's PIPE (``poll(budget)``): a page still
    rasterizing past the budget is KILLED by the OS and counted as a visible
    ``render timeout (partial)`` (waiting on the pipe, not the process, is what
    keeps a finished child's large PNG reachable);
  - bounds how many render processes are live at once with a MODULE-level
    ``threading.BoundedSemaphore`` (across all concurrent ``_render_pdf``
    calls, not per call).

Offline: real PDFs built with pymupdf, a fake heavy/slow/dying render child;
no network, no LLM call. The render path is the real one (a spawned process);
the fake is injected only for the heavy/slow/dying pages the real MuPDF
rasterizer would not produce in a test.
"""

from __future__ import annotations

import ctypes
import logging
import os
import resource
import time
import zlib
from typing import Any

import fitz  # pymupdf (fitz is its deprecated alias)
import pytest

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
    is ~3 batches of _HOLD_S (the bound), not 1 batch (no bound).

    Also registers its own pid in the dir named by the AO_RENDER_PID_DIR env
    var (one file per live child) so the concurrency test can count live
    render children by a robust process count instead of a cmdline guess. The
    child removes its pid file on the way out (finally)."""
    import pymupdf

    pid_dir = os.environ.get("AO_RENDER_PID_DIR")
    my_pid_file = f"{pid_dir}/{os.getpid()}" if pid_dir else None
    if my_pid_file:
        with open(my_pid_file, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))

    try:
        matrix = pymupdf.Matrix(zoom, zoom)
        wdoc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        pix = wdoc[page_index].get_pixmap(matrix=matrix, alpha=False)
        wdoc.close()
        conn.send(pix.tobytes("png"))
        # Hold the process (and thus its semaphore slot) live past the send so
        # the concurrent calls overlap; the parent's join reaps it on exit.
        time.sleep(_HOLD_S)
    finally:
        if my_pid_file and os.path.exists(my_pid_file):
            os.unlink(my_pid_file)


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


def _slow_render_child_with_pid(
    pdf_bytes: Any, page_index: Any, zoom: Any, conn: Any
) -> None:
    """_slow_render_child + a pid record: burns CPU past the per-page budget
    (a CPU-bound rasterizer holds the GIL and is the thing the kill must
    interrupt), then keeps the process alive so the parent's poll(budget)
    times out and the parent must kill it. Records its pid to a file first
    (the test asserts the child is DEAD right after the timeout returns)."""
    _record_child_pid()
    _slow_render_child(pdf_bytes, page_index, zoom, conn)


def _record_child_pid() -> None:
    """Module-level (spawn can pickle it): writes the child's own pid to the
    file named by the AO_RENDER_PID_FILE env var. A spawn child is a separate
    process, so neither a module global nor a file path can cross the boundary
    by reference -- but an env var is inherited by the child at spawn time, so
    the child writes its pid to the path the test chose. The test sets the env
    var before the render; the child reads it and writes its pid there."""
    path = os.environ.get("AO_RENDER_PID_FILE")
    if path is None:
        return
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(str(os.getpid()))


def test_render_timeout_kills_child(monkeypatch, caplog) -> None:
    """R11-P1-1 + P3: a page that overruns the per-page budget is KILLED by
    the OS, the render returns with a visible ``render timeout (partial)`` and
    zero pages, and the render child is DEAD right after the timeout returns.

    The fake render child (injected for the heavy page, standing in for a
    pathological rasterize) burns CPU past the budget; the parent's
    ``poll(budget)`` must time out and ``proc.kill()`` must reap it.
    """
    import tempfile

    data = _one_page_pdf()
    pid_file = tempfile.mkstemp(suffix=".pid")[1]
    # The fake heavy render child (module-level so spawn can pickle it) burns
    # CPU past the budget then stays alive: the parent must poll(budget), see
    # no data, and kill the still-alive child. The pid-recording variant writes
    # the child pid to pid_file (spawn is a separate process, so the test passes
    # the path via an env var the child inherits; the child writes its pid there).
    monkeypatch.setenv("AO_RENDER_PID_FILE", pid_file)
    monkeypatch.setattr(mod, "_RENDER_PAGE_BUDGET_S", 2.0)
    monkeypatch.setattr(mod, "_render_one_page", _slow_render_child_with_pid)

    with caplog.at_level(logging.WARNING, logger="accounting_ops.tax_doc_vlm"):
        t0 = time.monotonic()
        out, error = mod._render_pdf(data, max_pages=2, dpi=150)
        elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "render timeout (partial)"
    # The parent did not wait out the 60s fake page: the budget (2s) + kill
    # bookkeeping is the only time spent.
    assert elapsed_s < 10.0, f"render took {elapsed_s:.1f}s (kill did not fire)"
    # R11-P3: ASSERT the child is dead right after the timeout returns. The
    # child wrote its pid to pid_file (spawn is a separate process).
    render_pid = None
    if os.path.exists(pid_file):
        with open(pid_file, "r", encoding="utf-8") as fh:
            text = fh.read().strip()
        if text:
            render_pid = int(text)
    assert render_pid is not None, "no render child was started"
    try:
        os.kill(render_pid, 0)
        alive = True
    except OSError:
        alive = False
    assert not alive, f"render child {render_pid} still alive after the timeout"
    # A partial render must log a WARNING.
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a partial render must log a WARNING"
    assert any("partial" in r.getMessage() for r in warnings)


def _noise_page_pdf() -> bytes:
    """A one-page PDF with a full-page noise image: its PNG is well over the
    64 KiB pipe buffer (the size that deadlocked the process-join wait)."""
    import pymupdf

    # A 2000x2000 grayscale block-noise page. Per-pixel random bytes
    # compress well under PNG's predictors, but value noise (8x8 blocks,
    # nearest-upsampled) is incompressible: its PNG is ~280 KiB, well over
    # the 64 KiB pipe buffer (the size that deadlocked a process-join wait).
    w = h = 2000
    block = 8
    bw, bh = w // block + 1, h // block + 1
    grid = os.urandom(bw * bh)
    px = bytearray()
    for y in range(h):
        gy = min(y // block, bh - 1)
        row = grid[gy * bw : (gy + 1) * bw]
        for x in range(w):
            px.append(row[min(x // block, bw - 1)])
    # A grayscale block-noise pixmap; samples length is w*h. pymupdf has no
    # frombytes: from_buffer on samples_mv shares the pixmap's memory (a
    # from_buffer_copy would write a throwaway copy), so memmove the noise in.
    pix = pymupdf.Pixmap(pymupdf.csGRAY, (0, 0, w, h))
    n = len(px)
    arr = (ctypes.c_char * n).from_buffer(pix.samples_mv)
    ctypes.memmove(ctypes.addressof(arr), bytes(px), n)
    # A lossless PNG of the incompressible noise (lossy JPEG would shrink it).
    png = pix.tobytes("png")
    doc = pymupdf.open()
    page = doc.new_page(width=w, height=h)
    page.insert_image(page.rect, stream=png)
    data = doc.tobytes()
    doc.close()
    return data


def test_large_png_page_renders_no_timeout() -> None:
    """R11-P1-1: a page whose PNG is well over the 64 KiB pipe buffer must
    render one page with no error. The parent waits on the pipe (poll +
    recv), not on the process, so a child blocked in conn.send with a large
    PNG is collected as a success instead of being killed as a timeout.
    """
    data = _noise_page_pdf()
    out, error = mod._render_pdf(data, max_pages=1, dpi=72)
    assert error is None, f"large PNG page failed: {error!r}"
    assert len(out) == 1
    assert out[0].startswith(b"\x89PNG")
    # The precondition that makes this test meaningful: the rendered PNG is
    # well over the pipe buffer (64 KiB on this kernel).
    assert len(out[0]) > 200_000, (
        f"test page PNG is only {len(out[0])} bytes (not over the pipe buffer)"
    )


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


def test_pipe_failure_releases_render_slot(monkeypatch) -> None:
    """R11-P2-3: a Pipe() failure (EMFILE under fd pressure) must not leak a
    render slot: the call returns a visible error and the semaphore value is
    back to _RENDER_CONCURRENCY. The old code bound parent_conn inside the
    try, so the finally raised UnboundLocalError before reaching the
    release, leaking the slot permanently."""
    import errno

    ctx = mod._RENDER_MP_CTX

    def _failing_pipe(duplex: bool = True):
        raise OSError(errno.EMFILE, "too many open files")

    monkeypatch.setattr(ctx, "Pipe", _failing_pipe, raising=False)
    data = _one_page_pdf()

    out, error = mod._render_pdf(data, max_pages=1, dpi=150)

    assert out == []
    assert error, "a Pipe() failure must give a visible error"
    assert not isinstance(error, Exception)
    # The slot must be back: the semaphore value is at its full concurrency.
    assert mod._RENDER_SLOT._value == mod._RENDER_CONCURRENCY, (
        f"render slot leaked: semaphore value {mod._RENDER_SLOT._value} "
        f"!= {mod._RENDER_CONCURRENCY}"
    )


def _live_render_child_count(pid_dir: str) -> int:
    """Count live render children: each fake render child writes its own pid
    file into ``pid_dir`` on entry and removes it on exit (finally), so the
    file count is a robust LIVE-PROCESS count (no cmdline guess, which cannot
    see the spawn child's target -- the target is sent over the pipe, not in
    the cmdline)."""
    try:
        return len(os.listdir(pid_dir))
    except OSError:
        return 0


def test_render_concurrency_bounded_across_calls(monkeypatch) -> None:
    """R10-P2-1(b) + R11-P3: N concurrent _render_pdf calls never have more
    than _RENDER_CONCURRENCY live render processes at once. The module-level
    semaphore is acquired in the parent around each page render (pipe + start
    + wait), so a call that cannot get a slot blocks until one frees up.

    The bound is proven by a LIVE-PROCESS COUNT, not by wall time: a sampler
    thread records the max number of live render children (each child registers
    its own pid file) while 6 concurrent renders overlap. The fake render child
    holds its page _HOLD_S past the pixmap so the renders overlap; it is
    restored via monkeypatch so it cannot leak into later tests.
    """
    import concurrent.futures
    import tempfile
    import threading as _threading

    data = _one_page_pdf()
    # The fake render child holds its page _HOLD_S past the pixmap so the 6
    # concurrent calls overlap. monkeypatch restores mod._render_one_page
    # after the test (no leak into later tests).
    monkeypatch.setattr(mod, "_render_one_page", _slow_success_render_child)
    # The fake render child registers its own pid in pid_dir on entry and
    # removes it on exit (finally), so counting the dir is a robust
    # LIVE-PROCESS count (a /proc cmdline scan cannot see the spawn child's
    # target -- the target travels over the pipe, not the cmdline).
    pid_dir = tempfile.mkdtemp(prefix="ao_render_pids_")
    monkeypatch.setenv("AO_RENDER_PID_DIR", pid_dir)

    peak = 0
    peak_lock = _threading.Lock()
    stop = _threading.Event()

    def sampler() -> None:
        nonlocal peak
        while not stop.is_set():
            live = _live_render_child_count(pid_dir)
            with peak_lock:
                if live > peak:
                    peak = live
            time.sleep(0.02)

    sam = _threading.Thread(target=sampler, daemon=True)
    sam.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = [
                pool.submit(mod._render_pdf, data, max_pages=1, dpi=72)
                for _ in range(6)
            ]
            results = [f.result() for f in futs]
    finally:
        stop.set()
        sam.join(2)

    for out, error in results:
        assert error is None
        assert len(out) == 1
        assert out[0].startswith(b"\x89PNG")
    assert peak > 0, "the sampler never observed a live render process"
    assert peak <= mod._RENDER_CONCURRENCY, (
        f"{peak} render processes were live at once (bound "
        f"{mod._RENDER_CONCURRENCY} exceeded)"
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
    """A Form XObject object with ``stream_raw`` as its (unfiltered) decoded
    content. Building the heavy fixture this way (raw stream bytes, no
    20,000 draw_rect calls) makes the test run in seconds. ``inner`` is an
    extra /Resources /XObject entry dict (for nested XObjects)."""
    return (
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 595 842] "
        b"/Resources <<" + inner + b">> /Matrix [1 0 0 1 0 0] "
        b"/Length "
        + str(len(stream_raw)).encode()
        + b" >>\nstream\n"
        + stream_raw
        + b"\nendstream\n"
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


def test_truncated_flate_stream_refused_fast() -> None:
    """R11-P2-1: a crafted stored-block zlib stream (header 78 01, one stored
    block, then empty stored blocks, ~1 MB) that never reaches eof must be
    refused as truncated, in under 1 s of CPU. The old decoder re-fed the
    whole raw stream each pass (one decoded byte per pass over the whole
    stream: ~65,000 passes); the fixed decoder feeds raw once then only
    decomp.unconsumed_tail, so it exhausts the input in one pass and refuses
    the missing eof.
    """
    # zlib header (78 01: deflate, no dict) + one stored block carrying the
    # payload + ~10 empty stored blocks, so the input is well over the 128 KB
    # bound and the stream is truncated (no eof block). The payload is kept
    # under 65535 bytes so the stored-block length fits the 2-byte LEN field.
    payload = b" " * 60000
    block_len = len(payload)
    stream = (
        b"\x78\x01"
        + bytes([0])
        + block_len.to_bytes(2, "little")
        + (~block_len & 0xFFFF).to_bytes(2, "little")
        + payload
        + b"\x00\x00\x00\x00" * 160
    )
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
                + str(len(stream)).encode()
                + b" /Filter /FlateDecode >>\nstream\n"
                + stream
                + b"\nendstream\n",
            ),
        ]
    )
    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    assert elapsed_s < 1.0, (
        f"truncated stream refusal took {elapsed_s:.1f}s (not bounded)"
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


def _content_pdf(filter_spec: bytes, stream_bytes: bytes) -> bytes:
    """A one-page PDF whose single content stream has ``filter_spec`` (a raw
    /Filter value: a name, an array, or an indirect ref) and the given
    (already-encoded) stream bytes."""
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
                + str(len(stream_bytes)).encode()
                + b" /Filter "
                + filter_spec
                + b" >>\nstream\n"
                + stream_bytes
                + b"\nendstream\n",
            ),
        ]
    )


def test_flate_array_filter_heavy_page_refused_by_precheck() -> None:
    """R11-P2-2: the ARRAY form /Filter [/FlateDecode] with a heavy stream must
    be refused exactly like the bare-name form. The old code only
    recognised the bare name and counted the array form at its raw size, so a
    tiny compressed stream overran the bound undetected."""
    ops = _heavy_content_stream()
    comp = _deflate(ops)
    data = _content_pdf(b"[ /FlateDecode ]", comp)
    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    assert elapsed_s < 2.0, f"array-filter refusal took {elapsed_s:.1f}s"


def test_runlength_filter_page_refused_by_precheck() -> None:
    """R11-P2-2: a /RunLengthDecode content stream (which decodes larger than
    its raw size and that the bounded decoder does not understand) must be
    refused, not counted at its raw size."""
    # A run-length stream: pairs of (repeat count, byte). 20000 x "A".
    raw = b"\x4e" * 10000  # 78 = 0x4E: repeat the next byte 79 times
    data = _content_pdf(b"/RunLengthDecode", raw)
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)

    assert out == []
    assert error == "page too large (partial)"


def _stream_doc(data: bytes) -> tuple[Any, int]:
    """Open ``data`` and return (doc, xref of page 0's first content stream)."""
    doc = fitz.open(stream=data, filetype="pdf")
    return doc, doc[0].get_contents()[0]


def _a85(data: bytes) -> bytes:
    """ASCII85 as a PDF /ASCII85Decode stream carries it (no <~, ends ~>)."""
    import base64

    return base64.a85encode(data, adobe=False) + b"~>"


def _light_ops() -> bytes:
    return b"BT /F1 12 Tf 72 720 Td (W-2 sample) Tj ET\n0 0 1 rg 10 10 50 20 re f"


def test_light_flate_array_filter_page_renders() -> None:
    """R12-P2-A: a LIGHT page whose /Filter is the array [/FlateDecode] (no
    space before "]") renders. The old tokeniser read "/FlateDecode]" and
    refused every array-form filter page."""
    data = _content_pdf(b"[/FlateDecode]", _deflate(_light_ops()))
    doc, xref = _stream_doc(data)
    assert mod._filter_list(doc, xref) == ["/FlateDecode"]
    assert mod._decoded_stream_bytes(doc, xref, mod._PAGE_CONTENT_MAX_BYTES) == len(
        _light_ops()
    )
    doc.close()

    out, error = mod._render_pdf(data, max_pages=2, dpi=72)
    assert error is None
    assert len(out) == 1 and out[0].startswith(b"\x89PNG")


def test_reportlab_one_line_page_renders() -> None:
    """R12-P2-A: ReportLab's default output ([/ASCII85Decode/FlateDecode] on
    the content stream) passes the pre-check via bounded A85 then bounded
    Flate, and renders."""
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "W-2 sample for Example Payroll Co")
    c.save()
    data = buf.getvalue()

    doc, xref = _stream_doc(data)
    assert mod._filter_list(doc, xref) == ["/ASCII85Decode", "/FlateDecode"]
    assert 0 < mod._decoded_stream_bytes(doc, xref, mod._PAGE_CONTENT_MAX_BYTES) < 1024
    doc.close()

    out, error = mod._render_pdf(data, max_pages=2, dpi=72)
    assert error is None
    assert len(out) == 1 and out[0].startswith(b"\x89PNG")


def test_heavy_a85_flate_chain_refused_by_decoded_size() -> None:
    """R12-P2-A: the supported chain with a HEAVY decoded stream is refused by
    the decoded-size bound (the bounded Flate behind the A85), not by an
    "unsupported filter" refusal."""
    data = _content_pdf(
        b"[ /ASCII85Decode /FlateDecode ]", _a85(_deflate(_heavy_content_stream()))
    )
    doc, xref = _stream_doc(data)
    with pytest.raises(ValueError, match="flate stream exceeds the content bound"):
        mod._decoded_stream_bytes(doc, xref, mod._PAGE_CONTENT_MAX_BYTES)
    doc.close()

    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    assert out == []
    assert error == "page too large (partial)"


def test_a85_z_run_refused_before_decoding() -> None:
    """R12-P2-A: an A85 body of "z" groups (1 char -> 4 zero bytes) would
    decode larger than its raw size: refused before the A85 decode."""
    data = _content_pdf(b"[/ASCII85Decode/FlateDecode]", b"z" * 50000 + b"~>")
    doc, xref = _stream_doc(data)
    with pytest.raises(ValueError, match="ascii85 stream would decode larger"):
        mod._decoded_stream_bytes(doc, xref, mod._PAGE_CONTENT_MAX_BYTES)
    doc.close()


def test_heavy_flate_array_refused_by_decoded_size() -> None:
    """R12-P2-A: the heavy [/FlateDecode] (no spaces) array page is refused by
    the bounded Flate (decoded size), now that the name tokenises."""
    data = _content_pdf(b"[/FlateDecode]", _deflate(_heavy_content_stream()))
    doc, xref = _stream_doc(data)
    with pytest.raises(ValueError, match="flate stream exceeds the content bound"):
        mod._decoded_stream_bytes(doc, xref, mod._PAGE_CONTENT_MAX_BYTES)
    doc.close()


@pytest.mark.parametrize(
    "filter_spec, stream",
    [
        (b"/RunLengthDecode", b"\x4e" * 10000),
        (b"[/RunLengthDecode]", b"\x4e" * 10000),
        (b"[/ASCII85Decode /LZWDecode]", _a85(b"x" * 100)),
        (b"[/FlateDecode /FlateDecode]", _deflate(_deflate(b"x" * 100))),
        (b"[/ASCIIHexDecode /FlateDecode]", _deflate(b"x" * 100).hex().encode()),
    ],
)
def test_other_filters_and_chains_refused_as_unsupported(
    filter_spec: bytes, stream: bytes
) -> None:
    """R12-P2-A: any filter or chain other than none, [/FlateDecode] and
    [/ASCII85Decode /FlateDecode] is refused as UNSUPPORTED (even light)."""
    data = _content_pdf(filter_spec, stream)
    doc, xref = _stream_doc(data)
    with pytest.raises(ValueError, match="unsupported content stream filter"):
        mod._decoded_stream_bytes(doc, xref, mod._PAGE_CONTENT_MAX_BYTES)
    doc.close()

    out, error = mod._render_pdf(data, max_pages=2, dpi=72)
    assert out == []
    assert error == "page too large (partial)"


def _xobject_heavy_page_pdf() -> bytes:
    """A one-page PDF whose content stream is tiny (one "/FzFrm0 Do") but
    invokes a Form XObject whose decoded stream is over the bound. Built
    directly from stream bytes (fast), not by drawing 20,000 rects."""
    heavy = _heavy_content_stream()
    objs = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>\n"),
        (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"),
        (
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /XObject << /FzFrm0 5 0 R >> >> "
            b"/Contents 4 0 R >>\n",
        ),
        (
            4,
            b"<< /Length 9 >>\nstream\n/FzFrm0 Do\nendstream\n",
        ),
        (5, _form_xobject(heavy)),
    ]
    return _minimal_pdf(objs)


def test_xobject_heavy_page_refused_by_precheck() -> None:
    """R10-P2-2: a page whose own content stream is tiny but invokes a heavy
    Form XObject must be refused by the decoded-size pre-check. The page
    content stream is ~10 bytes decoded; the XObject stream it invokes is
    over the bound. A page-level-only measure would let it through."""
    data = _xobject_heavy_page_pdf()
    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    assert elapsed_s < 3.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"


def test_nested_xobject_bytes_counted_by_precheck() -> None:
    """R11-P3: a page whose Form XObject itself invokes a nested heavy Form
    XObject (one nesting level, via the XObject's /Resources /XObject dict)
    must be refused: the pre-check follows XObjects an XObject references."""
    heavy = _heavy_content_stream()
    # The outer XObject (5 0 R) has a tiny stream and a flat /Resources
    # /XObject dict (inline here) naming the nested heavy XObject (6 0 R).
    # The pre-check follows that dict one nesting level deep.
    objs = [
        (1, b"<< /Type /Catalog /Pages 2 0 R >>\n"),
        (2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>\n"),
        (
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /XObject << /FzFrm0 5 0 R >> >> "
            b"/Contents 4 0 R >>\n",
        ),
        (
            4,
            b"<< /Length 9 >>\nstream\n/FzFrm0 Do\nendstream\n",
        ),
        (
            5,
            b"<< /Type /XObject /Subtype /Form /BBox [0 0 595 842] "
            b"/Resources << /XObject << /FzFrm1 6 0 R >> >> "
            b"/Matrix [1 0 0 1 0 0] /Length 9 "
            b">>\nstream\n/FzFrm1 Do\nendstream\n",
        ),
        # The nested XObject: the heavy decoded stream.
        (6, _form_xobject(heavy)),
    ]
    data = _minimal_pdf(objs)

    t0 = time.monotonic()
    out, error = mod._render_pdf(data, max_pages=2, dpi=150)
    elapsed_s = time.monotonic() - t0

    assert out == []
    assert error == "page too large (partial)"
    assert elapsed_s < 3.0, f"pre-check refusal took {elapsed_s:.1f}s (not fast)"
