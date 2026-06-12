"""Model-agnostic LLM categorizer — Tier-2 fallback for the categorization engine.

This module is a **platform feature**, not a bespoke script. Any future customer
can point their own Accounting-Ops deployment at their preferred model by setting
three environment variables:

    CATEGORIZER_LLM_ENDPOINT_URL — any OpenAI-compatible /v1/chat/completions endpoint
    CATEGORIZER_LLM_MODEL        — the model identifier the endpoint expects
    CATEGORIZER_LLM_API_KEY      — blank for local/no-auth (Ollama / llama.cpp / vLLM),
                                   or the virtual/cloud key for the metered gateway

These are SEPARATE from the vision receipt-OCR knobs (LLM_ENDPOINT_URL / LLM_MODEL /
LLM_API_KEY), which stay pinned to an image-capable model. Categorization is pure
text classification, so it routes to a text model (default: local Qwen text;
suite: the metered gateway alias ``qwen3.6-35b-a3b``). See app/config.py.

The categorizer is intentionally thin and opinionated:

*   **Prompt**: few-shot, supplies the *allowed* category list so the model
    cannot invent a non-existent COA bucket. Output is constrained to JSON
    ``{"category":"...","confidence":0.0,"reasoning":"..."}``.
*   **Validation**: if the returned category is not in ``allowed_categories``,
    ``suggest()`` returns ``None`` — the transaction stays Uncategorized and
    flows to /approvals for human review. Hallucinations are handled gracefully,
    not silently accepted.
*   **Audit trail**: ``CategorySuggestion`` carries ``model``, ``endpoint``,
    ``duration_ms``, and ``raw_response`` so every AI-assisted decision is
    fully traceable.
*   **No secrets in code**: the API key is consumed from ``Settings`` (env-only).
    This module never reads env vars directly.

Concurrency note: ``LLMCategorizer`` is stateless; share one instance across
concurrent tasks. ``httpx.AsyncClient`` is created fresh per call (safe for
short-lived concurrent ingests; the caller can optionally share a client).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings, get_settings


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass
class LLMCategorySuggestion:
    """One LLM-produced categorization suggestion.

    ``category`` is guaranteed to be a member of ``allowed_categories`` — the
    caller can use it directly.  ``confidence`` is self-reported by the model
    (0.0–1.0) and is advisory only — do not use it as an auto-post gate.
    ``reasoning`` is a one-sentence explanation for the audit trail.
    ``raw_response`` is the exact completion text returned by the model (stored
    for human reviewers and CPA audits).
    """

    category: str        # canonical COA category string; always in allowed_categories
    confidence: float    # 0.0–1.0 as self-reported by the model; advisory
    reasoning: str       # one-sentence explanation for the audit trail
    raw_response: str    # exact completion text for the audit trail
    model: str           # which model produced it (traceability)
    endpoint: str        # which endpoint was called (traceability)
    duration_ms: int     # wall-clock time for the HTTP call


# ---------------------------------------------------------------------------
# Few-shot examples (synthetic — no real merchant data)
# ---------------------------------------------------------------------------

# These examples teach the model the JSON shape and the category vocabulary.
# They are intentionally generic so they generalize across customers.
_FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": (
            "Transaction: description='CHECKCARD SYNTH-CLOUD-HOST SAN JOSE CA', "
            "amount=-59.99, payee=None, memo=None\n"
            "Allowed categories: [\"Infrastructure · VPS\", \"SaaS · AI\", "
            "\"Office · Supplies\", \"Uncategorized\"]\n"
            "Reply with JSON only."
        ),
    },
    {
        "role": "assistant",
        "content": (
            '{"category":"Infrastructure · VPS","confidence":0.85,'
            '"reasoning":"Cloud hosting descriptor pattern suggests VPS expense."}'
        ),
    },
    {
        "role": "user",
        "content": (
            "Transaction: description='ACH CREDIT SYNTH MARKETPLACE PAYOUT', "
            "amount=1200.00, payee=None, memo=None\n"
            "Allowed categories: [\"Revenue · Marketplace\", \"Revenue · Music\", "
            "\"Inter-entity transfer\", \"Uncategorized\"]\n"
            "Reply with JSON only."
        ),
    },
    {
        "role": "assistant",
        "content": (
            '{"category":"Revenue · Marketplace","confidence":0.90,'
            '"reasoning":"ACH credit payout pattern matches marketplace revenue."}'
        ),
    },
    {
        "role": "user",
        "content": (
            "Transaction: description='Zelle payment from Jordan Lee', "
            "amount=500.00, payee=None, memo=None\n"
            "Allowed categories: [\"Revenue · Marketplace\", "
            "\"Owner's Contribution\", \"Owner's Draw\", \"Uncategorized\"]\n"
            "Reply with JSON only."
        ),
    },
    {
        "role": "assistant",
        "content": (
            '{"category":"Owner\'s Contribution","confidence":0.80,'
            '"reasoning":"Incoming Zelle in an individual\'s name is the owner '
            'funding the business, not third-party revenue."}'
        ),
    },
]

_SYSTEM_PROMPT = (
    "You are a financial categorization assistant for a small business accounting system. "
    "You will receive a bank transaction and a list of allowed Chart-of-Accounts categories. "
    "You MUST reply with ONLY a JSON object with exactly three keys: "
    '"category" (one of the allowed categories, copied EXACTLY as shown — including any '
    "middle-dot `·` separator characters, spacing, and capitalisation), "
    '"confidence" (float 0.0–1.0, your self-assessed confidence), '
    '"reasoning" (one sentence, plain English). '
    "The allowed categories use a middle-dot character `·` (U+00B7) as a separator — "
    "for example `\"Office · Supplies\"` NOT `\"Office Supplies\"`. "
    "Copy the category string character-for-character from the provided list. "
    "If no allowed category is a clear, confident fit, choose \"Uncategorized\" "
    "(when it is in the allowed list) with low confidence. Do NOT force a poor "
    "match into an unrelated bucket — abstaining is better than guessing wrong. "
    "Money the business owner moves to or from the business in their own name "
    "(e.g. Zelle / Venmo / Cash App to or from the owner) is an OWNER transfer, "
    "not income or an expense: use \"Owner's Contribution\" for money received "
    "from the owner and \"Owner's Draw\" for money paid to the owner or spent "
    "personally — but only when those categories appear in the allowed list. "
    "Do not include any text outside the JSON object."
)


# ---------------------------------------------------------------------------
# Categorizer
# ---------------------------------------------------------------------------


class LLMCategorizer:
    """Thin, model-agnostic LLM wrapper for Tier-2 transaction categorization.

    Instantiate once per ingest run (or share as a singleton). Stateless:
    all configuration comes from ``settings`` at construction time.

    Usage::

        categorizer = LLMCategorizer()
        suggestion = await categorizer.suggest(
            payee="UNKNOWN VENDOR",
            description="CHECKCARD 0401 UNKNOWN VENDOR CHARLESTON SC",
            memo=None,
            amount=Decimal("-42.99"),
            allowed_categories=list(CATEGORY_TO_ACCOUNT.keys()),
        )
        if suggestion is not None:
            # Use suggestion.category
            ...
        else:
            # Hallucination or error — stays Uncategorized, goes to /approvals.
            ...
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def suggest(
        self,
        *,
        payee: str | None,
        description: str | None,
        memo: str | None,
        amount: Decimal,
        allowed_categories: list[str],
    ) -> LLMCategorySuggestion | None:
        """Propose a COA category for a transaction.

        Returns ``None`` if:
        - ``allowed_categories`` is empty (nothing to choose from)
        - the model times out
        - the model returns malformed JSON
        - the returned category is not in ``allowed_categories`` (hallucination)

        In all ``None`` cases the transaction stays Uncategorized and flows to
        /approvals for human review. The caller should log the reason.
        """
        if not allowed_categories:
            return None

        settings = self._settings
        user_content = _build_user_message(
            payee=payee,
            description=description,
            memo=memo,
            amount=amount,
            allowed_categories=allowed_categories,
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            *_FEW_SHOT_EXAMPLES,
            {"role": "user", "content": user_content},
        ]

        # --- Primary model (routine tier — Qwen by default) ---
        primary = await self._run_model(
            model=settings.categorizer_model,
            endpoint=settings.categorizer_endpoint_url,
            api_key=settings.categorizer_api_key,
            timeout=settings.categorizer_timeout_s,
            messages=messages,
            allowed_categories=allowed_categories,
        )

        # --- Frontier fallback (per-agent routing policy) ---
        # Escalate to the configured frontier model when the primary returned
        # nothing valid, OR returned a confidence below the threshold. The
        # escalated answer is preferred; if it too fails we keep the primary.
        # The returned suggestion's ``model`` field records which model actually
        # produced it, so the audit trail shows when/where we escalated.
        fb_model = settings.categorizer_fallback_model
        if fb_model:
            needs_escalation = primary is None or (
                primary.confidence < settings.categorizer_escalate_below_confidence
            )
            if needs_escalation:
                fallback = await self._run_model(
                    model=fb_model,
                    endpoint=(
                        settings.categorizer_fallback_endpoint_url
                        or settings.categorizer_endpoint_url
                    ),
                    api_key=settings.categorizer_fallback_api_key,
                    timeout=settings.categorizer_timeout_s,
                    messages=messages,
                    allowed_categories=allowed_categories,
                )
                if fallback is not None:
                    return fallback

        return primary

    async def _run_model(
        self,
        *,
        model: str,
        endpoint: str,
        api_key: str,
        timeout: int,
        messages: list[dict[str, str]],
        allowed_categories: list[str],
    ) -> LLMCategorySuggestion | None:
        """Run ONE categorization call against a specific model/endpoint.

        Returns a validated suggestion, or None on timeout / HTTP error /
        malformed JSON / hallucinated category. Shared by both the primary
        (Qwen) and frontier-fallback paths so the prompt + validation are
        identical across models.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": self._settings.llm_max_tokens,
            "temperature": 0.0,   # deterministic; consistent categorization
        }
        # Only Qwen3 / llama.cpp needs thinking suppressed (otherwise it burns the
        # whole budget on reasoning_content → empty content). Frontier models
        # (Anthropic/OpenAI via the gateway) don't take this param — and some
        # gateways reject unknown params — so send it ONLY to Qwen models.
        if "qwen" in model.lower():
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        url = endpoint.rstrip("/") + "/chat/completions"

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
                response.raise_for_status()
        except httpx.TimeoutException:
            return None
        except httpx.HTTPError:
            return None
        duration_ms = int((time.monotonic() - t0) * 1000)

        raw_json = _extract_completion(response.json())
        if raw_json is None:
            return None

        parsed = _parse_and_validate(raw_json, allowed_categories)
        if parsed is None:
            return None

        category, confidence, reasoning = parsed
        return LLMCategorySuggestion(
            category=category,
            confidence=max(0.0, min(1.0, float(confidence))),
            reasoning=reasoning[:500],   # guard against runaway reasoning strings
            raw_response=raw_json,
            model=model,
            endpoint=endpoint,
            duration_ms=duration_ms,
        )

    async def health(self) -> dict[str, Any]:
        """Probe the configured endpoint.

        Returns a dict with keys: endpoint, model, reachable (bool),
        and optionally error (str).
        """
        settings = self._settings
        url = settings.categorizer_endpoint_url.rstrip("/") + "/models"
        headers: dict[str, str] = {}
        if settings.categorizer_api_key:
            headers["Authorization"] = f"Bearer {settings.categorizer_api_key}"
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(url, headers=headers)
                reachable = resp.status_code < 500
        except Exception as exc:
            return {
                "endpoint": settings.categorizer_endpoint_url,
                "model": settings.categorizer_model,
                "reachable": False,
                "error": str(exc),
            }
        return {
            "endpoint": settings.categorizer_endpoint_url,
            "model": settings.categorizer_model,
            "reachable": reachable,
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_user_message(
    *,
    payee: str | None,
    description: str | None,
    memo: str | None,
    amount: Decimal,
    allowed_categories: list[str],
) -> str:
    """Build the user-turn message for the completion request."""
    parts: list[str] = []
    if payee:
        parts.append(f"payee={payee!r}")
    if description:
        parts.append(f"description={description!r}")
    if memo:
        parts.append(f"memo={memo!r}")
    txn_str = "Transaction: " + ", ".join(parts) + f", amount={float(amount):.2f}"
    cats_str = "Allowed categories: " + json.dumps(allowed_categories)
    return txn_str + "\n" + cats_str + "\nReply with JSON only."


def _extract_completion(response_body: dict[str, Any]) -> str | None:
    """Pull the completion text from an OpenAI-compatible response body.

    Handles both streaming-finished and non-streaming shapes. Returns None
    if the shape is unexpected (broken endpoint / wrong API version).
    """
    try:
        choices = response_body.get("choices") or []
        if not choices:
            return None
        first = choices[0]
        # Non-streaming: choices[0].message.content
        msg = first.get("message") or {}
        content = msg.get("content")
        if content is not None:
            return str(content).strip()
        # Some endpoints put it directly in choices[0].text
        text = first.get("text")
        if text is not None:
            return str(text).strip()
        return None
    except (KeyError, IndexError, TypeError):
        return None


def _normalize_category(s: str) -> str:
    """Canonical-form normalizer for fuzzy category matching.

    Strips the middle-dot separator (U+00B7) and surrounding whitespace,
    collapses any remaining internal whitespace runs to a single space, and
    lowercases.  Used only for the *comparison key* — the canonical string
    from ``allowed_categories`` is always what gets returned to callers.

    Examples::

        "Office · Supplies"  →  "office supplies"
        "Office Supplies"    →  "office supplies"   ← Qwen drops the dot
        " SaaS · AI "        →  "saas ai"
    """
    # Remove middle-dot (·, U+00B7) and any surrounding spaces around it.
    no_dot = s.replace("·", " ")
    # Collapse multiple whitespace runs, strip leading/trailing.
    return " ".join(no_dot.lower().split())


def _parse_and_validate(
    raw: str,
    allowed_categories: list[str],
) -> tuple[str, float, str] | None:
    """Parse the raw completion string as JSON, validate it.

    Returns (category, confidence, reasoning) or None if:
    - not valid JSON
    - missing required keys
    - category not in allowed_categories (hallucination guard)

    Matching order (first hit wins):
    1. Exact string match against ``allowed_categories``.
    2. Case-insensitive exact match (catches capitalisation drift).
    3. Normalised match via ``_normalize_category`` — strips the middle-dot
       `·` separator so that Qwen returning ``"Office Supplies"`` is correctly
       mapped to the canonical ``"Office · Supplies"``.
    In cases 2 and 3 the *canonical* form from ``allowed_categories`` is
    returned so downstream code always sees the exact COA-compatible string.
    """
    # Strip markdown code fences if the model wraps its JSON (common with
    # instruction-tuned models that were trained to fence code blocks).
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # drop first line (```json or ```) and last line (```)
        cleaned = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, dict):
        return None

    category = data.get("category")
    confidence = data.get("confidence")
    reasoning = data.get("reasoning")

    if not isinstance(category, str) or not category:
        return None
    if not isinstance(reasoning, str):
        reasoning = ""
    try:
        conf_float = float(confidence) if confidence is not None else 0.5
    except (TypeError, ValueError):
        conf_float = 0.5

    # --- Hallucination guard: the category MUST be one we know about. ---
    # Build lookup tables once, cheaply (N is tiny — ~20 categories at most).
    normalized = category.strip()

    # 1. Exact match (fast path, no allocation).
    if normalized in allowed_categories:
        return normalized, conf_float, reasoning

    # 2. Case-insensitive exact match.
    allowed_lower: dict[str, str] = {c.lower(): c for c in allowed_categories}
    canonical = allowed_lower.get(normalized.lower())
    if canonical:
        return canonical, conf_float, reasoning

    # 3. Normalised (middle-dot-stripped) match.
    #    Handles Qwen returning "Office Supplies" for canonical "Office · Supplies".
    allowed_normalized: dict[str, str] = {
        _normalize_category(c): c for c in allowed_categories
    }
    canonical = allowed_normalized.get(_normalize_category(normalized))
    if canonical:
        return canonical, conf_float, reasoning

    # Category not in the allowed list → reject as hallucination.
    return None
