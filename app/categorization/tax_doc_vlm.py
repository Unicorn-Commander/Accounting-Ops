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
import threading
import time
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

# Rasterizer concurrency bound: fitz holds the GIL inside the C rasterizer, so
# more than a couple of concurrent page renders just thrash the interpreter. A
# module-level semaphore caps how many pages rasterize at once; a render that
# cannot get a slot within _RENDER_SLOT_TIMEOUT_S fails with a clear error
# instead of queuing behind a pathological page forever.
_RENDER_CONCURRENCY = 2
_RENDER_SLOT_TIMEOUT_S = 30.0
# A single page that takes longer than this is pathological for a tax doc (a
# real W-2 / 1099 page rasterizes in well under a second at 150 DPI). Module
# constant so tests can shrink it. BEST-EFFORT ONLY: the join deadline cannot
# interrupt a page that is already rasterizing, because the fitz C rasterizer
# holds the GIL the whole time (a join deadline only fires while the GIL is
# free). It bounds how long the caller WAITS, not the page itself; the hard
# refusal for a pathological page is the content-stream pre-check below.
_RENDER_PAGE_BUDGET_S = 10.0
# Pre-check bound: a page whose (uncompressed) content stream is larger than
# this is refused before it is rasterized. A real W-2 / 1099 / 1099-R page
# carries a few KB of operators (measured: a text-only page is ~200 bytes);
# a page over 128 KB of content stream is pathological (thousands of drawing
# ops / a huge inline XObject). The refusal must happen BEFORE the page is
# ever handed to the C rasterizer, because that raster -- not the pre-check --
# is the thing that would hold a worker thread and (while it holds the GIL)
# block the server's event loop.
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
        # fitz PDF work (open + content-stream pre-check + rasterization) is
        # CPU-bound and holds the GIL inside the C rasterizer: run it off the
        # event loop so one heavy uploaded PDF cannot block the server while
        # it is being refused or rasterized. Images skip this (no fitz).
        try:
            if mime in _PDF_MIMES:
                images, render_error = await run_in_threadpool(
                    _render_pdf, body, _MAX_PAGES, _RENDER_DPI
                )
            else:
                images, render_error = _to_page_images(body, mime)
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


def _to_page_images(body: bytes, mime: str) -> tuple[list[bytes], str | None]:
    """Return (PNG bytes for the first pages, error or None).

    Images pass through; PDFs rasterize under a page + time cap. ``error`` is
    set when a page overran its budget (the render is partial) so the caller
    can mark the extraction result accordingly — a truncated tax document must
    be visible, never silently "complete".
    """
    if mime in _IMAGE_MIMES:
        return [body], None
    if mime in _PDF_MIMES:
        return _render_pdf(body, max_pages=_MAX_PAGES, dpi=_RENDER_DPI)
    return [], None


