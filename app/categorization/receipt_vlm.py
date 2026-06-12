"""Local-VLM receipt extractor — pulls {date, vendor, amount} from a receipt.

Mirrors ``app.categorization.llm.LLMCategorizer`` in spirit: a thin, opinionated
wrapper around an OpenAI-compatible ``/v1/chat/completions`` endpoint that
(a) takes only env-sourced config (no secret in code), (b) constrains the model
to a tiny JSON output, and (c) degrades gracefully — on timeout, malformed JSON,
or an unparseable image it returns a *low-confidence empty* result rather than
raising. The caller ALWAYS queues the proposed item for human review; this
extractor never decides anything by itself and never auto-posts.

Target model: the local ``Qwen3.6-35B-A3B-Vision-p40`` VLM on midboy1 (port 8088),
configured via ``LLM_ENDPOINT_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY``.
It is LOCAL + free, so calling it on every uploaded receipt costs nothing
(the Phase-1 constraint: no frontier/cloud spend).

Vision wire format: the image is sent as an OpenAI-style content block
``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}}``
alongside a text instruction. This is the de-facto standard llama.cpp / vLLM /
Ollama all accept for VLMs.

Confidence tiers (advisory only — NEVER a posting gate):
    high   — vendor AND amount AND date all extracted, model confidence ≥ 0.6
    low    — partial extraction (some fields missing) or model confidence < 0.6
    none   — nothing usable (empty/garbage/error). Caller still queues it as
             "needs human".

Structured extraction (``extract_structured``):
    Uses a richer prompt asking the VLM to transcribe line_items, subtotal, tax,
    tip, discount, and total. A deterministic Python validator then checks the
    arithmetic in integer-cents (no float rounding). Mismatches surface as
    ``arithmetic="mismatch"`` with downgraded confidence; auto-repair fills a
    single missing component. The model is kept as a PURE TRANSCRIBER — it NEVER
    computes or infers totals.

Tier-2 bbox-oracle cross-check (implemented — see ``_call_bbox_oracle``,
``_geometric_row_amounts``, ``_bbox_cross_check``):
    A dedicated traditional-OCR service (bbox-oracle at OCR_BBOX_SERVICE_URL,
    default http://100.126.207.157:8097) acts as the geometric cross-check tier.
    It POSTs the raw image bytes (multipart) and returns per-line bounding boxes.
    ``_geometric_row_amounts`` clusters lines into y-bands, identifies the
    right-most numeric token per row as the amount, and builds a label→amount map.
    ``_bbox_cross_check`` then recovers null VLM fields from that map (provenance
    "recovered_from_bbox") and flags disagreements >1 cent as "disputed".  This
    is the correct fix for right-aligned price-column misreads by the VLM.
    The cross-check is fully graceful: if the oracle is unreachable the pipeline
    returns the VLM-only result unmodified (never raises, never blocks).
    Disable by setting OCR_BBOX_SERVICE_URL="" in the environment.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Literal

import httpx

from app.config import Settings, get_settings

_log = logging.getLogger(__name__)

# MIME types we will actually send to the VLM as an image. A PDF or CSV is NOT
# an image the VLM can read here (PDF→image rasterization is Phase-2 / out of
# scope), so the extractor declines those up front and the caller queues them
# "for review" without a model call.
_VLM_IMAGE_MIME_PREFIXES = ("image/",)

_CONFIDENCE_HIGH_FLOOR = 0.6
# Confidence penalty applied when arithmetic validation finds a mismatch.
_ARITHMETIC_MISMATCH_PENALTY = 0.3

_SYSTEM_PROMPT = (
    "You are a precise receipt-reading assistant for a bookkeeping system. "
    "You will be shown a SINGLE image of a purchase receipt or invoice. "
    "Extract exactly three fields and reply with ONLY a JSON object with these keys: "
    '"vendor" (the merchant/seller name as printed, or null if not visible), '
    '"amount" (the GRAND TOTAL paid as a plain number with no currency symbol or '
    'thousands separators, e.g. 42.99, or null if not visible), '
    '"date" (the transaction date in ISO 8601 YYYY-MM-DD form, or null if not '
    'visible or ambiguous), '
    '"currency" (ISO 4217 code like USD if you can tell, else null), '
    '"confidence" (your float 0.0-1.0 self-assessment of the overall extraction). '
    "If the image is not a receipt, is unreadable, or a field is genuinely absent, "
    "use null for that field and a low confidence. Never guess a total. "
    "Do not include any text, markdown, or explanation outside the JSON object."
)

# Structured-extraction prompt: the model is a PURE TRANSCRIBER.
# ALL arithmetic is done deterministically in Python (_validate_arithmetic).
# The instruction to "NEVER compute or infer" is intentional and critical —
# general VLMs silently hallucinate subtotals/totals that look plausible but
# are off by rounding or line-item misreads.
_STRUCTURED_SYSTEM_PROMPT = (
    "You are a receipt-transcription assistant for a bookkeeping system. "
    "You will be shown a SINGLE image of a purchase receipt or invoice. "
    "YOUR ONLY JOB IS TO COPY NUMBERS EXACTLY AS PRINTED. "
    "NEVER compute, infer, or calculate any value. If a field is not visibly printed, "
    "set it to null. Do not guess totals from line items or vice versa. "
    "Reply with ONLY a JSON object with these keys: "
    '"vendor" (merchant name as printed, or null), '
    '"date" (transaction date as printed, or null), '
    '"currency" (ISO 4217 code like USD if visible, else null), '
    '"line_items" (array of {"description": string, "amount": number|null} for each '
    "line item row printed, or empty array [] if none visible — copy amounts EXACTLY "
    "as printed, do not total them), "
    '"subtotal" (the subtotal line as printed, or null), '
    '"tax" (tax amount as printed, or null), '
    '"tip" (tip/gratuity as printed, or null), '
    '"discount" (discount/coupon amount as printed as a POSITIVE number, or null), '
    '"total" (the grand total / amount due as printed, or null), '
    '"confidence" (your float 0.0-1.0 for how clearly visible the document is). '
    "All amount fields: plain numbers only, no currency symbols or commas (e.g. 42.99). "
    "If a number is ambiguous or partially obscured, use null — do not guess. "
    "Do not include any text, markdown, or explanation outside the JSON object."
)


@dataclass
class LineItem:
    """A single line-item row from a receipt."""

    description: str
    amount: str | None  # decimal string or None; None = not visible/parseable


@dataclass
class StructuredReceiptExtraction:
    """Result of ``extract_structured()``.

    Extends the simple ReceiptExtraction with per-component fields and an
    arithmetic validation outcome.  Like ReceiptExtraction, this is ALWAYS
    advisory — the caller ALWAYS queues for human review; this object never
    decides whether to post.

    ``arithmetic``:
        "ok"       — components reconcile to ±1 cent.
        "repaired" — one component was missing/null; solved deterministically
                     from the others (provenance set to "repaired" for that field).
        "mismatch" — components are all present but do NOT reconcile, OR more
                     than one was missing.  Confidence is downgraded.
                     Do NOT guess; surface for human.
        "partial"  — not enough fields were present to attempt validation
                     (e.g. no line items AND no subtotal AND no total).
        "skipped"  — validation was not attempted (parse/transport failure).

    ``provenance``:
        Per-money-field provenance: "extracted" | "repaired" | "disputed".
        Keys match the field names: subtotal, tax, tip, discount, total.
    """

    vendor: str | None
    amount: str | None  # = validated total; the grand total callers rely on
    date: str | None
    currency: str | None
    confidence: float
    tier: str  # "high" | "low" | "none"
    model: str
    endpoint: str
    duration_ms: int
    raw_response: str
    error: str | None = None

    # Structured fields (None = not present in the VLM output)
    line_items: list[LineItem] = field(default_factory=list)
    subtotal: str | None = None
    tax: str | None = None
    tip: str | None = None
    discount: str | None = None
    total: str | None = None  # raw extracted total (before repair)

    arithmetic: Literal["ok", "repaired", "mismatch", "partial", "skipped"] = "skipped"
    provenance: dict[str, str] = field(default_factory=dict)
    # Human-readable notes from the validator (e.g. "repaired tax = 1.50")
    validation_notes: list[str] = field(default_factory=list)

    @property
    def has_any_field(self) -> bool:
        return bool(self.vendor or self.amount or self.date)


@dataclass
class ReceiptExtraction:
    """Structured result of a single receipt extraction.

    ``tier`` is one of "high" | "low" | "none". The caller uses it for UI
    framing + reasoning copy only — it is NEVER a gate to auto-post. Every
    extraction (even ``none``) is queued for a human.
    """

    vendor: str | None
    amount: str | None  # decimal string (e.g. "42.99") or None — never float
    date: str | None  # ISO YYYY-MM-DD or None
    currency: str | None  # ISO 4217 or None
    confidence: float  # 0.0-1.0
    tier: str  # "high" | "low" | "none"
    model: str
    endpoint: str
    duration_ms: int
    raw_response: str  # exact completion text (for the audit trail), truncated
    error: str | None = None  # populated on timeout/transport/parse failure

    @property
    def has_any_field(self) -> bool:
        return bool(self.vendor or self.amount or self.date)


def _empty(
    *, settings: Settings, duration_ms: int, raw: str = "", error: str | None = None
) -> ReceiptExtraction:
    """Build the safe "nothing usable" result. Always tier='none'."""
    return ReceiptExtraction(
        vendor=None,
        amount=None,
        date=None,
        currency=None,
        confidence=0.0,
        tier="none",
        model=settings.llm_model,
        endpoint=settings.llm_endpoint_url,
        duration_ms=duration_ms,
        raw_response=raw[:2000],
        error=error,
    )


class ReceiptVLMExtractor:
    """Stateless local-VLM receipt extractor. Construct once, share freely."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    def can_extract(self, mime_type: str) -> bool:
        """True only for raster image mime types the VLM can read directly.

        PDFs and CSVs return False — the caller stores + queues them for human
        review without a model call (PDF rasterization is Phase 2).
        """
        m = (mime_type or "").lower()
        return any(m.startswith(p) for p in _VLM_IMAGE_MIME_PREFIXES)

    async def extract(
        self, *, image_bytes: bytes, mime_type: str
    ) -> ReceiptExtraction:
        """Extract {vendor, amount, date} from a receipt image.

        Returns a ReceiptExtraction in ALL cases — including timeouts, transport
        errors, and unparseable output (tier='none', error set). Never raises for
        an extraction failure; only programmer errors (e.g. None bytes) would.
        The caller treats the result as advisory and always queues for review.
        """
        settings = self._settings
        t0 = time.monotonic()

        if not image_bytes:
            return _empty(settings=settings, duration_ms=0, error="empty image")
        if not self.can_extract(mime_type):
            # Defensive: callers should gate on can_extract(), but never send a
            # non-image to the VLM here.
            return _empty(
                settings=settings,
                duration_ms=0,
                error=f"unsupported mime for VLM: {mime_type}",
            )

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:{mime_type};base64,{b64}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Extract vendor, grand-total amount, date, and currency "
                            "from this receipt. Reply with JSON only."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ]

        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "max_tokens": settings.llm_max_tokens,
            "temperature": 0.0,
            # Suppress Qwen3 extended-thinking (same rationale as the categorizer:
            # without this the model can burn the whole budget on reasoning and
            # return empty content). Silently ignored by endpoints that lack it.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        url = settings.llm_endpoint_url.rstrip("/") + "/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
        except httpx.TimeoutException:
            return _empty(
                settings=settings,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error="vlm timeout",
            )
        except httpx.HTTPError as exc:
            return _empty(
                settings=settings,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"vlm transport: {type(exc).__name__}",
            )

        duration_ms = int((time.monotonic() - t0) * 1000)
        raw = _extract_completion(resp.json())
        if not raw:
            return _empty(
                settings=settings,
                duration_ms=duration_ms,
                error="empty completion",
            )

        parsed = _parse_receipt_json(raw)
        if parsed is None:
            return _empty(
                settings=settings,
                duration_ms=duration_ms,
                raw=raw,
                error="unparseable json",
            )

        vendor, amount, date, currency, confidence = parsed
        tier = _tier_for(
            vendor=vendor, amount=amount, date=date, confidence=confidence
        )
        return ReceiptExtraction(
            vendor=vendor,
            amount=amount,
            date=date,
            currency=currency,
            confidence=confidence,
            tier=tier,
            model=settings.llm_model,
            endpoint=settings.llm_endpoint_url,
            duration_ms=duration_ms,
            raw_response=raw[:2000],
            error=None,
        )

    async def extract_structured(
        self, *, image_bytes: bytes, mime_type: str
    ) -> StructuredReceiptExtraction:
        """Structured receipt extraction with deterministic arithmetic validation.

        The VLM is used as a PURE TRANSCRIBER: it copies numbers from the image.
        All arithmetic checks are done in deterministic Python using integer-cents
        math (no floats for money).  See module docstring for the validation logic.

        Returns a StructuredReceiptExtraction in ALL cases.  Never raises on
        extraction failure. The caller ALWAYS queues for human review.
        """
        settings = self._settings
        t0 = time.monotonic()

        def _empty_structured(
            error: str, duration_ms: int = 0, raw: str = ""
        ) -> StructuredReceiptExtraction:
            return StructuredReceiptExtraction(
                vendor=None,
                amount=None,
                date=None,
                currency=None,
                confidence=0.0,
                tier="none",
                model=settings.llm_model,
                endpoint=settings.llm_endpoint_url,
                duration_ms=duration_ms,
                raw_response=raw[:2000],
                error=error,
                arithmetic="skipped",
            )

        if not image_bytes:
            return _empty_structured("empty image")
        if not self.can_extract(mime_type):
            return _empty_structured(f"unsupported mime for VLM: {mime_type}")

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:{mime_type};base64,{b64}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _STRUCTURED_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Transcribe all receipt fields as JSON. "
                            "Copy numbers EXACTLY as printed; do NOT compute anything."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            },
        ]
        payload: dict[str, Any] = {
            "model": settings.llm_model,
            "messages": messages,
            "max_tokens": settings.llm_max_tokens,
            "temperature": 0.0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        url = settings.llm_endpoint_url.rstrip("/") + "/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
        except httpx.TimeoutException:
            return _empty_structured(
                "vlm timeout", duration_ms=int((time.monotonic() - t0) * 1000)
            )
        except httpx.HTTPError as exc:
            return _empty_structured(
                f"vlm transport: {type(exc).__name__}",
                duration_ms=int((time.monotonic() - t0) * 1000),
            )

        duration_ms = int((time.monotonic() - t0) * 1000)
        raw = _extract_completion(resp.json())
        if not raw:
            return _empty_structured("empty completion", duration_ms=duration_ms)

        parsed = _parse_structured_receipt_json(raw)
        if parsed is None:
            return _empty_structured(
                "unparseable json", duration_ms=duration_ms, raw=raw
            )

        vendor, date, currency, line_items, subtotal, tax, tip, discount, total, confidence = parsed

        # Run deterministic arithmetic validation (pure Python, integer-cents).
        validated = _validate_arithmetic(
            line_items=line_items,
            subtotal=subtotal,
            tax=tax,
            tip=tip,
            discount=discount,
            total=total,
            confidence=confidence,
        )

        # --- Tier-2: bbox-oracle cross-check ---
        # Call the bbox-oracle in parallel with (already complete) VLM parse.
        # If the URL is empty or the service is down, oracle_lines is [] and
        # the cross-check is a no-op.  NEVER raises; NEVER blocks on failure.
        oracle_lines = await _call_bbox_oracle(
            image_bytes=image_bytes,
            mime_type=mime_type,
            service_url=settings.ocr_bbox_service_url,
        )
        bbox_amounts = _geometric_row_amounts(oracle_lines)

        if bbox_amounts:
            # Cross-check / recover / dispute against oracle amounts.
            xc = _bbox_cross_check(
                vlm_result=validated,
                bbox_amounts=bbox_amounts,
                line_items=line_items,
            )
            # Use recovered line items if any were fixed.
            line_items = xc.pop("_recovered_line_items", line_items)
            validated = xc
            # Re-run arithmetic with recovered values (oracle may have filled
            # a null that now makes the equation solvable).
            validated2 = _validate_arithmetic(
                line_items=line_items,
                subtotal=validated.get("subtotal"),
                tax=validated.get("tax"),
                tip=validated.get("tip"),
                discount=validated.get("discount"),
                total=validated.get("total"),
                confidence=validated.get("confidence", confidence),
            )
            # Merge: keep cross-check provenance annotations over the fresh
            # arithmetic pass's provenance (the dispute/recover marks are
            # authoritative; the arithmetic pass only sees "extracted"/"missing").
            merged_prov = {**validated2["provenance"], **validated["provenance"]}
            # But if arithmetic now says "ok" or "repaired", reset any field
            # that was marked "extracted" in the xc pass back to the arithmetic
            # pass's verdict (e.g. "repaired") without clobbering dispute marks.
            for fname in ("subtotal", "tax", "tip", "discount", "total"):
                if validated["provenance"].get(fname) in ("extracted", "missing"):
                    merged_prov[fname] = validated2["provenance"].get(fname, "missing")
            validated2["provenance"] = merged_prov
            # Preserve cross-check notes (prepend so they're visible).
            xc_notes = [n for n in validated.get("notes", []) if "bbox-oracle" in n or "disputed" in n]
            validated2["notes"] = xc_notes + [n for n in validated2["notes"] if n not in xc_notes]
            # Honour confidence downgrade from dispute (arithmetic pass resets to input conf).
            if validated.get("confidence", confidence) < validated2.get("confidence", confidence):
                validated2["confidence"] = validated["confidence"]
            validated = validated2

        # The canonical amount callers rely on is the validated total.
        final_amount = validated["total"]
        final_confidence = validated["confidence"]
        tier = _tier_for(
            vendor=vendor, amount=final_amount, date=date, confidence=final_confidence
        )

        return StructuredReceiptExtraction(
            vendor=vendor,
            amount=final_amount,
            date=date,
            currency=currency,
            confidence=final_confidence,
            tier=tier,
            model=settings.llm_model,
            endpoint=settings.llm_endpoint_url,
            duration_ms=duration_ms,
            raw_response=raw[:2000],
            error=None,
            line_items=line_items,
            subtotal=validated["subtotal"],
            tax=validated["tax"],
            tip=validated["tip"],
            discount=validated["discount"],
            total=validated["total"],
            arithmetic=validated["arithmetic"],
            provenance=validated["provenance"],
            validation_notes=validated["notes"],
        )


# --------------------------------------------------------------------------- #
# Helpers (module-level, pure, unit-testable)
# --------------------------------------------------------------------------- #


def _extract_completion(response_body: dict[str, Any]) -> str | None:
    """Pull completion text from an OpenAI-compatible body (same as llm.py)."""
    try:
        choices = response_body.get("choices") or []
        if not choices:
            return None
        first = choices[0]
        msg = first.get("message") or {}
        content = msg.get("content")
        if content is not None:
            # Some VLM servers return content as a list of parts.
            if isinstance(content, list):
                texts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") in (None, "text")
                ]
                return "".join(texts).strip() or None
            return str(content).strip()
        text = first.get("text")
        if text is not None:
            return str(text).strip()
        return None
    except (KeyError, IndexError, TypeError, AttributeError):
        return None


