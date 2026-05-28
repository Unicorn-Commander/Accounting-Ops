"""Categorization engine: rule layer → LLM layer.

Two-tier system:
  Tier 1: deterministic vendor rules (``vendor_rules.py``).  Fast, auditable,
          no external calls.  Covers ~70%+ of recurring transactions.
  Tier 2: LLM fallback via ``app.categorization.llm.LLMCategorizer``.  Called
          ONLY when Tier 1 returns ``Uncategorized`` AND the caller supplies an
          ``llm_fallback`` instance.  Output status is always ``"suggest"`` (not
          ``"auto"``) so the materiality gate treats LLM-sourced categories with
          appropriate caution (queues them rather than auto-posting).

Integration point
-----------------
``categorize_transaction`` is the only entry point callers should use.  Passing
``llm_fallback=None`` (the default) preserves the deterministic-only behaviour,
which is correct for tests and for the rule-engine layer in isolation.

The async variant ``categorize_transaction_async`` is provided for callers in an
async context (e.g. the bulk ingest script) so the event loop is not blocked by
the sync wrapper around an async LLM call.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Literal

from app.categorization.vendor_rules import match_vendor_rule

if TYPE_CHECKING:
    from app.categorization.llm import LLMCategorizer

CategoryStatus = Literal["auto", "suggest", "unknown"]


@dataclass(frozen=True)
class CategorySuggestion:
    """Categorical output. NEVER numeric confidence — mirrors the same
    convention used in worker_extractions and receipt_transaction_link."""

    category: str
    status: CategoryStatus  # auto: high-trust rule, suggest: LLM/weak, unknown: human-only
    source: str  # "vendor_rule:stripe" | "qwen3-instruct" | "default"
    kind: str  # "revenue" | "expense" | "transfer" | "unknown"
    reason: str  # one-line explanation
    sign_matched: bool  # True if kind matches txn sign (revenue=positive, etc.)


def _transaction_blob(payee: str | None, description: str | None, memo: str | None) -> str:
    return " ".join(filter(None, [payee, description, memo]))


def categorize_transaction(
    payee: str | None,
    description: str | None,
    memo: str | None,
    amount: float,
    overrides: list[dict] | None = None,
    llm_fallback: "LLMCategorizer | None" = None,
    allowed_categories: list[str] | None = None,
) -> CategorySuggestion:
    """Categorize a single transaction by its surface text.

    Order:
      1. User-trained category overrides → status=auto, source=override
      2. Vendor rule match → status=auto, source=vendor_rule:<pattern>
      3. LLM tier-2 fallback (if llm_fallback supplied + Uncategorized) →
         status=suggest, source=llm:<model>
      4. Fallback → status=unknown, category=Uncategorized

    ``llm_fallback``: an ``LLMCategorizer`` instance to call when Tier 1
    returns Uncategorized.  When provided this function runs the async
    ``suggest()`` call in a new event loop via ``asyncio.run()`` — suitable
    for synchronous callers (CLI scripts).  For async callers in a running
    event loop, prefer ``categorize_transaction_async``.

    ``allowed_categories``: the list of valid COA categories to pass to the
    LLM.  If None and llm_fallback is provided, the engine imports
    ``CATEGORY_TO_ACCOUNT`` keys as the allowed set.
    """
    result = _categorize_sync_tiers(
        payee=payee,
        description=description,
        memo=memo,
        amount=amount,
        overrides=overrides,
    )
    if result.category != "Uncategorized" or llm_fallback is None:
        return result

    # Tier 2: LLM fallback — synchronous wrapper.
    return asyncio.run(
        _llm_tier(
            llm_fallback=llm_fallback,
            payee=payee,
            description=description,
            memo=memo,
            amount=amount,
            allowed_categories=allowed_categories,
        )
    )


async def categorize_transaction_async(
    payee: str | None,
    description: str | None,
    memo: str | None,
    amount: float,
    overrides: list[dict] | None = None,
    llm_fallback: "LLMCategorizer | None" = None,
    allowed_categories: list[str] | None = None,
) -> CategorySuggestion:
    """Async variant of ``categorize_transaction``.

    Prefer this in async contexts (e.g. the bulk ingest script running with
    asyncio.gather) — it awaits the LLM call directly rather than spawning a
    nested event loop.
    """
    result = _categorize_sync_tiers(
        payee=payee,
        description=description,
        memo=memo,
        amount=amount,
        overrides=overrides,
    )
    if result.category != "Uncategorized" or llm_fallback is None:
        return result

    return await _llm_tier(
        llm_fallback=llm_fallback,
        payee=payee,
        description=description,
        memo=memo,
        amount=amount,
        allowed_categories=allowed_categories,
    )


def _categorize_sync_tiers(
    *,
    payee: str | None,
    description: str | None,
    memo: str | None,
    amount: float,
    overrides: list[dict] | None,
) -> CategorySuggestion:
    """Tiers 0 + 1: overrides + vendor rules.  No I/O."""
    blob = _transaction_blob(payee, description, memo)
    if not blob.strip():
        return CategorySuggestion(
            category="Uncategorized",
            status="unknown",
            source="default",
            kind="unknown",
            reason="No payee or description.",
            sign_matched=False,
        )

    # Tier 0: user-trained overrides (consulted before vendor rules so
    # user corrections always win)
    blob_lower = blob.lower()
    for ov in overrides or []:
        if ov["pattern"] in blob_lower:
            # Sign-validation: assume override means "this category is right"
            # regardless of sign — the user said so.
            return CategorySuggestion(
                category=ov["category"],
                status="auto",
                source=f"override:{ov['pattern']}",
                kind="unknown",  # We don't know revenue/expense without DR/CR introspection
                reason=f"User-trained override on pattern '{ov['pattern']}'.",
                sign_matched=True,
            )

    rule = match_vendor_rule(blob, amount=amount)
    if rule:
        # Validate kind vs sign. Note: rules with direction="inflow"|
        # "outflow" are already pre-filtered by the matcher so this
        # check is effectively a no-op for them, but it stays here so a
        # bug in a future rule (e.g. wrong direction tag) still surfaces
        # as `sign_matched=False` → status="suggest" rather than auto-
        # posting silently.
        is_revenue = amount > 0
        is_expense = amount < 0
        sign_matched = (
            (rule.kind == "revenue" and is_revenue)
            or (rule.kind == "expense" and is_expense)
            or (rule.kind == "transfer")
        )
        return CategorySuggestion(
            category=rule.category,
            status="auto" if sign_matched else "suggest",
            source=f"vendor_rule:{rule.pattern}",
            kind=rule.kind,
            reason=(
                f"Matched vendor rule '{rule.pattern}'."
                if sign_matched
                else (
                    f"Matched vendor rule '{rule.pattern}' but transaction sign "
                    f"({'+' if amount > 0 else '-'}) doesn't match expected "
                    f"{rule.kind}. Flagging for human review."
                )
            ),
            sign_matched=sign_matched,
        )

    # Tier 1 exhausted; Tier 2 (LLM) will be tried by the caller if wired.
    return CategorySuggestion(
        category="Uncategorized",
        status="unknown",
        source="default",
        kind="unknown",
        reason="No vendor rule matched; routed to LLM Tier 2 (or /approvals if none).",
        sign_matched=False,
    )


async def _llm_tier(
    *,
    llm_fallback: "LLMCategorizer",
    payee: str | None,
    description: str | None,
    memo: str | None,
    amount: float,
    allowed_categories: list[str] | None,
) -> CategorySuggestion:
    """Call the LLM fallback and map its output to a CategorySuggestion."""
    from app.journal.posting import CATEGORY_TO_ACCOUNT  # local import avoids circular

    cats = allowed_categories if allowed_categories is not None else list(CATEGORY_TO_ACCOUNT.keys())

    llm_result = await llm_fallback.suggest(
        payee=payee,
        description=description,
        memo=memo,
        amount=Decimal(str(amount)),
        allowed_categories=cats,
    )

    if llm_result is None:
        # LLM refused / timed out / hallucinated — stay Uncategorized.
        return CategorySuggestion(
            category="Uncategorized",
            status="unknown",
            source="llm:no_result",
            kind="unknown",
            reason="LLM tier-2 returned no valid category; routed to /approvals.",
            sign_matched=False,
        )

    # sign_matched: the LLM doesn't know about DR/CR; we set it False so the
    # materiality gate always queues LLM suggestions (status="suggest" already
    # forces queue_for_confirm for tier-L; this is belt-and-suspenders).
    return CategorySuggestion(
        category=llm_result.category,
        status="suggest",
        source=f"llm:{llm_result.model}",
        kind="unknown",
        reason=llm_result.reasoning,
        sign_matched=False,
    )
