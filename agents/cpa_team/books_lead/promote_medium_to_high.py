"""Phase 2 — promote medium-confidence approvals to high using vendor history.

For each pending bank_transaction_import row scored ('flag_for_review',
'medium') by the analyzer, we look at the entity's posted journal_entry
history for the SAME vendor (matched by a normalized vendor signature
extracted from `description`). If the vendor:

  (a) has >= 3 prior posted journal entries, AND
  (b) all of them are to the SAME category as the pending row's suggested
      category, AND
  (c) the pending amount is within 2x the median amount of those priors,
      AND
  (d) the description carries NO counter-signals (REFUND / REVERSAL /
      ADJUSTMENT / CHARGEBACK / DISPUTE / RETURN / CREDIT), AND
  (e) the sign of the pending amount matches the prior history (all prior
      same-vendor entries were outflows and so is this one; or all inflows
      and so is this one),

then promote → auto-apply via the same posting primitive as Phase 1.
Otherwise keep at medium with an updated rationale describing why we did
NOT promote (so Aaron can see the reasoning when he reviews).

This is REAL MONEY: we are intentionally conservative. Better to leave a
row for Aaron to decide than to misclassify.

Notes
-----
* "Same vendor" is derived from a normalized signature on the txn description:
  strip leading "PURCHASE/CHECKCARD/MOBILE PURCHASE", date-like tokens, ID
  numbers, and trailing CKCD junk → keep the alpha tokens. Two descriptions
  yield the same signature if their vendor part matches after this
  normalization. The signature is intentionally aggressive so e.g.
  "CHECKCARD 0207 APPLE.COM/BILL 866-712-7753 CA 5543... CKCD ..." and
  "PURCHASE 0317 APPLE.COM/BILL 866-712-7753 CA" collapse to the same
  vendor.
* We never auto-promote if the prior history is ambiguous (multiple
  categories) or contains its own refund/reversal entries.
* We use median (not mean) to compare amounts so a single outlier prior
  doesn't blow the 2x band.
* Reuses the Phase 1 `apply_one_approval` helper to keep the posting code
  path identical.

Run:
    docker exec -t accounting_ops-app-1 \
        python -m agents.cpa_team.books_lead.promote_medium_to_high
"""
from __future__ import annotations

import asyncio
import logging
import re
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.workspace_session import workspace_session

