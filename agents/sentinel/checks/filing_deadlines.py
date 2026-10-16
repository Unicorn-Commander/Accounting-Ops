"""Filing deadlines check.

For each active entity:
  - Federal 1120 due (April 15, year+1 for C-Corp) — flag if within 60 days.
  - SC1120 due — flag if within 60 days AND entity is SC-registered.
  - Form 7004 extension — flag if within 30 days of original deadline.
  - Quarterly estimated payments (4/15, 6/15, 9/15, 1/15) — flag within 30 days.
  - 1099-NEC issuance (1/31) — flag within 30 days.

Skips entities incorporated AFTER the tax_year (no obligation yet).

Cadence: daily. Drafts a memo on the first detection; schedules a reminder
for the day-before; relies on de-dup (same memo content for same day) to
avoid re-flagging.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import (
    EntitySummary,
    days_until,
    estimated_tax_due_dates,
    federal_return_deadline,
    form_7004_deadline,
    list_active_entities,
    return_form_for_kind,
    sc1120_deadline,
)
from app.core.business_time import business_day_bounds, business_today

CORP_KINDS = {"c_corp", "s_corp"}
SC_ENTITY_KINDS = {"c_corp", "s_corp", "llc", "partnership"}  # SC-registered subset
DEADLINE_WINDOW_DAYS_TAX = 60
DEADLINE_WINDOW_DAYS_EST = 30
DEADLINE_WINDOW_DAYS_EXTENSION = 30
DEADLINE_WINDOW_DAYS_1099 = 30


async def _existing_memo_today(
    ctx: ActionContext,
    entity_id: UUID,
    topic_key: str,
    *,
    business_date: dt.date | None = None,
) -> bool:
    """Has Sentinel already drafted a memo with this topic_key in today's window?

    Used to make re-runs idempotent within a business-local day.
    """
    day = business_date or business_today()
    business_day_start, _ = business_day_bounds(day)
    row = (
        await ctx.session.execute(
            text(
                """
                SELECT 1
                FROM tax_review_queue
                WHERE entity_id = :eid
                  AND submitter_id = 'sentinel/v1'
                  AND created_at >= :business_day_start
                  AND content->>'topic' = :topic
                LIMIT 1
                """
            ),
            {
                "eid": str(entity_id),
                "topic": topic_key,
                "business_day_start": business_day_start,
            },
        )
    ).first()
    return row is not None


async def _flag_deadline(
    ctx: ActionContext,
    entity: EntitySummary,
    *,
    form: str,
    due: dt.date,
    days: int,
    severity: str,
    business_date: dt.date | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    topic = f"sentinel-deadline-{form}-{due.isoformat()}"
    if await _existing_memo_today(ctx, entity.id, topic, business_date=business_date):
        return {"ok": False, "skipped_reason": "deduped_same_day"}

    title = f"{form} due in {days} day{'s' if days != 1 else ''} ({due.isoformat()})"
    summary = (
        f"{entity.display_name} ({entity.kind.upper()}): {form} filing deadline is "
        f"{due.isoformat()} — {days} days away. Confirm draft is in CPA review."
    )
    return await execute_action(
        ctx,
        action_type="draft_memo",
        entity_id=entity.id,
        title=title,
        summary=summary,
        severity=severity,
        details={"form": form, "due": due.isoformat(), "days_until": days, **(extra or {})},
        payload={
            "tax_year": due.year - 1 if form in ("1120", "SC1120", "7004") else due.year,
            "topic": topic,
            "confidence": "high",
        },
        # One open finding per (form, due date) — multiple deadlines per entity.
        dedupe_key=topic,
        reasoning=(
            f"Filing deadline scanner: {form} for {entity.slug} is due {due.isoformat()} "
            f"({days} days away)."
        ),
        target_date=due,
    )


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    """Run the filing-deadlines check across all active entities."""
    today = business_today()
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0

    for ent in entities:
        # Determine the tax year to consider for filings:
        #   For a C/S corp incorporated 2023, in May 2026 we care about 2025 returns
        #   (due 4/15/2026). Compute tax_year as "the most recent year for which a
        #   return is or recently was due".
        return_form = return_form_for_kind(ent.kind)
        if return_form:
            candidate_years = []
            for ty in (today.year - 2, today.year - 1, today.year):
                if ent.incorporated_at and ent.incorporated_at.year > ty:
                    continue
                candidate_years.append(ty)
            for tax_year in candidate_years:
                # Federal return — 1120 / 1120-S / 1065 / 1040 by entity kind.
                # Flow-throughs are due a month earlier than 1120 and 1040 filers.
                fed_due = federal_return_deadline(tax_year, ent.kind)
                d = days_until(fed_due, today)
                if 0 <= d <= DEADLINE_WINDOW_DAYS_TAX:
                    res = await _flag_deadline(
                        ctx, ent, form=return_form, due=fed_due, days=d,
                        severity="high" if d <= 14 else "medium",
                        business_date=today,
                    )
                    findings += 1
                    if res.get("ok"):
                        actions += 1

                # Form 7004 extension (business returns only; 1040 filers use 4868)
                if return_form != "1040":
                    ext_due = form_7004_deadline(tax_year, ent.kind)
                    d_ext = days_until(ext_due, today)
                    if 0 <= d_ext <= DEADLINE_WINDOW_DAYS_EXTENSION:
                        res = await _flag_deadline(
                            ctx, ent, form="7004", due=ext_due, days=d_ext,
                            severity="medium",
                            business_date=today,
                            extra={"note": f"File 7004 if {return_form} won't be ready."},
                        )
                        findings += 1
                        if res.get("ok"):
                            actions += 1

                # SC1120 — only if SC-registered (proxy: country_code US and entity has SC presence)
                # For Acme (SC C-Corp), this fires. Other US C-Corps without
                # SC presence get a false positive; future: add jurisdiction column.
                if ent.country_code == "US" and ent.kind == "c_corp":
                    sc_due = sc1120_deadline(tax_year)
                    d_sc = days_until(sc_due, today)
                    if 0 <= d_sc <= DEADLINE_WINDOW_DAYS_TAX:
                        res = await _flag_deadline(
                            ctx, ent, form="SC1120", due=sc_due, days=d_sc,
                            severity="high" if d_sc <= 14 else "medium",
                            business_date=today,
                            extra={"note": "Only applies if SC-registered."},
                        )
                        findings += 1
                        if res.get("ok"):
                            actions += 1

            # Estimated tax dates for the CURRENT year. Entity-level obligation
            # for corporations; for flow-throughs the owners pay personally, so
            # do not flag it against the partnership/LLC itself.
            for est_due in (
                estimated_tax_due_dates(today.year) if ent.kind in CORP_KINDS else []
            ):
                d_est = days_until(est_due, today)
                if 0 <= d_est <= DEADLINE_WINDOW_DAYS_EST:
                    res = await _flag_deadline(
                        ctx, ent, form="estimated-tax", due=est_due, days=d_est,
                        severity="medium",
                        business_date=today,
                        extra={"quarter": _quarter_of(est_due)},
                    )
                    findings += 1
                    if res.get("ok"):
                        actions += 1

            # 1099-NEC issuance (January 31)
            for ty in (today.year - 1, today.year):
                form_1099_due = dt.date(ty + 1, 1, 31)
                if form_1099_due.weekday() == 5:
                    form_1099_due += dt.timedelta(days=2)
                elif form_1099_due.weekday() == 6:
                    form_1099_due += dt.timedelta(days=1)
                d_1099 = days_until(form_1099_due, today)
                if 0 <= d_1099 <= DEADLINE_WINDOW_DAYS_1099:
                    res = await _flag_deadline(
                        ctx, ent, form="1099-NEC", due=form_1099_due, days=d_1099,
                        severity="high",
                        business_date=today,
                        extra={"note": "Run the 1099-NEC specialist if not already drafted."},
                    )
                    findings += 1
                    if res.get("ok"):
                        actions += 1

    summary = f"deadlines: {findings} findings, {actions} actions taken"
    return {"findings": findings, "actions": actions, "summary": summary}


def _quarter_of(d: dt.date) -> str:
    if d.month in (1, 4):
        return f"Q1 {d.year}" if d.month == 4 else f"Q4 {d.year - 1}"
    if d.month == 6:
        return f"Q2 {d.year}"
    if d.month == 9:
        return f"Q3 {d.year}"
    return f"Q? {d.year}"
