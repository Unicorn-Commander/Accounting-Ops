"""Tax-document classification + field extraction (local VLM, on-prem, free).

The receipt path (``receipt_vlm.py``) handles RECEIPT IMAGES and pulls
{vendor, amount, date}. Tax documents are different: multi-page scanned PDFs
(1099-R, W-2, SSA-1099, 1095, K-1, 1098, IRS/state notices) whose value is the
FORM TYPE, the TAX YEAR, the payer/recipient, and the labeled box amounts.

This module reuses the same infrastructure the receipt extractor uses — the
local vision VLM (``llm_endpoint_url`` / ``llm_model``, the Qwen3.6 vision model
on the RTX-6000 box) and the same OpenAI-compatible chat shape — but with a
tax-form prompt and a tax-shaped result. PDFs are rasterized to page images
(pymupdf) since the source docs are scans with no text layer.

Design rules (mirrors the receipt path):
  * NEVER raises for an extraction failure — returns a result with ``error`` set
    so a bad document can't break the upload. The caller treats it as advisory
    and always queues the doc for human review.
  * The model TRANSCRIBES labeled values; it does NOT compute or sum. (A bbox
    cross-check against the dots.ocr oracle, like the receipt path's
    ``_call_bbox_oracle``, is a planned follow-up — VLM-primary for v1.)
  * No money math here; downstream consumers (Tax-Planning-Ops, cpa-export) read
    the stored fields and apply their own logic.
"""

from __future__ import annotations

import base64
import json
import logging
import multiprocessing
import re
import threading
import time
import zlib
from dataclasses import dataclass, field
from typing import Any

import httpx
from starlette.concurrency import run_in_threadpool

from app.categorization.receipt_vlm import _extract_completion
from app.config import Settings, get_settings
from app.llm_client import llm_default_headers

_log = logging.getLogger("accounting_ops.tax_doc_vlm")

# Canonical tax-document types the classifier maps to. "other" = not a tax doc
# (or unrecognized) — still stored, just not typed.
TAX_DOC_TYPES = (
    "1099-R", "1099-NEC", "1099-MISC", "1099-INT", "1099-DIV", "1099-K", "1099-G",
    "W-2", "W-2G", "SSA-1099", "RRB-1099",
    "1095-A", "1095-B", "1095-C", "1098", "1098-E", "1098-T",
    "K-1", "5498", "1040", "1040-ES",
    "irs-notice", "state-tax-notice", "other",
)

# Images we can feed straight to the VLM; PDFs are rasterized first.
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/tiff", "image/gif", "image/heic", "image/heif"}
_PDF_MIMES = {"application/pdf"}

# Rasterization bounds — tax forms put their substance on the first page or two;
# capping keeps a multi-page scan (e.g. a 20-page notice) from fanning out into
# a slow many-image VLM call.
_MAX_PAGES = 2
_RENDER_DPI = 150

