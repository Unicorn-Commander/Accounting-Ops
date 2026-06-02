"""Local-VLM receipt extractor — pulls {date, vendor, amount} from a receipt.

Mirrors ``app.categorization.llm.LLMCategorizer`` in spirit: a thin, opinionated
wrapper around an OpenAI-compatible ``/v1/chat/completions`` endpoint that
(a) takes only env-sourced config (no secret in code), (b) constrains the model
to a tiny JSON output, and (c) degrades gracefully — on timeout, malformed JSON,
or an unparseable image it returns a *low-confidence empty* result rather than
raising. The caller ALWAYS queues the proposed item for human review; this
extractor never decides anything by itself and never auto-posts.

Target model: the local ``Qwen3.6-35B-A3B-Vision`` VLM on midboy1 (port 8088),
configured via the same ``LLM_ENDPOINT_URL`` / ``LLM_MODEL`` / ``LLM_API_KEY``
settings the categorizer uses. It is LOCAL + free, so calling it on every
uploaded receipt costs nothing (the Phase-1 constraint: no frontier/cloud spend).

Vision wire format: the image is sent as an OpenAI-style content block
``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<...>"}}``
alongside a text instruction. This is the de-facto standard llama.cpp / vLLM /
Ollama all accept for VLMs.

Confidence tiers (advisory only — NEVER a posting gate):
    high   — vendor AND amount AND date all extracted, model confidence ≥ 0.6
    low    — partial extraction (some fields missing) or model confidence < 0.6
    none   — nothing usable (empty/garbage/error). Caller still queues it as
             "needs human".
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings

# MIME types we will actually send to the VLM as an image. A PDF or CSV is NOT
# an image the VLM can read here (PDF→image rasterization is Phase-2 / out of
# scope), so the extractor declines those up front and the caller queues them
# "for review" without a model call.
_VLM_IMAGE_MIME_PREFIXES = ("image/",)

_CONFIDENCE_HIGH_FLOOR = 0.6

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


__all__ = (
    "ReceiptVLMExtractor",
    "ReceiptExtraction",
    "_parse_receipt_json",
    "_normalize_amount",
    "_normalize_date",
    "_tier_for",
)
