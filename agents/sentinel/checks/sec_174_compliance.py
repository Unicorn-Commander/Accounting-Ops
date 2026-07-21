"""§174 R&E capitalize-amortize compliance check.

Since TCJA 2022, §174 requires US software/research costs to be capitalized +
amortized (5y domestic, 15y foreign) — NOT immediately deducted. For any
C-Corp or S-Corp entity with categorized R&D-flavored expenses (categories
like "SaaS · AI", "SaaS · Dev", "Research", "Software"), draft a memo
flagging that §174 amortization treatment must be applied on the 1120.

This is a perennial flag for Acme Inc: the entity capitalises ~all
its AI/research SaaS. Sentinel re-surfaces it quarterly so it doesn't get
forgotten between drafting cycles.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities

CORP_KINDS = {"c_corp", "s_corp"}

# Category keywords that suggest §174 R&E activity. (We use category names from
# CATEGORY_TO_ACCOUNT; not all are exact matches — we substring scan.)
RE_CATEGORY_KEYWORDS = (
    "SaaS · AI",
    "SaaS · Dev",
    "SaaS · Engineering",
    "Research",
    "Software · Dev",
    "Cloud · GPU",
    "Cloud · AI",
)


async def _rd_volume_last_year(session, entity_id: str, tax_year: int) -> Decimal:
    """Sum journal entries in R&D-flavored categories for the tax year."""
    # We use category_id → category_name via account_mapping if present, else
    # tag the categories straight from the reasoning_trace (we know the
    # bookkeeper writes category names into the reasoning).
    #
    # Simpler proxy here: count journal entries whose reasoning_trace contains
    # any of the keywords. This is a heuristic that surfaces a memo; the CPA
    # then does the real calc.
    keywords = "|".join(k.replace(".", r"\.") for k in RE_CATEGORY_KEYWORDS)
    row = (
        await session.execute(
            text(
                f"""
                SELECT
                  count(*)::int AS cnt,
                  coalesce(sum(usd_equivalent_at_event_time), 0) AS amount
                FROM journal_entry
                WHERE entity_id = :eid
                  AND extract(year FROM effective_date) = :ty
                  AND reasoning_trace ~* '{keywords}'
                """
            ),
            {"eid": entity_id, "ty": tax_year},
        )
    ).first()
    return Decimal(str(row.amount or 0)) if row else Decimal(0)


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0
    today = dt.date.today()

    # Quarterly re-surface: topic includes the quarter.
    quarter = (today.month - 1) // 3 + 1
    quarter_key = f"{today.year}Q{quarter}"

    for ent in entities:
        if ent.kind not in CORP_KINDS:
            continue
        if ent.country_code != "US":
            continue

        # Look at last completed tax year (the one we'd be filing now).
        last_ty = today.year - 1
        if ent.incorporated_at and ent.incorporated_at.year > last_ty:
            continue

        amount = await _rd_volume_last_year(ctx.session, str(ent.id), last_ty)
        if amount <= 0:
            continue  # nothing R&D-flavored to flag

        topic = f"sentinel-sec174-{ent.slug}-{quarter_key}-{last_ty}"
        title = f"§174 R&E capitalization reminder — {ent.display_name} TY{last_ty}"
        summary = (
            f"{ent.display_name} has ${amount:,.2f} of R&D-flavored expense in "
            f"TY{last_ty} (matched on SaaS · AI / Dev / Research / Cloud · GPU "
            f"categories). Under TCJA-amended §174, these must be CAPITALIZED + "
            f"AMORTIZED over 5 years (domestic) or 15 years (foreign), NOT "
            f"deducted as ordinary expense. Verify the 1120 draft applies §174 "
            f"amortization and Form 4562 includes the amortization schedule."
        )
        res = await execute_action(
            ctx,
            action_type="flag_for_cpa_review",
            entity_id=ent.id,
            title=title,
            summary=summary,
            severity="high",
            details={
                "tax_year": last_ty,
                "rd_amount_usd": str(amount),
                "section": "§174",
                "form_references": ["1120 line 26", "Form 4562"],
            },
            payload={
                "tax_year": last_ty,
                "topic": topic,
                "confidence": "high",
                "form_reference": "1120-line-26",
                "recommendation": (
                    "Confirm §174 amortization schedule is reflected on Form 4562 "
                    "and the deduction on 1120 line 26 uses the amortized portion "
                    "(not full expense)."
                ),
                "citations": [
                    {"type": "section", "value": "§174"},
                    {"type": "form", "value": "Form 4562"},
                ],
            },
            reasoning=(
                f"§174 compliance: {ent.slug} has ${amount} in R&D-flavored "
                f"expense for TY{last_ty}."
            ),
        )
        findings += 1
        if res.get("ok"):
            actions += 1

    return {
        "findings": findings,
        "actions": actions,
        "summary": f"sec_174_compliance: {findings} entities flagged",
    }