# Rasterizer concurrency bound: each page rasterizes in its own spawned
# process (see _render_one_page / _render_pdf); the BoundedSemaphore held
# around each page render bounds how many page render processes are live at
# once, across ALL concurrent _render_pdf calls (a module-level object, not
# per call). More than a couple of live MuPDF rasterizers just burns CPUs.
_RENDER_CONCURRENCY = 2
# Per-page wall-clock budget. The parent does proc.join(budget): a page still
# rasterizing when the budget is up is KILLED by the OS (proc.kill), which is
# the hard guarantee a thread join cannot give (the C rasterizer holds the
# GIL, so a join deadline cannot interrupt a page already inside it).
_RENDER_PAGE_BUDGET_S = 10.0
# Module-level bound on how many page render processes may be live at once
# (across every concurrent _render_pdf call, not per call). Acquired in the
# parent around each page render and released in the parent's finally.
_RENDER_SLOT = threading.BoundedSemaphore(_RENDER_CONCURRENCY)
# Spawn context: never fork (no inherited secrets/sockets, no
# fork-from-thread deadlock). A child opened via fork could inherit the
# server's connections and credentials; spawn starts from a clean interpreter.
_RENDER_MP_CTX = multiprocessing.get_context("spawn")
# Pre-check bound: a page whose DECODED content stream (plus the decoded
# streams of every Form XObject the page references, one nesting level) is
# larger than this is refused before it is rasterized. A real W-2 / 1099 /
# 1099-R page carries a few KB of operators (measured: a text-only page is
# ~200 bytes); a page over 128 KB of decoded content is pathological
# (thousands of drawing ops / a huge XObject). Decoded size is the bound, not
# the compressed (raw) stream size: a Flate-compressed content stream can be
# tiny on disk yet expand to a huge operator list that the C rasterizer must
# walk. The measure itself is BOUNDED (zlib decompressobj with max_length):
# it never inflates more than bound+1 bytes into the API process, so a
# compression-bomb stream is refused without a multi-GB decode. The refusal
# must happen BEFORE the page is ever handed to the C rasterizer, because that
# raster is the thing that would burn a render process.
_PAGE_CONTENT_MAX_BYTES = 128 * 1024

_SYSTEM_PROMPT = (
    "You are a meticulous U.S. tax-document analyzer. You are shown the page "
    "image(s) of one uploaded document. Identify the tax form type and "
    "TRANSCRIBE its labeled fields exactly as printed. Never compute, sum, or "
    "infer a value that is not printed. Return STRICT JSON only — no prose."
)


@dataclass(slots=True)
class TaxDocExtraction:
    """Result of a tax-document extraction. Always returned (never raises)."""

    doc_type: str = "other"            # one of TAX_DOC_TYPES
    tax_year: int | None = None
    payer_name: str | None = None
    recipient_name: str | None = None
    # Labeled values transcribed from the form, e.g.
    # {"box 1 gross distribution": "39612.00", "box 2a taxable amount": "UNKNOWN"}.
    fields: dict[str, str] = field(default_factory=dict)
    confidence: float | None = None
    model: str = ""
    duration_ms: int = 0
    raw_response: str = ""
    error: str | None = None

    @property
    def is_tax_document(self) -> bool:
        return self.doc_type not in ("", "other") and self.error is None

    def has_any_field(self) -> bool:
        return bool(self.fields) or self.tax_year is not None or self.payer_name