def _render_pdf(
    pdf_bytes: bytes, *, max_pages: int, dpi: int
) -> tuple[list[bytes], str | None]:
    """Rasterize the first ``max_pages`` of a PDF to PNG bytes via pymupdf.

    Returns ``(pages, error)`` where ``error`` is set when the render was
    partial (a page overran its per-page budget or could not get a render slot
    in time).

    The PDF is untrusted (client-uploaded), so the limits are enforced:
    - PAGE cap: only the first ``max_pages`` are rasterized (a 9,999-page
      document cannot burn the whole worker).
    - CONTENT-SIZE pre-check: before a page is handed to the C rasterizer, its
      (uncompressed) content stream is measured; a page over
      ``_PAGE_CONTENT_MAX_BYTES`` is refused and the render stops. This is the
      HARD bound against a pathological page (thousands of drawing ops / a
      huge inline XObject): the join budget below is best-effort only, because
      fitz holds the GIL inside the C rasterizer, so a join deadline cannot
      interrupt a page that is already rasterizing. The pre-check is cheap
      (read the page's content-stream xrefs and sum their lengths, no
      rasterization) and runs in the caller's context, so a heavy page is
      refused fast, before it can burn a worker or block the event loop.
    - TIME cap (BEST-EFFORT): each page is rasterized in a worker thread
      joined with a deadline; a page that overruns it is dropped and the
      render stops. Because fitz rasterization holds the GIL inside the C
      library, the deadline fires only around the call, not inside it: it
      bounds how long the caller waits, not the page itself.
    - CONCURRENCY cap: a module-level semaphore bounds how many pages
      rasterize at once (fitz + GIL means more than a couple just thrash). A
      render that cannot get a slot within the timeout fails with a clear
      error instead of queuing forever.
    """
    import fitz  # pymupdf — imported lazily so a missing dep can't break import

    per_page_budget_s = _RENDER_PAGE_BUDGET_S
    out: list[bytes] = []
    error: str | None = None
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    # fitz document + page objects are not thread-safe to share, and a page
    # needs its document alive to rasterize. So each worker opens its OWN
    # document from the (already-capped) bytes and owns the whole lifecycle:
    # it closes its doc and releases its render slot in ITS OWN finally,
    # whether the render succeeded, failed, or the caller abandoned the thread
    # on a timeout. That ownership split is the point of this fix: on a
    # per-page timeout the caller abandons the thread and returns, and must
    # NOT close the document or release the slot while the daemon thread is
    # still inside page.get_pixmap (use-after-close + a leaked or
    # double-released slot). Reopening the bytes per page is cheap (a 20-page
    # scan is a few KB and opens in ~ms) and is the only fitz-safe way to let
    # each worker own its handle.
    #
    # The loop bound is min(max_pages, page_count), not max_pages: a PDF with
    # fewer pages than the cap (the common case, a single-page W-2 or 1099)
    # must rasterize every page it has without doc[page_index] raising
    # IndexError for the missing pages.
    # The pre-check doc is kept open for the whole loop so each page's content
    # stream can be measured (the workers open their own docs to rasterize); it
    # is closed in the finally so a refused/errored render still releases it.
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_count = doc.page_count
        for page_index in range(min(max_pages, page_count)):
            # Cheap pre-check BEFORE the page is rasterized: measure this
            # page's (uncompressed) content stream and refuse it if it is over
            # the bound. This is the hard refusal for a pathological page --
            # the per-page join budget below is best-effort only (the GIL
            # means it cannot interrupt a page already inside the C rasterizer),
            # so a page that would burn the worker is stopped here, fast, with
            # no rasterization and no render slot consumed.
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
            # Bound concurrency: a page render that cannot get a slot within the
            # timeout fails loudly rather than queuing behind a pathological one.
            if not _render_slot.acquire(timeout=_RENDER_SLOT_TIMEOUT_S):
                error = "render timeout (partial)"
                _log.warning(
                    "tax_doc_vlm render: no slot within %.0fs, stopping after %d page(s)",
                    _RENDER_SLOT_TIMEOUT_S,
                    len(out),
                )
                break
            # Each worker keeps its PNG in its OWN local list. The caller takes
            # it only after a successful join; a page that finishes after the
            # caller timed out appends to its list, but the caller has already
            # returned and never reads it, so a late page can't mutate the
            # returned value.
            worker_out: list[bytes] = []
            exc: list[BaseException | None] = []

            # Bind the per-page loop variables as defaults so each worker
            # captures ITS OWN page_index / worker_out / exc (B023): a late
            # worker for page N must not see the values rebound for page N+1.
            def _work(
                _pi: int = page_index,
                _out: list[bytes] = worker_out,
                _exc: list[BaseException | None] = exc,
            ) -> None:
                # The worker owns its document + the slot: close the doc and
                # release the slot in its own finally -- whether the render
                # succeeded, failed, or the caller abandoned this thread on a
                # timeout.
                wdoc = None
                try:
                    wdoc = fitz.open(stream=pdf_bytes, filetype="pdf")
                    _out.append(_rasterize(wdoc[_pi], matrix))
                except BaseException as exc_:  # noqa: BLE001 - report to joiner
                    _exc.append(exc_)
                finally:
                    if wdoc is not None:
                        wdoc.close()
                    _render_slot.release()

            thread = threading.Thread(target=_work, daemon=True)
            try:
                thread.start()
            except BaseException:
                # thread.start raised (e.g. the OS refused to spawn a thread):
                # the worker never ran, so it will never release the slot in
                # its own finally. Release it here and re-raise, so the slot
                # count is unchanged and the error propagates.
                _render_slot.release()
                raise
            thread.join(per_page_budget_s)
            if thread.is_alive():
                # Still running after the budget: give up on this page and stop
                # the render. Do NOT close the document or release the slot
                # here: the worker thread still holds its doc, its page, and
                # the slot, and it closes the doc + releases the slot in its
                # own finally when it finishes. Marking the result partial
                # keeps the truncated document visible.
                error = "render timeout (partial)"
                _log.warning(
                    "tax_doc_vlm render: page overran %.0fs budget, partial after %d page(s)",
                    per_page_budget_s,
                    len(out),
                )
                break
            if exc:
                raise exc[0]
            # The worker finished: take its PNG (its own list, read only after
            # a successful join).
            out.extend(worker_out)
    finally:
        # The pre-check doc is the caller's (the workers own their docs); close
        # it here so a refused / errored / partial render still releases it.
        doc.close()
    return out, error


# Module-level semaphore bounding concurrent fitz page renders. See the note at
# _RENDER_CONCURRENCY for why (GIL + fitz C rasterizer).
_render_slot = threading.BoundedSemaphore(_RENDER_CONCURRENCY)


def _page_content_bytes(doc: Any, page_index: int) -> int:
    """Total (uncompressed) bytes of a page's content stream, or 0 on error.

    Cheap by design: it reads the page's content-stream xrefs and sums their
    lengths without rasterizing anything, so it runs in the caller's context in
    microseconds for a normal page. This is the pre-check that refuses a
    pathological page before it can burn a render worker; 0 is treated as
    "no measurable content" (an empty page), never as "too large".
    """
    page = doc[page_index]
    total = 0
    for xref in page.get_contents():
        try:
            if doc.xref_is_stream(xref):
                total += len(doc.xref_stream_raw(xref))
        except Exception:  # noqa: BLE001 - a corrupt stream is not "too large"
            continue
    return total


def _rasterize(page: Any, matrix: Any) -> bytes:
    pix = page.get_pixmap(matrix=matrix, alpha=False)
    return pix.tobytes("png")


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