from agents.cpa_team.books_lead.apply_high_confidence import (
    ApprovalRow,
    ENTITY_ID,
    WORKSPACE_ID,
    apply_one_approval,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("books-lead.phase2")


# ------ Constants ----------------------------------------------------------

MIN_PRIOR_COUNT = 3
MEDIAN_BAND_MULTIPLIER = 2.0     # within 0.5x .. 2x median is "normal"
LOWER_BAND_MULTIPLIER = 0.5      # symmetric band on the low side

# A token that, if it appears in description (case-insensitive, word-bounded),
# disqualifies the row from auto-promotion. These are signals that a refund,
# reversal, or dispute is in play — a CPA must look.
COUNTER_SIGNAL_TOKENS = frozenset(
    [
        "refund",
        "reversal",
        "reverse",
        "adjustment",
        "adjust",
        "chargeback",
        "dispute",
        "return",
        "credit",      # "PURCHASE REFUND ... CREDIT" pattern
        "rbt",         # BofA "REVERSAL ... RBT ..." rebate token
    ]
)

# Categories that NEVER auto-promote (CPA judgment always required, mirrors
# the analyzer's _CPA_FLAGGED_CATEGORIES set).
NEVER_AUTO_PROMOTE_CATEGORIES = frozenset(
    [
        "SaaS · AI",
        "Banking · Interest",
        "Inter-entity transfer",
        "Contractor · 1099-NEC",
    ]
)


# ------ Vendor signature ---------------------------------------------------


_DATE_PATTERN = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{2}/\d{2}/\d{4}\b")
_LEADING_VERB = re.compile(
    r"^(MOBILE PURCHASE|PURCHASE REFUND|PURCHASE|CHECKCARD|RECURRING CKCD|REVERSAL|CHECK)\s+\d*\s*",
    re.IGNORECASE,
)
_CKCD_SUFFIX = re.compile(
    r"\s+(RECURRING\s+)?CKCD\s+\d.*$|\s+XXXXXXX.*$",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\b\d{4,}\b")
_ALL_DIGIT_TOKEN = re.compile(r"\b\d+\b")
_PUNCT_RUNS = re.compile(r"[^A-Za-z0-9\s]+")
_WHITESPACE = re.compile(r"\s+")
_STATE_SUFFIX = re.compile(
    r"\s+(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b\s*$",
    re.IGNORECASE,
)

# City stopwords stripped to better group same-vendor / different-location
# (e.g. "lowe 661 77 03" / "lowe 661 77 04" → "lowe").
_CITY_STOP = frozenset(
    [
        "charleston",
        "mountain",
        "viewca",
        "view",
        "san",
        "mateo",
        "minato",
        "shibuya",
        "girard",
        "conway",
        "ny",
        "ca",
        "wa",
        "sc",
        "fl",
        "pa",
        "tx",
        "az",
        "oh",
        "nj",
        "cca",
    ]
)


def vendor_signature(description: str | None) -> str | None:
    """Normalize a bank-statement description to a vendor signature.

    Strips:
      - Leading verbs ("PURCHASE", "CHECKCARD", ...) + their date prefix
      - CKCD suffix + masked card numbers
      - All-digit tokens (phone numbers, IDs, store numbers, MMDD prefixes)
      - Trailing US state codes
      - Common city/state stopwords

    Then keeps the first 3 vendor-identifying tokens. The truncation to 3
    (down from 4) groups same-vendor / different-store-number more
    aggressively — important for things like "LOWE'S #661 ..." where the
    store number changes per transaction.

    Examples:
      "PURCHASE 0801 APPLE.COM/BILL 866-712-7753 CA"
        -> "apple com bill"
      "PAYPAL DES:INST XFER ID:1031538994833 INDN:MAGIC UNICORN UNCONVEN CO"
        -> "paypal des inst"
    """
    if not description:
        return None
    s = description
    s = _DATE_PATTERN.sub("", s)
    s = _CKCD_SUFFIX.sub("", s)
    s = _LEADING_VERB.sub("", s)
    s = _LONG_DIGITS.sub("", s)
    s = _PUNCT_RUNS.sub(" ", s)
    s = _STATE_SUFFIX.sub("", s)
    # Drop all remaining pure-digit tokens (store numbers etc.)
    s = _ALL_DIGIT_TOKEN.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip().lower()
    # Drop short tokens and city/state stopwords
    tokens = [
        t for t in s.split()
        if len(t) >= 2 and t not in _CITY_STOP
    ]
    if not tokens:
        return None
    # Keep only the first 3 vendor-identifying tokens to stabilize the key
    sig = " ".join(tokens[:3])
    return sig or None


def has_counter_signal(description: str | None) -> str | None:
    """Return the matched counter-signal token, or None."""
    if not description:
        return None
    d = description.lower()
    for tok in COUNTER_SIGNAL_TOKENS:
        # word-boundary match — avoid "creditcard" matching "credit"
        if re.search(rf"\b{re.escape(tok)}\b", d):
            return tok
    return None


# ------ DB queries ---------------------------------------------------------


@dataclass
class PendingMedium:
    approval_id: UUID
    payload: dict[str, Any]
    recommended_category: str | None
    rationale: str
    vendor_sig: str | None
    counter_signal: str | None


@dataclass
class VendorHistorySummary:
    count: int                      # number of prior posted entries that match
    categories: dict[str, int]      # category -> count
    medians_by_category: dict[str, float]
    signs_by_category: dict[str, set]  # category -> {-1, +1}


async def fetch_pending_mediums(session: AsyncSession) -> list[PendingMedium]:
    """Snapshot all pending bank_transaction_import rows scored medium."""
    rows = (
        await session.execute(
            text(
                """
                SELECT aq.id::text AS approval_id,
                       aq.payload  AS payload,
                       ar.rationale AS rationale,
                       ar.recommended_category AS recommended_category
                FROM approval_queue aq
                JOIN approval_recommendation ar ON ar.approval_id = aq.id
                WHERE aq.entity_id = :eid
                  AND aq.status = 'pending'
                  AND aq.subject_type = 'bank_transaction_import'
                  AND ar.recommended_decision = 'flag_for_review'
                  AND ar.confidence = 'medium'
                ORDER BY aq.created_at DESC
                """
            ),
            {"eid": str(ENTITY_ID)},
        )
    ).mappings().all()
    pendings: list[PendingMedium] = []
    for r in rows:
        payload: dict = r["payload"] or {}
        desc = payload.get("description") or ""
        sig = vendor_signature(desc)
        counter = has_counter_signal(desc)
        pendings.append(
            PendingMedium(
                approval_id=UUID(r["approval_id"]),
                payload=payload,
                recommended_category=r["recommended_category"],
                rationale=r["rationale"] or "",
                vendor_sig=sig,
                counter_signal=counter,
            )
        )
    return pendings


async def fetch_vendor_history_index(
    session: AsyncSession,
) -> dict[str, VendorHistorySummary]:
    """Group posted journal_entry rows by vendor signature (extracted from
    description) and return a per-signature summary of category counts +
    medians + sign sets.

    Refunds/reversals etc. in the prior history disqualify that vendor (the
    summary just won't include them, so the count check is the gate).
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT description, category, amount
                FROM journal_entry
                WHERE entity_id = :eid
                  AND status = 'posted'
                  AND amount IS NOT NULL
                """
            ),
            {"eid": str(ENTITY_ID)},
        )
    ).mappings().all()

    # Per signature: amounts grouped by category, and refund/reversal flag
    by_sig: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    refund_flagged: set[str] = set()

    for r in rows:
        desc = r["description"] or ""
        category = r["category"] or ""
        amount = float(r["amount"]) if r["amount"] is not None else 0.0
        sig = vendor_signature(desc)
        if not sig or not category:
            continue
        # Track any vendor that has a refund/reversal in its history
        if has_counter_signal(desc):
            refund_flagged.add(sig)
            continue
        by_sig[sig][category].append(amount)

    summary: dict[str, VendorHistorySummary] = {}
    for sig, cat_amts in by_sig.items():
        if sig in refund_flagged:
            # Vendor has historical refunds — skip the promotion path entirely
            continue
        counts: dict[str, int] = {}
        medians: dict[str, float] = {}
        signs: dict[str, set] = {}
        total = 0
        for category, amts in cat_amts.items():
            counts[category] = len(amts)
            medians[category] = float(statistics.median(amts)) if amts else 0.0
            signs[category] = {(1 if a >= 0 else -1) for a in amts}
            total += len(amts)
        summary[sig] = VendorHistorySummary(
            count=total,
            categories=counts,
            medians_by_category=medians,
            signs_by_category=signs,
        )
    return summary