class TaxDocExtractor:
    """Classify + extract a tax document via the local vision VLM."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def can_extract(self, mime_type: str) -> bool:
        mime = (mime_type or "").split(";")[0].strip().lower()
        return mime in _IMAGE_MIMES or mime in _PDF_MIMES

    async def extract(self, *, body: bytes, mime_type: str) -> TaxDocExtraction:
        settings = self._settings
        t0 = time.monotonic()
        model = settings.llm_model
        if not body:
            return TaxDocExtraction(model=model, error="empty body")

        mime = (mime_type or "").split(";")[0].strip().lower()
        render_error: str | None = None
        # fitz PDF work (open + bounded content-stream pre-check) is CPU-bound:
        # run it off the event loop so one heavy uploaded PDF cannot block the
        # server while it is being refused. The pre-check is bounded (it never
        # inflates more than bound+1 bytes), so it is fast even for a
        # compression-bomb page. Each page that survives the pre-check is then
        # rasterized in its own spawned process (see _render_pdf) with a hard
        # kill timeout, so a pathological page that slips through cannot hold
        # the GIL, block the loop, or starve other requests. Images skip this
        # (no fitz).
        try:
            if mime in _PDF_MIMES:
                images, render_error = await run_in_threadpool(
                    _render_pdf, body, max_pages=_MAX_PAGES, dpi=_RENDER_DPI
                )
            else:
                # Image mimes pass through as a single page (no fitz, no
                # render). Any other mime is not extractable here.
                images = [body] if mime in _IMAGE_MIMES else []
                render_error = None
        except Exception as exc:  # noqa: BLE001 — render failure must not raise
            return TaxDocExtraction(
                model=model,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"render failed: {type(exc).__name__}",
            )
        if not images:
            # A render that produced no pages: surface the specific reason
            # (partial-timeout vs. no renderable pages) so a truncated tax
            # document is never silently "complete".
            return TaxDocExtraction(
                model=model,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=render_error or "no renderable pages",
            )

        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    "Classify this tax document and transcribe its fields. Return "
                    "JSON with EXACTLY these keys: "
                    '{"doc_type": one of '
                    + ", ".join(TAX_DOC_TYPES)
                    + '; "tax_year": integer or null; "payer_name": string or '
                    'null (the issuer/payer/employer/agency); "recipient_name": '
                    'string or null; "fields": an object mapping each labeled box '
                    "or line to its printed value as a string (keep the printed "
                    'text verbatim, e.g. "UNKNOWN"); "confidence": 0.0-1.0}. '
                    "Transcribe every labeled monetary box and identifying line "
                    "you can read. Do NOT add, sum, or compute values. If this is "
                    'not a tax document, set doc_type to "other".'
                ),
            }
        ]
        for png in images:
            uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": uri}})

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "max_tokens": max(settings.llm_max_tokens, 1500),
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        # Shared UA (Cloudflare in front of the UC gateway 403s the SDK/urllib
        # defaults with "error code: 1010"). NO failover here -- see the note in
        # receipt_vlm: the fallback rung is a text model, not a vision one.
        headers = llm_default_headers({"Content-Type": "application/json"})
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        url = settings.llm_endpoint_url.rstrip("/") + "/chat/completions"

        try:
            # Tax pages are denser than a receipt + there can be two of them, so
            # give the VLM more headroom than the receipt timeout.
            timeout = max(settings.llm_timeout_s, 60)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
        except httpx.TimeoutException:
            return TaxDocExtraction(
                model=model, duration_ms=int((time.monotonic() - t0) * 1000),
                error="vlm timeout",
            )
        except httpx.HTTPError as exc:
            return TaxDocExtraction(
                model=model, duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"vlm transport: {type(exc).__name__}",
            )

        duration_ms = int((time.monotonic() - t0) * 1000)
        raw = _extract_completion(resp.json())
        if not raw:
            return TaxDocExtraction(
                model=model, duration_ms=duration_ms, error="empty completion"
            )
        parsed = _parse_tax_json(raw)
        if parsed is None:
            return TaxDocExtraction(
                model=model, duration_ms=duration_ms,
                raw_response=raw[:4000], error="unparseable json",
            )
        parsed.model = model
        parsed.duration_ms = duration_ms
        parsed.raw_response = raw[:4000]
        if render_error is not None:
            # The render was partial (a page overran its budget): mark the
            # result so a truncated tax document is visible, never silently
            # "complete".
            parsed.error = render_error
        return parsed


def _render_pdf(
    pdf_bytes: bytes,
    *,
    max_pages: int,
    dpi: int,
) -> tuple[list[bytes], str | None]:
    """Rasterize the first ``max_pages`` of a PDF to PNG bytes via pymupdf.

    Returns ``(pages, error)`` where ``error`` is set when the render was
    partial (a page overran its per-page budget, a page was refused by the
    pre-check, or a render child died without a result).

    The PDF is untrusted (client-uploaded), so the limits are enforced:
    - PAGE cap: only the first ``min(max_pages, page_count)`` pages are
      rendered (a 9,999-page document cannot fan out; a one-page W-2 renders
      exactly one page, never an IndexError on the missing second page).
    - CONTENT-SIZE pre-check: before a page is handed to a render process, its
      DECODED content stream (plus the decoded streams of every Form XObject
      the page references, one nesting level) is measured with a BOUNDED
      decompression (zlib max_length); a page over ``_PAGE_CONTENT_MAX_BYTES``
      is refused and the render stops. Decoded size is the bound because a
      Flate-compressed content stream can be tiny on disk yet expand to a
      huge operator list. This is the fast, in-process bound against a
      pathological page (thousands of drawing ops / a huge XObject / a
      compression bomb): a heavy page is refused in well under a second,
      before it can burn a render process.
    - TIME cap: each page that survives the pre-check is rasterized in its
      own spawned process (``multiprocessing`` spawn context, never fork) and
      the parent does ``proc.join(budget)``. A page still rasterizing when the
      budget is up is KILLED by the OS (``proc.kill``) and counted as a
      visible ``render timeout (partial)``. A process timeout can be enforced
      by the OS; a thread join cannot interrupt a GIL-holding rasterizer, so
      this is the hard guarantee, with the pre-check in front of it.
    - CONCURRENCY cap: a MODULE-level ``threading.BoundedSemaphore`` is held
      around each page render, bounding how many page render processes are
      live at once across ALL concurrent ``_render_pdf`` calls (not per call).
      The semaphore is released in the parent's finally.

    The pre-check doc is opened in the caller's process to measure page
    content sizes; each page is then rasterized in a fresh spawned process
    that opens its own doc from the bytes (fitz docs are not picklable, and
    spawn does not inherit the caller's interpreter state).
    """
    import fitz  # pymupdf -- imported lazily so a missing dep can't break import

    per_page_budget_s = _RENDER_PAGE_BUDGET_S
    out: list[bytes] = []
    error: str | None = None
    zoom = dpi / 72.0

    # The pre-check doc is kept open for the whole loop so each page's content
    # stream can be measured (the render children open their own docs to
    # rasterize); it is closed in the finally so a refused/errored render still
    # releases it.
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_count = doc.page_count
        pages_to_render = min(max_pages, page_count)

        for page_index in range(pages_to_render):
            content_bytes = _page_content_bytes(doc, page_index)
            if content_bytes > _PAGE_CONTENT_MAX_BYTES:
                error = "page too large (partial)"
                _log.warning(
                    "tax_doc_vlm render: page %d content stream %d bytes exceeds "
                    "%d, refusing (partial after %d page(s))",
                    page_index,
                    content_bytes,
                    _PAGE_CONTENT_MAX_BYTES,
                    len(out),
                )
                break

            # Bounded concurrency across ALL concurrent _render_pdf calls: the
            # module-level semaphore is acquired in the parent around the page
            # render (start + join + kill) and released in the parent's finally.
            # If start() raises after the slot is held, the finally releases it.
            _RENDER_SLOT.acquire()
            proc: multiprocessing.context.SpawnProcess | None = None
            try:
                parent_conn, child_conn = _RENDER_MP_CTX.Pipe(duplex=False)
                proc = _RENDER_MP_CTX.Process(
                    target=_render_one_page,
                    args=(pdf_bytes, page_index, zoom, child_conn),
                )
                proc.start()
                child_conn.close()
                # Wait up to the budget for the child to exit. is_alive() is
                # the reliable liveness check (join's return value is not
                # always 0 on a clean exit, depending on the platform), so a
                # child that has finished is the one that is no longer alive.
                proc.join(per_page_budget_s)
                if not proc.is_alive():
                    # The child finished within the budget. Take its result:
                    # the child is dead, so the pipe holds at most one item.
                    # A child that died without sending (os._exit, a hard
                    # crash) leaves the pipe closed: recv() raises EOFError,
                    # which is a visible "render failed", not a timeout.
                    # (poll() alone cannot tell "sent" from "died without
                    # sending": a fast exit can leave a half-closed pipe that
                    # poll() still reports, so recv's EOF is the real signal.)
                    try:
                        png = parent_conn.recv()
                        out.append(png)
                        continue
                    except EOFError:
                        error = "render failed"
                        _log.warning(
                            "tax_doc_vlm render: page %d render child exited without "
                            "a result (partial after %d page(s))",
                            page_index,
                            len(out),
                        )
                        break
                # The child is still rasterizing after the budget: kill it by
                # the OS (a thread join cannot interrupt the GIL-holding C
                # rasterizer) and count it as a visible timeout.
                proc.kill()
                proc.join(1)
                error = "render timeout (partial)"
                _log.warning(
                    "tax_doc_vlm render: page %d overran %.0fs budget, killed "
                    "(partial after %d page(s))",
                    page_index,
                    per_page_budget_s,
                    len(out),
                )
                break
            finally:
                parent_conn.close()
                _RENDER_SLOT.release()
    finally:
        doc.close()
    return out, error


def _render_one_page(
    pdf_bytes: bytes, page_index: int, zoom: float, conn: multiprocessing.connection.PipeConnection
) -> None:
    """Rasterize one PDF page to PNG bytes in a spawned child process.

    Top-level and self-contained (spawn re-imports the module in a fresh
    interpreter): it opens its own fitz doc from the bytes, rasterizes the
    page, sends the PNG bytes through the pipe, and closes the doc. The
    parent owns the timeout and the kill (see _render_pdf); the child does
    no timeout bookkeeping of its own. If anything here fails (a MuPDF error,
    a page the doc cannot open), the child exits without sending a result,
    which the parent counts as a visible ``render failed``.
    """
    # Import the real package, not the deprecated ``fitz`` shim: the shim is a
    # thin re-export layer that does not survive a spawn re-import (the child
    # re-runs the import in a fresh interpreter and the shim's lazy bindings
    # are gone), so it raises in the child and the render would time out.
    # ``pymupdf`` is the same library (``fitz`` is its deprecated alias) and
    # imports cleanly in the child.
    import pymupdf  # the real package (fitz is its deprecated alias)

    try:
        matrix = pymupdf.Matrix(zoom, zoom)
        wdoc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        try:
            pix = wdoc[page_index].get_pixmap(matrix=matrix, alpha=False)
            conn.send(pix.tobytes("png"))
        finally:
            wdoc.close()
    except Exception:  # noqa: BLE001 - a child that fails dies with no result
        # The parent sees a non-zero exit and no pipe item: a visible
        # "render failed". Nothing to send; the process ends here.
        return
    finally:
        conn.close()


def _xobject_xrefs(doc: Any, xobj_xref: int) -> set[int]:
    """XObject xrefs an XObject object references, via its /Resources /XObject
    dict (one nesting level: the XObjects an XObject itself invokes)."""
    xrefs: set[int] = set()
    try:
        kind, value = doc.xref_get_key(xobj_xref, "Resources/XObject")
    except Exception:  # noqa: BLE001 - a corrupt xobject has no nested xrefs
        return xrefs
    if kind != "dict" or not isinstance(value, str):
        return xrefs
    for _name, xref in re.findall(r"(/\S+)\s+(\d+)\s+0\s+R", value):
        xrefs.add(int(xref))
    return xrefs


def _filter_list(doc: Any, xref: int) -> list[str]:
    """The stream's /Filter normalised to a list of filter names.

    PDFs spell /Filter three ways: a bare name (``/FlateDecode``), an array
    (``[/FlateDecode]`` or a chain ``[/A85Decode /FlateDecode]``), or an
    indirect object (``3 0 R`` to either of those). This returns the names as
    a list; an empty list means no filter. A name that cannot be resolved
    (indirect cycle, malformed) raises: the caller treats any raise as
    "refuse the page".
    """
    kind, value = doc.xref_get_key(xref, "Filter")
    if kind in ("null", "") or value in (None, "", "null"):
        return []
    if kind == "array" and isinstance(value, str):
        return [t.strip() for t in re.findall(r"/\S+", value)]
    if kind == "xref" and isinstance(value, str):
        m = re.match(r"(\d+)\s+0\s+R", value)
        if not m:
            raise ValueError("unresolvable /Filter indirect reference")
        return _filter_list(doc, int(m.group(1)))
    if kind == "name":
        return [value] if value not in (None, "", "null") else []
    raise ValueError("unrecognised /Filter encoding")


def _decoded_stream_bytes(doc: Any, xref: int, limit: int) -> int:
    """DECODED byte count of the stream at ``xref``, BOUNDED at ``limit + 1``.

    Never calls ``doc.xref_stream()`` (MuPDF's full decode) on an unbounded
    stream in the API process: that would inflate a compression bomb into the
    server. Instead it reads the RAW stream (``xref_stream_raw``) and:
      - no filter: the raw bytes are the decoded bytes (count them);
      - exactly [/FlateDecode] (bare name, array, or indirect): decompress
        with ``zlib.decompressobj`` at ``max_length = limit + 1``, stopping the
        moment the decoded output passes the bound, so a compression bomb
        (tiny raw, huge decoded) is refused without a multi-GB inflate;
      - ANY other filter or filter chain (RunLengthDecode, LZWDecode,
        [/A85Decode /FlateDecode], ...): REFUSE. Real content streams are
        Flate or unfiltered; any other filter can expand a tiny raw stream to
        a large decoded one (RunLengthDecode and LZWDecode do), and the
        bounded decoder only understands Flate, so counting such a stream at
        its raw size would understate a bomb.
    Raises on a decode error (a zlib.error on a corrupt/bomb stream, a
    truncated stream, an over-bound stream): the caller treats any raise as
    "too large" and refuses the page. A stream that cannot be decoded is
    NEVER counted as 0 -- that would let a bomb through.

    Returns the decoded byte count; 0 for a non-stream or empty xref.
    """
    if not doc.xref_is_stream(xref):
        return 0
    raw = doc.xref_stream_raw(xref)
    if not raw:
        return 0
    filters = _filter_list(doc, xref)
    if filters == ["/FlateDecode"]:
        # Bounded Flate decompression. Feed the raw input ONCE; every later
        # pass feeds only what the previous pass left unconsumed
        # (decomp.unconsumed_tail). Re-feeding the whole raw stream each pass
        # would restart the decompressor state and make a stream that yields
        # one byte per pass burn CPU linear in (raw size x passes) instead of
        # linear in raw size. If the input is exhausted before decomp.eof,
        # the stream is truncated (a corrupt or adversarial stream): refuse.
        decomp = zlib.decompressobj()
        decoded_len = 0
        data = raw
        while data:
            chunk = decomp.decompress(data, limit + 1)
            decoded_len += len(chunk)
            if decoded_len > limit:
                raise ValueError("flate stream exceeds the content bound")
            data = decomp.unconsumed_tail
        tail = decomp.flush()
        decoded_len += len(tail)
        if decoded_len > limit:
            raise ValueError("flate stream exceeds the content bound")
        if not decomp.eof:
            raise ValueError("flate stream truncated (no eof)")
        return decoded_len
    if not filters:
        # No filter: the raw bytes ARE the decoded bytes.
        if len(raw) > limit:
            raise ValueError("unfiltered stream exceeds the content bound")
        return len(raw)
    # Any other filter or filter chain: refuse. These filters can expand
    # (RunLengthDecode, LZWDecode) and the bounded decoder does not understand
    # them, so the raw size is not a safe lower bound.
    raise ValueError(
        "unsupported content stream filter " + " ".join(filters)
    )


def _page_content_bytes(doc: Any, page_index: int) -> int:
    """Total DECODED bytes of a page's content streams plus its Form
    XObjects, or 0 on a measurement error that is not an over-bound.

    Cheap by design: it reads the page's content-stream xrefs and the XObjects
    referenced by the page (one nesting level), then sums their DECODED
    lengths with a BOUNDED decompression (zlib max_length), so it never
    inflates more than ``_PAGE_CONTENT_MAX_BYTES + 1`` bytes into the API
    process and runs in microseconds for a normal page. This is the pre-check
    that refuses a pathological page before it can burn a render process.

    Decoded size is the bound (not the compressed/raw size): a
    Flate-compressed content stream is small on disk yet expands to the
    operator list the C rasterizer walks, so the compressed size understates
    the cost. The measure is bounded: a Flate stream is decompressed with
    ``max_length = bound + 1`` and a compression bomb (tiny raw, huge decoded)
    is refused the moment the decoded output passes the bound, without a
    multi-GB decode. Form XObjects are included because their operators are
    executed by the rasterizer when the page invokes them ("/X Do"), including
    the one nesting level of XObjects an XObject itself references (its
    /Resources /XObject dict). The sum stops as soon as it passes the bound:
    the caller only needs the verdict, and a page whose content is already
    over the bound must not burn time decoding every stream.

    ANY decode error (a zlib.error on a corrupt stream, a truncated Flate
    stream, an unsupported filter or chain) refuses the page: it returns a
    count OVER the bound (the caller refuses), never 0. A stream that cannot
    be decoded must not be counted as "no content" -- that would let a bomb
    through.
    """
    page = doc[page_index]
    limit = _PAGE_CONTENT_MAX_BYTES
    total = 0

    def _add(xref: int) -> bool:
        """Add xref's decoded bytes to the running total; True if over bound."""
        nonlocal total
        try:
            total += _decoded_stream_bytes(doc, xref, limit)
        except Exception:  # noqa: BLE001 - a decode error refuses the page
            # A stream that cannot be decoded (corrupt, or a compression bomb
            # the bounded decoder refuses) is treated as over the bound:
            # refuse it. Never count it as 0.
            total = limit + 1
            return True
        return total > limit

    # The page's own content streams.
    for xref in page.get_contents():
        if _add(xref):
            return total

    # The page's Form XObjects, one nesting level deep (the XObjects an
    # XObject itself invokes, via its /Resources /XObject dict).
    try:
        nested: set[int] = set()
        for xobj in page.get_xobjects():
            xobj_xref = xobj[0]
            if _add(xobj_xref):
                return total
            nested.update(_xobject_xrefs(doc, xobj_xref))
        for xref in sorted(nested):
            if _add(xref):
                return total
    except Exception:  # noqa: BLE001 - a corrupt xobject listing refuses the page
        total = limit + 1
        return total
    return total


def _parse_tax_json(raw: str) -> TaxDocExtraction | None:
    """Parse the model's JSON into a TaxDocExtraction. Tolerant of code fences."""
    text = raw.strip()
    if text.startswith("```"):
        # Strip a ```json ... ``` fence.
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    # Find the outermost JSON object.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        obj = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    doc_type = str(obj.get("doc_type") or "other").strip()
    if doc_type not in TAX_DOC_TYPES:
        # Normalize common variants (e.g. "1099R", "w2", "ssa1099").
        norm = doc_type.upper().replace(" ", "").replace("_", "").replace("-", "")
        match = next(
            (t for t in TAX_DOC_TYPES
             if t.upper().replace("-", "") == norm),
            None,
        )
        doc_type = match or "other"

    tax_year: int | None = None
    ty = obj.get("tax_year")
    if isinstance(ty, (int, float)):
        tax_year = int(ty)
    elif isinstance(ty, str) and ty.strip().isdigit():
        tax_year = int(ty.strip())
    if tax_year is not None and not (1900 <= tax_year <= 2100):
        tax_year = None

    raw_fields = obj.get("fields")
    fields: dict[str, str] = {}
    if isinstance(raw_fields, dict):
        for k, v in raw_fields.items():
            if v is None:
                continue
            fields[str(k).strip()[:200]] = str(v).strip()[:500]

    conf = obj.get("confidence")
    confidence = float(conf) if isinstance(conf, (int, float)) else None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    def _s(key: str) -> str | None:
        val = obj.get(key)
        return str(val).strip()[:300] if isinstance(val, str) and val.strip() else None

    return TaxDocExtraction(
        doc_type=doc_type,
        tax_year=tax_year,
        payer_name=_s("payer_name"),
        recipient_name=_s("recipient_name"),
        fields=fields,
        confidence=confidence,
    )
