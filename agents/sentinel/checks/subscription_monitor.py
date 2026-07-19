"""Recurring subscription monitor check.

Runs the existing pure subscription detector (``app.subscriptions.detect``) over
each active entity's posted journal and surfaces every currently-recurring
subscription as a human-reviewable finding via ``flag_for_cpa_review``.

v1 scope = SURFACE FOR REVIEW only. Nothing is cancelled and no charge is
touched — the finding asks a human to decide whether the subscription is still
needed. Cancellation is a separate (browser) agent.

Recency is judged relative to **how current the books are** (the most recent
posted entry for the entity), NOT wall-clock today: a ledger is routinely a few
weeks/months behind, and judging "is this still active?" against today would
mark every subscription stale and surface nothing. Anchoring ``as_of`` to the
latest entry keeps the detector's cadence-aware ``is_active`` honest (so an
annual subscription mid-cycle still counts) while genuinely-dead recurring
charges (cancelled long before the ledger's end) correctly drop out.

Mirrors the DB layer of ``app.tools.subscriptions.detect_subscriptions_tool``:
it pulls the same posted ``journal_entry`` window (by ``effective_date``) for
the entity and feeds the rows straight into ``detect_subscriptions``. The
session is already workspace-pinned (search_path + RLS) by the runner.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import text

from agents.sentinel.actions import ActionContext, execute_action
from agents.sentinel.checks._helpers import list_active_entities
from app.subscriptions.detect import detect_subscriptions

#: Trailing window scanned per entity (months). Matches the tool's default; an
#: annual subscription needs multiple years of history, so keep it generous.
LOOKBACK_MONTHS = 24

#: Surface a subscription if its most recent charge is within this many months of
#: the ledger's latest entry (book-relative), OR the detector's cadence-aware
#: ``is_active`` is true. Inclusive on purpose: a partially-loaded latest month
#: can push a still-live monthly sub just past the tight is_active window, and a
#: missed real subscription is worse than one the user can dismiss in a click. It
#: still drops genuinely-dead charges (cancelled many months before the book end).
SURFACE_WITHIN_MONTHS = 4

#: Monthly-equivalent spend (USD) at or above which a finding is "medium" rather
#: than "low" — bigger recurring burn deserves more visible review.
MEDIUM_MONTHLY_USD = 50.0

#: Rough charges-per-month by cadence, for the monthly-equivalent estimate.
_CADENCE_PER_MONTH = {
    "weekly": 4.33,
    "biweekly": 2.17,
    "monthly": 1.0,
    "quarterly": 1.0 / 3.0,
    "annual": 1.0 / 12.0,
}

#: Columns the detector consumes (identical to detect_subscriptions_tool).
_COLUMNS = (
    "amount, currency_code, effective_date, posted_at, counterparty_id, "
    "category, source, description, payee, memo"
)


def _b(value: Any) -> Any:
    """JSON-safe scalar: stringify Decimals/dates/UUIDs, pass the rest through."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


def _monthly_equiv(typical_amount: str, cadence: str) -> float:
    """Normalize a per-charge amount to a rough monthly-equivalent USD figure."""
    try:
        amount = float(typical_amount)
    except (TypeError, ValueError):
        return 0.0
    return amount * _CADENCE_PER_MONTH.get(cadence, 1.0)


async def run(ctx: ActionContext, **_) -> dict[str, Any]:
    entities = await list_active_entities(ctx.session)
    findings = 0
    actions = 0

    today = date.today()
    start = today - timedelta(days=30 * LOOKBACK_MONTHS)

    for ent in entities:
        rows = (
            await ctx.session.execute(
                text(
                    f"SELECT {_COLUMNS} FROM journal_entry "
                    "WHERE entity_id = :entity_id "
                    "  AND status = 'posted' "
                    "  AND effective_date >= :start "
                    "  AND effective_date <= :as_of "
                    "ORDER BY effective_date ASC"
                ),
                {"entity_id": str(ent.id), "start": start, "as_of": today},
            )
        ).mappings().all()

        entries = [{k: _b(v) for k, v in dict(r).items()} for r in rows]
        if not entries:
            continue

        # Anchor recency to the ledger's own latest entry, not wall-clock today,
        # so a books-are-behind lag doesn't mark every subscription stale.
        book_as_of = max(
            (date.fromisoformat(e["effective_date"]) for e in entries if e.get("effective_date")),
            default=today,
        )
        subscriptions = detect_subscriptions(entries, as_of=book_as_of)

        for sub in subscriptions:
            if not (sub["is_active"] or sub["months_since_last"] <= SURFACE_WITHIN_MONTHS):
                # Recurring charge that stopped many months before the ledger's
                # end — already-cancelled spend, not something to nag about.
                continue

            merchant = sub["merchant"]
            cadence = sub["cadence"]
            typical = sub["typical_amount"]
            charge_count = sub["charge_count"]
            last_seen = sub["last_seen"]
            funding = sub["funding_source"] or "unknown account"
            total_paid = sub["total_paid"]
            months_since = sub["months_since_last"]

            monthly_equiv = _monthly_equiv(typical, cadence)
            severity = "medium" if monthly_equiv >= MEDIUM_MONTHLY_USD else "low"

            title = f"Recurring subscription: {merchant} (~${typical}/{cadence})"
            summary = (
                f"{ent.display_name}: recurring {cadence} charge to {merchant} of "
                f"~${typical} (~${monthly_equiv:.2f}/mo; {charge_count} charges, "
                f"${total_paid} total to date, via {funding}). Last charged "
                f"{last_seen}. Review whether it's still needed; cancel if not."
            )

            res = await execute_action(
                ctx,
                action_type="flag_for_cpa_review",
                entity_id=ent.id,
                title=title,
                summary=summary,
                severity=severity,
                # Full detector dict lands on the sentinel_finding row.
                details={**sub, "entity": ent.display_name, "monthly_equiv_usd": round(monthly_equiv, 2)},
                payload={
                    "tax_year": today.year,
                    "confidence": "medium",
                    "recommendation": (
                        "Confirm this subscription is still in use; cancel it if "
                        "no longer needed."
                    ),
                },
                # ONE self-refreshing finding per merchant per entity — NOT
                # period-stamped, so a re-detected sub refreshes in place.
                dedupe_key=f"subscription:{ent.id}:{merchant}",
                reasoning=(
                    f"Subscription monitor: recurring {cadence} charge to {merchant} "
                    f"(~${typical}, last seen {last_seen})."
                ),
            )
            findings += 1
            if res.get("ok"):
                actions += 1

    return {
        "findings": findings,
        "actions": actions,
        "summary": f"subscription_monitor: {findings} recurring subscriptions surfaced",
    }
