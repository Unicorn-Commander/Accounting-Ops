"""Categorization engine: rule layer → (future) LLM layer.

For now only the rule layer is wired. The LLM tier 2 hook is stubbed so
the API surface is stable when we wire it in. When tier 2 lands, it
calls local Qwen 3.6 with a GBNF grammar that constrains the output to
one of the canonical categories defined in vendor_rules.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.categorization.vendor_rules import match_vendor_rule

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
) -> CategorySuggestion:
    """Categorize a single transaction by its surface text.

    Order:
      1. User-trained category overrides → status=auto, source=override
      2. Vendor rule match → status=auto, source=vendor_rule
      3. (future) LLM tier 2 → status=suggest
      4. fallback → status=unknown, category=Uncategorized
    """
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

    # TODO: Tier 2 LLM call goes here. For now: unknown.
    return CategorySuggestion(
        category="Uncategorized",
        status="unknown",
        source="default",
        kind="unknown",
        reason="No vendor rule matched. Will route to Tier 2 (LLM) once wired.",
        sign_matched=False,
    )