def _normalize_amount(value: Any) -> str | None:
    """Coerce a model-supplied amount to a clean decimal string, or None.

    Accepts an int/float/str. Strips currency symbols, thousands separators,
    and whitespace. Rejects anything that isn't a finite non-negative number
    (a receipt total is never negative; a sign would be a parse artifact).
    Returns a canonical string like "42.99" so downstream Decimal() never sees
    a float-rounding artifact.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    try:
        if isinstance(value, (int, float)):
            num = float(value)
        else:
            s = str(value).strip()
            if not s:
                return None
            # Drop common currency symbols + grouping separators.
            for ch in ("$", "€", "£", "¥", ","):
                s = s.replace(ch, "")
            s = s.strip()
            num = float(s)
    except (TypeError, ValueError):
        return None
    if num != num or num in (float("inf"), float("-inf")):  # NaN/inf
        return None
    if num < 0:
        return None
    # Two-decimal canonical form (currency); keeps it as a string.
    return f"{num:.2f}"


def _normalize_date(value: Any) -> str | None:
    """Validate an ISO YYYY-MM-DD date string from the model, else None."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    # Strict ISO date — reject datetimes, slashes, or partial dates so a
    # malformed value never lands in the proposed transaction.
    import datetime as _dt

    try:
        _dt.date.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return s