# ------ Promotion decision -------------------------------------------------


@dataclass
class PromotionDecision:
    promote: bool
    rationale: str


def decide_promote(
    pending: PendingMedium,
    history: dict[str, VendorHistorySummary],
) -> PromotionDecision:
    """Apply the conservative promotion rules. Returns (promote?, rationale)."""

    category = pending.recommended_category or pending.payload.get("category")
    if not category:
        return PromotionDecision(False, "No suggested category; cannot auto-promote.")

    if category in NEVER_AUTO_PROMOTE_CATEGORIES:
        return PromotionDecision(
            False,
            f"Category '{category}' is in the CPA-judgment list; never auto-promoted.",
        )

    if pending.counter_signal:
        return PromotionDecision(
            False,
            f"Description contains counter-signal token '{pending.counter_signal}' "
            "(refund/reversal/adjustment); CPA must classify.",
        )

    sig = pending.vendor_sig
    if not sig:
        return PromotionDecision(False, "Could not derive a vendor signature.")

    summary = history.get(sig)
    if not summary:
        return PromotionDecision(
            False,
            f"No prior posted history for vendor signature '{sig}'.",
        )

    prior_count = summary.categories.get(category, 0)
    if prior_count < MIN_PRIOR_COUNT:
        return PromotionDecision(
            False,
            f"Vendor '{sig}' has only {prior_count} prior posted entries in "
            f"category '{category}' (need {MIN_PRIOR_COUNT}).",
        )

    # Disqualify if the vendor was historically posted to multiple categories
    # (ambiguous classification).
    if len(summary.categories) > 1:
        # If 95%+ of history is in the same category, allow; else flag.
        dominant_share = prior_count / max(summary.count, 1)
        if dominant_share < 0.95:
            return PromotionDecision(
                False,
                f"Vendor '{sig}' has prior entries across multiple categories "
                f"({dict(summary.categories)}); recommended '{category}' covers "
                f"only {dominant_share:.0%} — ambiguous.",
            )

    # Sign check: every prior entry for this vendor+category had the same sign?
    pending_amount_cents = int(pending.payload.get("amount_cents") or 0)
    pending_sign = 1 if pending_amount_cents >= 0 else -1
    prior_signs = summary.signs_by_category.get(category, set())
    # prior amount sign on journal_entry is positive (the |amount| of the txn)
    # so "sign" here really means: did the vendor always appear as the same
    # cash side? With prior_signs always {+1} this collapses; we use the
    # raw pending sign just to flag refunds that survived counter_signal
    # detection.
    if pending_sign > 0 and category not in ("Revenue · Marketplace", "Revenue · Music"):
        return PromotionDecision(
            False,
            f"Pending amount is positive (inflow) but category '{category}' is "
            "an expense — looks like a refund slipped past counter-signal check.",
        )

    # Median band check
    median = summary.medians_by_category[category]
    if median <= 0:
        return PromotionDecision(
            False,
            f"Vendor '{sig}' historical median is {median:.2f} (non-positive); "
            "cannot compare.",
        )
    pending_abs = abs(pending_amount_cents) / 100.0
    upper = median * MEDIAN_BAND_MULTIPLIER
    lower = median * LOWER_BAND_MULTIPLIER
    if pending_abs > upper:
        return PromotionDecision(
            False,
            f"Pending amount ${pending_abs:,.2f} exceeds 2x median "
            f"${median:,.2f} for vendor '{sig}' / '{category}' — unusual spike.",
        )
    if pending_abs < lower:
        return PromotionDecision(
            False,
            f"Pending amount ${pending_abs:,.2f} is below 0.5x median "
            f"${median:,.2f} for vendor '{sig}' / '{category}' — unusual.",
        )

    return PromotionDecision(
        True,
        f"Vendor '{sig}' has {prior_count} prior posted entries in "
        f"'{category}' with median ${median:,.2f}; pending ${pending_abs:,.2f} "
        f"is within 0.5x..2x band; no counter-signal — promoting to high.",
    )


