"""Materiality gate: decides whether an entry can auto-post.

  L  < $75            → auto-post (if categorized + entity-mapped)
  M  $75 - $1000      → queue for human confirmation (sits in /reconciliation-queue)
  H  > $1000          → hard block; requires explicit confirm with memo

IRS §274(d) overrides: travel/meals require receipt link to post at all
regardless of amount. The categorization engine flags these categories,
and the materiality gate refuses to post them without `receipt_link_id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MaterialityTier = Literal["L", "M", "H"]
GateOutcome = Literal["auto_post", "queue_for_confirm", "block_high_value", "block_274d"]

# Categories that fall under IRS §274(d) substantiation rules.
SECTION_274D_CATEGORIES: frozenset[str] = frozenset({
    "Travel · Lodging",
    "Travel · Transport",
    "Meals",
})

# Thresholds in cents to avoid float drift.
LOW_THRESHOLD_CENTS = 7_500       # $75
HIGH_THRESHOLD_CENTS = 100_000    # $1000


def materiality_tier(amount_cents: int) -> MaterialityTier:
    abs_cents = abs(amount_cents)
    if abs_cents < LOW_THRESHOLD_CENTS:
        return "L"
    if abs_cents < HIGH_THRESHOLD_CENTS:
        return "M"
    return "H"


@dataclass(frozen=True)
class MaterialityVerdict:
    tier: MaterialityTier
    outcome: GateOutcome
    reason: str


def evaluate(
    *,
    amount_cents: int,
    category: str,
    category_status: str,  # "auto" | "suggest" | "unknown"
    entity_mapped: bool,
    sign_matched: bool,
    has_receipt_link: bool,
    user_confirmed: bool = False,
) -> MaterialityVerdict:
    """Decide what to do with a categorized transaction.

    Inputs come from app.categorization.engine.CategorySuggestion + the
    txn's mapping state + (optional) receipt match.
    """
    tier = materiality_tier(amount_cents)

    # 274(d) is non-negotiable: no receipt → block, regardless of amount.
    if category in SECTION_274D_CATEGORIES and not has_receipt_link:
        return MaterialityVerdict(
            tier=tier,
            outcome="block_274d",
            reason=(
                f"IRS §274(d) requires a receipt for '{category}'. "
                "Match the transaction to a receipt extraction before posting."
            ),
        )

    # Human attested → post at any tier. TRUST CONTRACT: callers may only set
    # user_confirmed after verifying the attestation themselves (HTTP route:
    # the authenticated human is the caller; decide path: an admin decision
    # row exists; MCP tool: a matching APPROVED approval_queue row was found).
    # Never plumb an agent-supplied argument into this flag.
    if user_confirmed:
        return MaterialityVerdict(
            tier=tier,
            outcome="auto_post",
            reason="User explicitly confirmed the posting.",
        )

    # Tier H always requires explicit confirm.
    if tier == "H":
        return MaterialityVerdict(
            tier=tier,
            outcome="block_high_value",
            reason=(
                f"Amount ≥ ${HIGH_THRESHOLD_CENTS / 100:,.2f} requires explicit "
                "human confirmation (not auto-postable)."
            ),
        )

    # Tier M: queue regardless of category certainty.
    if tier == "M":
        return MaterialityVerdict(
            tier=tier,
            outcome="queue_for_confirm",
            reason=(
                f"Amount in M tier (${LOW_THRESHOLD_CENTS / 100:.2f} - "
                f"${HIGH_THRESHOLD_CENTS / 100:,.2f}); awaits human confirm."
            ),
        )

    # Tier L: auto-post only if categorization is fully trusted.
    if (
        category_status == "auto"
        and entity_mapped
        and sign_matched
        and category != "Uncategorized"
    ):
        return MaterialityVerdict(
            tier=tier,
            outcome="auto_post",
            reason="Vendor rule matched cleanly, entity mapped, sign correct.",
        )

    # Tier L but categorization weak → still queue.
    why = []
    if category_status != "auto":
        why.append(f"category_status={category_status}")
    if not entity_mapped:
        why.append("entity_unmapped")
    if not sign_matched:
        why.append("sign_mismatch")
    if category == "Uncategorized":
        why.append("uncategorized")
    return MaterialityVerdict(
        tier=tier,
        outcome="queue_for_confirm",
        reason="Tier L but categorization not auto-trustable: " + ", ".join(why),
    )