def _normalize_currency(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    s = value.strip().upper()
    return s if len(s) == 3 and s.isalpha() else None


def _parse_receipt_json(
    raw: str,
) -> tuple[str | None, str | None, str | None, str | None, float] | None:
    """Parse + sanitize the model's JSON. Returns a 5-tuple or None.

    Handles markdown-fenced JSON (some instruction-tuned VLMs wrap output). All
    fields are individually sanitized; a bad field becomes None rather than
    failing the whole parse — partial extractions are useful (they still queue).
    Returns None only when the payload isn't a JSON object at all.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(
            lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        )
    # If the model emitted prose around the JSON, try to isolate the object.
    if not cleaned.lstrip().startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    vendor_raw = data.get("vendor")
    vendor = (
        str(vendor_raw).strip()[:256]
        if isinstance(vendor_raw, str) and vendor_raw.strip()
        else None
    )
    amount = _normalize_amount(data.get("amount"))
    date = _normalize_date(data.get("date"))
    currency = _normalize_currency(data.get("currency"))

    conf_raw = data.get("confidence")
    try:
        confidence = float(conf_raw) if conf_raw is not None else 0.5
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return vendor, amount, date, currency, confidence


def _tier_for(
    *, vendor: str | None, amount: str | None, date: str | None, confidence: float
) -> str:
    """Map an extraction to a confidence tier (advisory only)."""
    if not (vendor or amount or date):
        return "none"
    all_present = bool(vendor and amount and date)
    if all_present and confidence >= _CONFIDENCE_HIGH_FLOOR:
        return "high"
    return "low"


# --------------------------------------------------------------------------- #
# Integer-cents arithmetic helpers (no floats for money)
# --------------------------------------------------------------------------- #


def _to_cents(value: str | None) -> int | None:
    """Convert a decimal-string amount to integer cents, or None.

    Uses Decimal for exact conversion — never float.  ``"12.50"`` → 1250.
    Negative values and unparseable strings return None.
    """
    if value is None:
        return None
    try:
        d = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if d < 0:
        return None
    # Round to nearest cent (handles any sub-cent VLM artefact).
    cents = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return cents


def _from_cents(cents: int) -> str:
    """Convert integer cents back to a canonical decimal string. 1250 → "12.50"."""
    return f"{cents // 100}.{cents % 100:02d}"


def _validate_arithmetic(
    *,
    line_items: list[LineItem],
    subtotal: str | None,
    tax: str | None,
    tip: str | None,
    discount: str | None,
    total: str | None,
    confidence: float,
) -> dict[str, Any]:
    """Deterministic arithmetic validator using integer-cents math.

    Checks: sum(line_items) + tax + tip − discount == total
    (subtotal is treated as an alias for sum(line_items) when line_items is
    absent, per standard receipt layouts: subtotal ≡ pre-tax sum).

    Returns a dict with keys:
        subtotal, tax, tip, discount, total  — (possibly repaired) decimal strings
        arithmetic    — "ok" | "repaired" | "mismatch" | "partial"
        confidence    — (possibly downgraded)
        provenance    — dict of field → "extracted" | "repaired" | "disputed"
        notes         — list of human-readable notes
    """
    notes: list[str] = []
    prov: dict[str, str] = {}

    # Seed provenance as "extracted" for any non-null field.
    for fname, fval in [("subtotal", subtotal), ("tax", tax), ("tip", tip),
                        ("discount", discount), ("total", total)]:
        prov[fname] = "extracted" if fval is not None else "missing"

    # Convert everything to cents (None = unknown).
    items_cents_list = [_to_cents(li.amount) for li in line_items]
    # If any line item has a None amount we can't rely on the sum.
    items_parseable = all(c is not None for c in items_cents_list) and bool(items_cents_list)
    items_sum_c: int | None = sum(items_cents_list) if items_parseable else None  # type: ignore[arg-type]

    subtotal_c = _to_cents(subtotal)
    tax_c = _to_cents(tax)
    tip_c = _to_cents(tip)
    discount_c = _to_cents(discount)
    total_c = _to_cents(total)

    # Determine the effective "pre-tax base": prefer line_items sum, fall back to
    # printed subtotal (standard receipt: subtotal = sum of line items).
    base_c: int | None
    if items_sum_c is not None:
        base_c = items_sum_c
        # Cross-check line_items vs printed subtotal if both present.
        if subtotal_c is not None and abs(items_sum_c - subtotal_c) > 1:
            notes.append(
                f"line_items sum {_from_cents(items_sum_c)} != printed subtotal "
                f"{_from_cents(subtotal_c)} — using line_items sum"
            )
    else:
        base_c = subtotal_c

    # How many of the four "addend" components (base, tax, tip, discount) are known?
    addends = {
        "base": base_c,
        "tax": tax_c,
        "tip": tip_c,
        "discount": discount_c,
    }
    known_count = sum(1 for v in addends.values() if v is not None)
    total_known = total_c is not None

    # Need at least base OR total to attempt any check.
    if known_count == 0 and not total_known:
        return {
            "subtotal": subtotal, "tax": tax, "tip": tip, "discount": discount,
            "total": total, "arithmetic": "partial", "confidence": confidence,
            "provenance": prov, "notes": notes,
        }

    # Treat unknown tip/discount as 0 for reconciliation (they're commonly absent).
    eff_tip_c = tip_c if tip_c is not None else 0
    eff_discount_c = discount_c if discount_c is not None else 0
    eff_tax_c = tax_c if tax_c is not None else 0

    # --- Case A: all components and total are present → check ---
    if base_c is not None and total_known:
        computed_total_c = base_c + eff_tax_c + eff_tip_c - eff_discount_c
        delta = abs(computed_total_c - total_c)  # type: ignore[operator]

        if delta <= 1:  # ±1 cent tolerance
            notes.append("arithmetic ok")
            for fname in ("subtotal", "tax", "tip", "discount", "total"):
                if prov[fname] == "extracted":
                    prov[fname] = "extracted"
            return {
                "subtotal": subtotal, "tax": tax, "tip": tip, "discount": discount,
                "total": total, "arithmetic": "ok", "confidence": confidence,
                "provenance": prov, "notes": notes,
            }

        # Mismatch: try auto-repair if the gap can be attributed to a SINGLE null field.
        #
        # Auto-repair uses GAP SIGN to disambiguate. The equation is:
        #   base + tax + tip − discount = total
        #
        # gap = total − computed  (where computed uses null→0 substitution)
        #
        # gap < 0 (total is LESS than computed): the ONLY structural explanation is a
        #   missing discount. A missing tax or tip would push total UP, not down.
        #   → repair discount = -gap (if discount is null).
        #
        # gap > 0 (total is MORE than computed) AND tax is null:
        #   Tax is the most structurally expected missing addend. We repair it ONLY
        #   when tip is also null (confirming it's not a large ambiguous tip gap).
        #   → repair tax = gap (if tax is null, tip is null/absent).
        #
        # gap > 0 AND tax is known AND tip is null:
        #   Ambiguous — could be a missing tip OR a misread total. Do NOT repair.
        #   → mismatch.
        #
        # All other multi-null combinations → mismatch (too ambiguous).

        gap = total_c - computed_total_c  # type: ignore[operator]

        repair_field: str | None = None
        repair_cents: int | None = None

        if gap < 0 and discount_c is None:
            # total < components: structurally a missing discount.
            repair_field, repair_cents = "discount", -gap
        elif gap > 0 and tax_c is None and tip_c is None:
            # total > components: tax is null and tip is absent — repair tax.
            repair_field, repair_cents = "tax", gap
        elif gap >= 0 and tax_c is None and tip_c is not None and discount_c is not None:
            # tax is the sole unknown positive addend; tip and discount are known.
            repair_field, repair_cents = "tax", gap
        elif gap >= 0 and tax_c is not None and tip_c is not None and discount_c is None:
            # All addends known except discount (but gap is not negative — unexpected).
            # This is inconsistent: discount would need to be negative to explain a
            # positive gap; treat as mismatch.
            pass
        # gap > 0 AND tax known AND tip null → ambiguous (tip or wrong total): mismatch.
        # All other multi-null cases → mismatch.

        if repair_field is not None and repair_cents is not None and repair_cents >= 0:
            repaired_str = _from_cents(repair_cents)
            out: dict[str, Any] = {
                "subtotal": subtotal, "tax": tax, "tip": tip,
                "discount": discount, "total": total,
            }
            out[repair_field] = repaired_str
            prov[repair_field] = "repaired"
            notes.append(
                f"auto-repaired {repair_field} = {repaired_str} "
                f"(solved from other components)"
            )
            return {
                **out,
                "arithmetic": "repaired", "confidence": min(confidence + 0.05, 1.0),
                "provenance": prov, "notes": notes,
            }

        # Can't auto-repair → mismatch.
        notes.append(
            f"arithmetic mismatch: computed {_from_cents(computed_total_c)} "
            f"vs printed total {_from_cents(total_c)}"
        )
        for fname in ("subtotal", "tax", "tip", "discount", "total"):
            if prov[fname] == "extracted":
                prov[fname] = "disputed"
        downgraded = max(0.0, confidence - _ARITHMETIC_MISMATCH_PENALTY)
        return {
            "subtotal": subtotal, "tax": tax, "tip": tip, "discount": discount,
            "total": total, "arithmetic": "mismatch", "confidence": downgraded,
            "provenance": prov, "notes": notes,
        }

    # --- Case B: total missing but base + addends present → compute total ---
    if base_c is not None and not total_known:
        computed_total_c = base_c + eff_tax_c + eff_tip_c - eff_discount_c
        repaired_total = _from_cents(computed_total_c)
        prov["total"] = "repaired"
        notes.append(f"auto-repaired total = {repaired_total} (computed from components)")
        return {
            "subtotal": subtotal, "tax": tax, "tip": tip, "discount": discount,
            "total": repaired_total, "arithmetic": "repaired",
            "confidence": min(confidence + 0.05, 1.0),
            "provenance": prov, "notes": notes,
        }

    # --- Case C: base missing but total + at least some addends present ---
    if base_c is None and total_known:
        computed_base_c = total_c - eff_tax_c - eff_tip_c + eff_discount_c  # type: ignore[operator]
        if computed_base_c >= 0:
            repaired_sub = _from_cents(computed_base_c)
            prov["subtotal"] = "repaired"
            notes.append(f"auto-repaired subtotal = {repaired_sub} (solved from total)")
            return {
                "subtotal": repaired_sub, "tax": tax, "tip": tip, "discount": discount,
                "total": total, "arithmetic": "repaired",
                "confidence": min(confidence + 0.05, 1.0),
                "provenance": prov, "notes": notes,
            }

    # Partial — not enough to validate.
    notes.append("insufficient fields to validate arithmetic")
    return {
        "subtotal": subtotal, "tax": tax, "tip": tip, "discount": discount,
        "total": total, "arithmetic": "partial", "confidence": confidence,
        "provenance": prov, "notes": notes,
    }


def _parse_structured_receipt_json(
    raw: str,
) -> (
    tuple[
        str | None,  # vendor
        str | None,  # date
        str | None,  # currency
        list[LineItem],  # line_items
        str | None,  # subtotal
        str | None,  # tax
        str | None,  # tip
        str | None,  # discount
        str | None,  # total
        float,       # confidence
    ]
    | None
):
    """Parse + sanitize the structured VLM JSON. Returns a 10-tuple or None.

    Returns None only if the payload isn't a JSON object at all.
    Bad individual fields become None / empty list.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        cleaned = "\n".join(
            lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        )
    if not cleaned.lstrip().startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end != -1 and end > start:
            cleaned = cleaned[start : end + 1]

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    vendor_raw = data.get("vendor")
    vendor = (
        str(vendor_raw).strip()[:256]
        if isinstance(vendor_raw, str) and vendor_raw.strip()
        else None
    )
    date = _normalize_date(data.get("date"))
    currency = _normalize_currency(data.get("currency"))

    # Parse line_items array.
    raw_items = data.get("line_items")
    line_items: list[LineItem] = []
    if isinstance(raw_items, list):
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            desc_raw = item.get("description")
            desc = str(desc_raw).strip()[:512] if isinstance(desc_raw, str) else ""
            amt = _normalize_amount(item.get("amount"))
            line_items.append(LineItem(description=desc, amount=amt))

    subtotal = _normalize_amount(data.get("subtotal"))
    tax = _normalize_amount(data.get("tax"))
    tip = _normalize_amount(data.get("tip"))
    discount = _normalize_amount(data.get("discount"))
    total = _normalize_amount(data.get("total"))

    conf_raw = data.get("confidence")
    try:
        confidence = float(conf_raw) if conf_raw is not None else 0.5
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return vendor, date, currency, line_items, subtotal, tax, tip, discount, total, confidence


# --------------------------------------------------------------------------- #
# Tier-2 bbox-oracle: client + geometric reconcile + cross-check
# --------------------------------------------------------------------------- #

# Regex for a bare numeric token that looks like a price (digits, optional dot,
# up to 2 decimal places).  Tolerates a leading currency symbol.
_PRICE_RE = re.compile(r"^[\$£€¥]?(\d{1,6}(?:\.\d{1,2})?)$")

# Fraction of image width that defines the "right column" for amount extraction.
# Tokens whose left x-edge is beyond this fraction are considered right-side.
_RIGHT_COLUMN_X_FRACTION = 0.55

# Maximum vertical gap (in pixels) between two bbox midpoints to be considered
# the same row.  Receipts vary in font size; 20 px is conservative and covers
# even large-font line totals.
_ROW_BAND_PX = 20


async def _call_bbox_oracle(
    *,
    image_bytes: bytes,
    mime_type: str,
    service_url: str,
    timeout_s: int = 10,
) -> list[dict[str, Any]]:
    """POST image bytes to the bbox-oracle service; return parsed ``lines``.

    Returns an empty list on ANY failure (timeout, HTTP error, bad JSON,
    missing ``lines`` key, service URL empty/disabled).  NEVER raises.
    The caller treats an empty return as "oracle unavailable" and proceeds
    with the VLM-only result.

    Args:
        image_bytes: Raw image data (PNG or JPEG).
        mime_type: MIME type string, used as the multipart content-type.
        service_url: Base URL for the bbox-oracle (e.g. "http://host:8097").
            Empty string disables the call entirely.
        timeout_s: HTTP timeout in seconds.

    Returns:
        List of line dicts: ``[{"text": str, "bbox": [x1,y1,x2,y2],
        "score": float}, ...]``.  May be empty.
    """
    if not service_url or not image_bytes:
        return []
    url = service_url.rstrip("/") + "/ocr"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                url,
                files={"file": (f"receipt.{mime_type.split('/')[-1]}", image_bytes, mime_type)},
            )
            resp.raise_for_status()
        data = resp.json()
        lines = data.get("lines")
        if isinstance(lines, list):
            return lines
        return []
    except Exception as exc:  # noqa: BLE001
        _log.debug("bbox-oracle unavailable (%s: %s) — skipping cross-check", type(exc).__name__, exc)
        return []