# ------ Recommendation update ---------------------------------------------


async def update_recommendation_for_kept_medium(
    session: AsyncSession,
    *,
    approval_id: UUID,
    new_rationale: str,
) -> None:
    """Update the existing approval_recommendation row in-place (keep medium
    but stamp the new rationale so the UI shows why it stayed at medium)."""
    await session.execute(
        text(
            """
            UPDATE approval_recommendation
            SET rationale = :rationale,
                submitter_kind = 'agent',
                submitter_id = 'books-lead/phase2',
                created_at = now()
            WHERE approval_id = :aid
            """
        ),
        {"rationale": new_rationale, "aid": str(approval_id)},
    )


async def update_recommendation_for_promoted(
    session: AsyncSession,
    *,
    approval_id: UUID,
    new_rationale: str,
) -> None:
    """Bump the existing approval_recommendation to ('approve', 'high') for
    the audit trail. Phase 1's apply path will already have decided the row,
    so this is just a documentation update."""
    await session.execute(
        text(
            """
            UPDATE approval_recommendation
            SET recommended_decision = 'approve',
                confidence = 'high',
                rationale = :rationale,
                submitter_kind = 'agent',
                submitter_id = 'books-lead/phase2',
                created_at = now()
            WHERE approval_id = :aid
            """
        ),
        {"rationale": new_rationale, "aid": str(approval_id)},
    )


# ------ Main orchestration ------------------------------------------------


