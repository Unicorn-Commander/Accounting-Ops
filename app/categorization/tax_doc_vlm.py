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
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

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
        try:
            images = _to_page_images(body, mime)
        except Exception as exc:  # noqa: BLE001 — render failure must not raise
            return TaxDocExtraction(
                model=model,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"render failed: {type(exc).__name__}",
            )
        if not images:
            return TaxDocExtraction(
                model=model,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error="no renderable pages",
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
        return parsed


def _to_page_images(body: bytes, mime: str) -> list[bytes]:
    """Return PNG bytes for the first pages. Images pass through; PDFs raster."""
    if mime in _IMAGE_MIMES:
        return [body]
    if mime in _PDF_MIMES:
        return _render_pdf(body, max_pages=_MAX_PAGES, dpi=_RENDER_DPI)
    return []


def _render_pdf(pdf_bytes: bytes, *, max_pages: int, dpi: int) -> list[bytes]:
    """Rasterize the first ``max_pages`` of a PDF to PNG bytes via pymupdf."""
    import fitz  # pymupdf — imported lazily so a missing dep can't break import

    out: list[bytes] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc[: max_pages]:
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            out.append(pix.tobytes("png"))
    return out


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