def _geometric_row_amounts(
    lines: list[dict[str, Any]],
    *,
    row_band_px: int = _ROW_BAND_PX,
    right_column_fraction: float = _RIGHT_COLUMN_X_FRACTION,
) -> dict[str, str]:
    """Cluster bbox lines into rows; extract right-column amounts per row.

    Algorithm (deterministic geometry — no model):
    1. For each bbox line, compute the vertical midpoint (y_mid = (y1+y2)/2).
    2. Cluster lines by y_mid using a greedy band: if a line's y_mid is within
       ``row_band_px`` of the current band centre, merge into that band;
       otherwise start a new band.
    3. Within each band, find the image width by scanning all bboxes for the
       maximum x2 coordinate.  Tokens whose left edge (x1) is beyond
       ``right_column_fraction`` of that width are "right-side".
    4. Among right-side tokens, keep only those whose text matches ``_PRICE_RE``
       (looks like a bare price).  Pick the rightmost one (highest x2) as the
       row's amount.
    5. The row label is formed by joining the text of all LEFT-side tokens in
       that row (x1 < right_column_fraction × width), lower-cased and stripped.
       If a row has no label tokens, we still record the amount under the key
       ``"row_{band_index}"``.

    Returns:
        ``{label: amount_decimal_string}`` — e.g.
        ``{"total": "11.34", "tax": "0.84", "sandwich": "8.50"}``.
        Empty dict if ``lines`` is empty or no numeric right-column tokens found.
    """
    if not lines:
        return {}

    # Step 1 + 2: build bands sorted by y position.
    # Each entry: {"y_mid": float, "items": [line_dict, ...]}
    bands: list[dict[str, Any]] = []
    for line in lines:
        bbox = line.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            continue
        try:
            x1, y1, x2, y2 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
        except (TypeError, ValueError):
            continue
        y_mid = (y1 + y2) / 2.0
        # Try to merge into an existing band.
        merged = False
        for band in bands:
            if abs(y_mid - band["y_mid"]) < row_band_px:
                band["items"].append({"line": line, "x1": x1, "x2": x2})
                # Re-centre the band on the running average y_mid.
                old_n = len(band["items"]) - 1
                band["y_mid"] = (band["y_mid"] * old_n + y_mid) / len(band["items"])
                merged = True
                break
        if not merged:
            bands.append({"y_mid": y_mid, "items": [{"line": line, "x1": x1, "x2": x2}]})

    if not bands:
        return {}

    # Step 3: determine image width from the maximum x2 across ALL lines.
    all_x2 = [it["x2"] for band in bands for it in band["items"]]
    img_width = max(all_x2) if all_x2 else 1.0
    right_threshold = img_width * right_column_fraction

    # Steps 4 + 5: extract label + amount per band.
    result: dict[str, str] = {}
    for band_idx, band in enumerate(sorted(bands, key=lambda b: b["y_mid"])):
        left_texts: list[tuple[float, str]] = []   # (x1, text)
        right_prices: list[tuple[float, str]] = []  # (x2, amount_str)

        for it in band["items"]:
            text = it["line"].get("text", "").strip()
            if not text:
                continue
            if it["x1"] >= right_threshold:
                # Right-side: check if it looks like a price.
                m = _PRICE_RE.match(text)
                if m:
                    amt = _normalize_amount(m.group(1))
                    if amt is not None:
                        right_prices.append((it["x2"], amt))
            else:
                left_texts.append((it["x1"], text))

        if not right_prices:
            continue  # No numeric right-side token in this row.

        # Pick the rightmost price token.
        right_prices.sort(key=lambda t: t[0], reverse=True)
        row_amount = right_prices[0][1]

        # Build label from left-side tokens sorted by x1 (reading order).
        left_texts.sort(key=lambda t: t[0])
        label = " ".join(t[1] for t in left_texts).lower().strip()
        if not label:
            label = f"row_{band_idx}"

        result[label] = row_amount

    return result