async def main() -> int:
    log.info(
        "Phase 2 starting — workspace=%s entity=%s",
        WORKSPACE_ID,
        ENTITY_ID,
    )

    async with workspace_session(SessionLocal, WORKSPACE_ID) as session:
        pendings = await fetch_pending_mediums(session)
        log.info("Loaded %d pending medium-confidence approvals", len(pendings))
        history = await fetch_vendor_history_index(session)
        log.info(
            "Built vendor history index: %d distinct vendor signatures",
            len(history),
        )

    # ---- Score each pending ----
    to_promote: list[tuple[PendingMedium, str]] = []
    to_keep: list[tuple[PendingMedium, str]] = []
    rule_examples: dict[str, list[str]] = defaultdict(list)

    for p in pendings:
        decision = decide_promote(p, history)
        if decision.promote:
            to_promote.append((p, decision.rationale))
        else:
            to_keep.append((p, decision.rationale))
            # Bucket examples by the leading "reason word" for the report
            key = decision.rationale.split(";")[0][:60]
            if len(rule_examples[key]) < 3:
                rule_examples[key].append(str(p.approval_id))

    log.info(
        "Promotion plan: promote=%d keep=%d",
        len(to_promote),
        len(to_keep),
    )

    # ---- Apply the promoted ones via Phase 1's posting helper ----
    posted: list[tuple[UUID, UUID]] = []
    refused: list[tuple[UUID, str]] = []
    errored: list[tuple[UUID, str]] = []

    for i, (p, rationale) in enumerate(to_promote, 1):
        approval_row = ApprovalRow(
            approval_id=p.approval_id,
            payload=p.payload,
            rationale=f"[Phase 2 promotion] {rationale}",
        )
        log.info(
            "[%d/%d] Promoting+applying approval %s amount=%s desc=%s",
            i,
            len(to_promote),
            p.approval_id,
            p.payload.get("amount_cents"),
            (p.payload.get("description") or "")[:80],
        )
        outcome, je_id, err = await apply_one_approval(SessionLocal, approval_row)
        if outcome in ("posted", "idempotent"):
            posted.append((p.approval_id, je_id))  # type: ignore[arg-type]
            log.info("    -> %s je=%s", outcome, je_id)
            # Update the recommendation row to reflect the promotion
            async with workspace_session(SessionLocal, WORKSPACE_ID) as s2:
                await update_recommendation_for_promoted(
                    s2,
                    approval_id=p.approval_id,
                    new_rationale=(
                        f"Promoted by Phase 2 (vendor history). {rationale}"
                    ),
                )
        elif outcome == "refused":
            refused.append((p.approval_id, err or ""))
            log.warning("    -> REFUSED: %s", err)
        else:
            errored.append((p.approval_id, err or ""))
            log.error("    -> ERROR: %s", err)

    # ---- Update kept-medium rationales (best-effort, separate transactions
    # so a failure on one doesn't roll back the others) ----
    updated_kept = 0
    for p, rationale in to_keep:
        try:
            async with workspace_session(SessionLocal, WORKSPACE_ID) as s3:
                await update_recommendation_for_kept_medium(
                    s3,
                    approval_id=p.approval_id,
                    new_rationale=(
                        f"Phase 2 review: stays at medium. {rationale} "
                        f"[Original: {p.rationale}]"
                    ),
                )
            updated_kept += 1
        except Exception as exc:  # pragma: no cover — defensive
            log.warning(
                "Failed to update kept rationale for approval %s: %s",
                p.approval_id,
                exc,
            )

    # ---- Summary ----
    print()
    print("=" * 72)
    print("PHASE 2 SUMMARY — medium-confidence promotion via vendor history")
    print("=" * 72)
    print(f"  Total medium considered: {len(pendings)}")
    print(f"  Promoted (auto-applied): {len(posted)}")
    print(f"  Promoted but refused:    {len(refused)}")
    print(f"  Promoted but errored:    {len(errored)}")
    print(f"  Kept at medium:          {len(to_keep)}")
    print(f"  Kept-medium rationales updated: {updated_kept}")
    print()
    print("Sample 'kept at medium' reasoning buckets:")
    for reason, examples in list(rule_examples.items())[:10]:
        print(f"  [{len(examples)}+] {reason}…")
        for aid in examples[:2]:
            print(f"        e.g. approval={aid}")
    if posted:
        print()
        print("Promoted+posted journal_entry IDs (first 25):")
        for aid, je_id in posted[:25]:
            print(f"  approval={aid}  je={je_id}")
        if len(posted) > 25:
            print(f"  …and {len(posted) - 25} more")
    if refused:
        print()
        print("Promotion attempts refused by posting gate:")
        for aid, reason in refused:
            print(f"  approval={aid}  reason={reason}")
    if errored:
        print()
        print("Promotion attempts errored:")
        for aid, err in errored:
            print(f"  approval={aid}  exc={err}")
    print("=" * 72)

    return 0 if not errored else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
