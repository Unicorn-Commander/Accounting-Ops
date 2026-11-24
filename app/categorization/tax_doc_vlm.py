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
import re
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures import TimeoutError as ProcessTimeoutError
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

# Rasterizer concurrency bound: fitz rasterization is CPU-heavy and holds the
# GIL inside the C rasterizer, so more than a couple of concurrent page renders
# in the same process just thrash the interpreter. Page rasterization is
# therefore offloaded to a process pool (multiprocessing) so a pathological
# page cannot block the server's event loop or starve other workers.
_RENDER_CONCURRENCY = 2
_RENDER_PAGE_BUDGET_S = 10.0
# Pre-check bound: a page whose DECODED content stream (plus the decoded
# streams of every Form XObject the page references, one nesting level) is
# larger than this is refused before it is rasterized. A real W-2 / 1099 /
# 1099-R page carries a few KB of operators (measured: a text-only page is
# ~200 bytes); a page over 128 KB of decoded content is pathological
# (thousands of drawing ops / a huge XObject). Decoded size is the bound, not
# the compressed (raw) stream size: a Flate-compressed content stream can be
# tiny on disk yet expand to a huge operator list that the C rasterizer must
# walk. The refusal must happen BEFORE the page is ever handed to the C
# rasterizer, because that raster is the thing that would hold a worker and
# block the event loop while it runs.
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
        # CPU-bound: run it off the event loop so one heavy uploaded PDF
        # cannot block the server while it is being refused or rasterized.
        # Rasterization happens in a worker process (see _render_pdf) so a
        # pathological page cannot hold the GIL or starve other requests.
        # Images skip this (no fitz).
        try:
            if mime in _PDF_MIMES:
                images, render_error = await run_in_threadpool(
                    _render_pdf, body, max_pages=_MAX_PAGES, dpi=_RENDER_DPI
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
    pdf_bytes: bytes,
    *,
    max_pages: int,
    dpi: int,
    rasterize_fn: Any = None,
) -> tuple[list[bytes], str | None]:
    """Rasterize the first ``max_pages`` of a PDF to PNG bytes via pymupdf.

    Returns ``(pages, error)`` where ``error`` is set when the render was
    partial (a page overran its per-page budget).

    The PDF is untrusted (client-uploaded), so the limits are enforced:
    - PAGE cap: only the first ``max_pages`` are rasterized (a 9,999-page
      document cannot burn the whole worker).
    - CONTENT-SIZE pre-check: before a page is handed to the C rasterizer, its
      DECODED content stream (plus the decoded streams of every Form XObject
      the page references, one nesting level) is measured; a page over
      ``_PAGE_CONTENT_MAX_BYTES`` is refused and the render stops. Decoded
      size is the bound because a Flate-compressed content stream can be tiny
      on disk yet expand to a huge operator list. This is the HARD bound
      against a pathological page (thousands of drawing ops / a huge XObject):
      the per-page budget below is best-effort only, because fitz holds the
      GIL inside the C rasterizer, so a join deadline cannot interrupt a page
      that is already rasterizing. The pre-check is cheap and runs in the
      caller's context, so a heavy page is refused fast, before it can burn a
      worker or block the event loop.
    - TIME cap: each page is rasterized in a separate process (via a
      ProcessPoolExecutor) with a hard timeout; a page that overruns it is
      dropped and the render stops. Unlike a thread join (which cannot
      interrupt a GIL-holding rasterizer), a process timeout can be enforced
      by the OS: the worker process is killed if it exceeds the budget.
    - CONCURRENCY cap: the process pool is sized to _RENDER_CONCURRENCY,
      bounding how many pages rasterize at once.

    ``rasterize_fn`` is optional and intended for tests: if provided, it is
    called as ``rasterize_fn(pdf_bytes, page_index, zoom) -> bytes`` in the
    caller's process (no process pool), bypassing the process-pool path. This
    lets tests inject a fake/stalling rasterizer without pickling issues.

    The pre-check doc is opened in the caller's process to measure page
    content sizes; each page is then rasterized in a fresh worker process
    that opens its own doc from the bytes (fitz docs are not picklable).
    """
    import fitz  # pymupdf -- imported lazily so a missing dep can't break import

    per_page_budget_s = _RENDER_PAGE_BUDGET_S
    out: list[bytes] = []
    error: str | None = None
    zoom = dpi / 72.0

    # The pre-check doc is kept open for the whole loop so each page's content
    # stream can be measured (the workers open their own docs to rasterize); it
    # is closed in the finally so a refused/errored render still releases it.
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        page_count = doc.page_count
        pages_to_render = min(max_pages, page_count)

        if rasterize_fn is not None:
            # Test path: rasterize in the caller's process (no pool). Use a
            # thread-based timeout so a stalling rasterize_fn does not block
            # the caller for its full duration.
            import threading

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

                result_box: list[bytes] = []
                exc_box: list[BaseException] = []

                def _run(fn=rasterize_fn, pi=page_index):
                    try:
                        result_box.append(fn(pdf_bytes, pi, zoom))
                    except BaseException as e:  # noqa: BLE001
                        exc_box.append(e)

                t = threading.Thread(target=_run, daemon=True)
                t.start()
                t.join(per_page_budget_s)
                if t.is_alive():
                    error = "render timeout (partial)"
                    _log.warning(
                        "tax_doc_vlm render: page %d overran %.0fs budget, partial after %d page(s)",
                        page_index,
                        per_page_budget_s,
                        len(out),
                    )
                    break
                if exc_box:
                    raise exc_box[0]
                if result_box:
                    out.append(result_box[0])
        else:
            # Production path: rasterize in a process pool.
            executor = ProcessPoolExecutor(max_workers=_RENDER_CONCURRENCY)
            try:
                futures = []
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
                    futures.append(
                        (
                            page_index,
                            executor.submit(
                                _rasterize_page_in_process,
                                pdf_bytes,
                                page_index,
                                zoom,
                            ),
                        )
                    )
                for page_index, future in futures:
                    try:
                        png = future.result(timeout=per_page_budget_s)
                        out.append(png)
                    except ProcessTimeoutError:
                        error = "render timeout (partial)"
                        _log.warning(
                            "tax_doc_vlm render: page %d overran %.0fs budget, partial after %d page(s)",
                            page_index,
                            per_page_budget_s,
                            len(out),
                        )
                        for _, f in futures:
                            f.cancel()
                        break
                    except Exception as exc:  # noqa: BLE001 - worker failure
                        raise exc
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
    finally:
        doc.close()
    return out, error