def _bbox_cross_check(
    *,
    vlm_result: dict[str, Any],
    bbox_amounts: dict[str, str],
    line_items: list[LineItem],
) -> dict[str, Any]:
    """Cross-check VLM money fields against bbox-oracle geometric amounts.

    For each of the top-level money fields (subtotal, tax, tip, discount, total)
    and each line-item amount that is null or fails reconciliation:

    1. **Recovery:** if the VLM field is ``None`` and the bbox-oracle map contains
       a matching label (fuzzy: the field name appears anywhere in the label), set
       the field to the bbox value and mark provenance ``"recovered_from_bbox"``.
       Recovery is also attempted for line-item amounts via description matching.

    2. **Agreement boost:** if both VLM and bbox agree within ±1 cent, no change
       needed — the existing provenance stays (the arithmetic pass will mark "ok").

    3. **Dispute:** if both VLM and bbox have a value and they disagree by more
       than 1 cent, mark provenance as ``"disputed"`` and downgrade confidence by
       ``_ARITHMETIC_MISMATCH_PENALTY``.  NEVER silently pick one value.

    The function mutates a copy of ``vlm_result`` (a dict with the same keys as
    ``_validate_arithmetic`` output) and returns the updated copy.

    ``bbox_amounts`` is the output of ``_geometric_row_amounts``.  Empty dict
    means the oracle produced nothing useful — function returns ``vlm_result``
    unchanged.

    The caller should re-run ``_validate_arithmetic`` after this function to
    refresh ``arithmetic`` status with any recovered values.
    """
    if not bbox_amounts:
        return vlm_result

    out = dict(vlm_result)
    prov: dict[str, str] = dict(out.get("provenance", {}))
    notes: list[str] = list(out.get("notes", []))
    confidence: float = out.get("confidence", 0.5)
    line_items_out: list[LineItem] = list(line_items)

    def _find_bbox_value(field_name: str) -> str | None:
        """Return the bbox-oracle amount for the best label match, or None.

        Matches on word boundaries to avoid "subtotal" matching the "total"
        field (or "tip" matching "subtip", etc.).  Checks:
        1. Exact label match.
        2. Field name appears as a whole word within the label (split on spaces).
        """
        label_words_map = {label: set(label.split()) for label in bbox_amounts}
        # 1. Exact match (highest priority).
        if field_name in bbox_amounts:
            return bbox_amounts[field_name]
        # 2. Field name is a whole word within the label.
        for label, words in label_words_map.items():
            if field_name in words:
                return bbox_amounts[label]
        return None

    def _within_1cent(a: str | None, b: str | None) -> bool:
        if a is None or b is None:
            return False
        ca, cb = _to_cents(a), _to_cents(b)
        if ca is None or cb is None:
            return False
        return abs(ca - cb) <= 1

    # Top-level money fields to cross-check.
    for field_name in ("subtotal", "tax", "tip", "discount", "total"):
        vlm_val: str | None = out.get(field_name)
        bbox_val = _find_bbox_value(field_name)
        if bbox_val is None:
            continue  # oracle has nothing for this field

        if vlm_val is None:
            # Recovery: VLM missed it; oracle has it.
            out[field_name] = bbox_val
            prov[field_name] = "recovered_from_bbox"
            notes.append(f"recovered {field_name}={bbox_val} from bbox-oracle")
        elif _within_1cent(vlm_val, bbox_val):
            # Agreement — do nothing (provenance stays "extracted").
            pass
        else:
            # Disagreement > 1 cent → dispute.
            if prov.get(field_name) != "disputed":
                prov[field_name] = "disputed"
                confidence = max(0.0, confidence - _ARITHMETIC_MISMATCH_PENALTY)
                notes.append(
                    f"disputed {field_name}: VLM={vlm_val} vs bbox-oracle={bbox_val}"
                )

    # Line-item recovery: for each line item with a null amount, try to match
    # its description against bbox row labels.
    recovered_items: list[LineItem] = []
    for li in line_items_out:
        if li.amount is not None:
            recovered_items.append(li)
            continue
        desc_lower = li.description.lower()
        match_val: str | None = None
        for label, amt in bbox_amounts.items():
            # Match if any word of the description appears in the label or vice versa.
            desc_words = set(desc_lower.split())
            label_words = set(label.split())
            if desc_words & label_words:
                match_val = amt
                break
        if match_val is not None:
            recovered_items.append(LineItem(description=li.description, amount=match_val))
            notes.append(f"recovered line item '{li.description}' amount={match_val} from bbox-oracle")
        else:
            recovered_items.append(li)

    out["provenance"] = prov
    out["notes"] = notes
    out["confidence"] = confidence
    out["_recovered_line_items"] = recovered_items
    return out


__all__ = (
    "ReceiptVLMExtractor",
    "ReceiptExtraction",
    "StructuredReceiptExtraction",
    "LineItem",
    "_parse_receipt_json",
    "_parse_structured_receipt_json",
    "_normalize_amount",
    "_normalize_date",
    "_to_cents",
    "_from_cents",
    "_validate_arithmetic",
    "_tier_for",
    "_call_bbox_oracle",
    "_geometric_row_amounts",
    "_bbox_cross_check",
)