def _rasterize_page_in_process(
    pdf_bytes: bytes, page_index: int, zoom: float
) -> bytes:
    """Rasterize one PDF page to PNG bytes. Runs in a worker process.

    This function must be top-level (picklable) and self-contained: it opens
    its own fitz doc from the bytes, rasterizes the page, and closes the doc.
    """
    import fitz  # pymupdf -- imported in the worker process

    matrix = fitz.Matrix(zoom, zoom)
    wdoc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        pix = wdoc[page_index].get_pixmap(matrix=matrix, alpha=False)
        return pix.tobytes("png")
    finally:
        wdoc.close()


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


def _page_content_bytes(doc: Any, page_index: int) -> int:
    """Total DECODED bytes of a page's content streams plus its Form
    XObjects, or 0 on error.

    Cheap by design: it reads the page's content-stream xrefs and the XObjects
    referenced by the page, then sums their decoded lengths without
    rasterizing anything, so it runs in the caller's context in microseconds
    for a normal page. This is the pre-check that refuses a pathological page
    before it can burn a render worker; 0 is treated as "no measurable
    content" (an empty page), never as "too large".

    Decoded size is the bound (``doc.xref_stream``, not ``xref_stream_raw``):
    a Flate-compressed stream is small on disk yet expands to the operator
    list the C rasterizer walks, so the compressed size understates the cost.
    Form XObjects are included because their operators are executed by the
    rasterizer when the page invokes them ("/X Do"), including the one
    nesting level of XObjects an XObject itself references (its /Resources
    /XObject dict). The sum stops as soon as it passes
    ``_PAGE_CONTENT_MAX_BYTES``: the caller only needs the verdict, and a page
    whose content is already over the bound must not burn time decoding every
    stream.
    """
    page = doc[page_index]
    limit = _PAGE_CONTENT_MAX_BYTES
    total = 0
    for xref in page.get_contents():
        try:
            if doc.xref_is_stream(xref):
                total += len(doc.xref_stream(xref))
                if total > limit:
                    return total
        except Exception:  # noqa: BLE001 - a corrupt stream is not "too large"
            continue
    try:
        nested: set[int] = set()
        for xobj in page.get_xobjects():
            xobj_xref = xobj[0]
            if doc.xref_is_stream(xobj_xref):
                total += len(doc.xref_stream(xobj_xref))
                if total > limit:
                    return total
            nested.update(_xobject_xrefs(doc, xobj_xref))
        for xref in nested:
            if doc.xref_is_stream(xref):
                total += len(doc.xref_stream(xref))
                if total > limit:
                    return total
    except Exception:  # noqa: BLE001 - a corrupt xobject is not "too large"
        pass
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
