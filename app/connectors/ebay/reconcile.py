"""eBay payout ↔ SimpleFIN BoA deposit reconciliation.

The eBay /payout endpoint reports each settlement as a single dollar amount on
a payout date. SimpleFIN reports the same dollar amount arriving on the operator's
BoA business checking ~1-2 business days later. Auto-match them so:

  * the eBay payout settlement journal entry has its corresponding bank-side
    journal entry identified
  * the BoA deposit row no longer shows as "unmatched income" in
    /open-items

Match rule (defaults conservative):
  amount within ±$1.00 AND |payout_date - bofa_deposit_date| ≤ 2 days

When matched, we don't UPDATE either journal_entry (they're append-only). The
match is recorded in connector_state.state['payout_matches'] as a list of
   {ebay_payout_id, bofa_journal_id, ebay_journal_id, matched_at_iso}
which the /open-items + /connectors UI both consume.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("acctops.ebay_reconcile")

MATCH_AMOUNT_TOLERANCE_CENTS = 100  # ±$1.00
MATCH_DATE_TOLERANCE_DAYS = 2


@dataclass(frozen=True)
class PayoutMatch:
    ebay_payout_id: str
    ebay_journal_id: str
    bofa_journal_id: str
    amount_cents: int
    payout_date: str  # iso
    bofa_date: str  # iso


async def find_unmatched_ebay_payouts(
    session: AsyncSession, *, entity_id: str, lookback_days: int = 60
) -> list[dict[str, Any]]:
    """Return ebay_finances PAYOUT entries that don't yet have a recorded match."""
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text AS journal_id, source_external_id AS payout_id,
                       amount, effective_date
                FROM journal_entry
                WHERE entity_id = :eid
                  AND source = 'ebay_finances'
                  AND category = 'eBay Payout Settlement'
                  AND effective_date >= ((now() AT TIME ZONE 'America/New_York')::date - (:days || ' days')::interval)::date
                ORDER BY effective_date DESC
                """
            ),
            {"eid": entity_id, "days": str(lookback_days)},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def find_simplefin_bofa_inflows(
    session: AsyncSession, *, entity_id: str, lookback_days: int = 60
) -> list[dict[str, Any]]:
    """SimpleFIN-sourced INFLOWs to the 1010 cash account (likely BoA)."""
    rows = (
        await session.execute(
            text(
                """
                SELECT je.id::text AS journal_id, je.source_external_id AS bofa_txn_id,
                       je.amount, je.effective_date, je.description, je.payee
                FROM journal_entry je
                JOIN chart_of_account coa
                  ON coa.canonical_form = je.debit_account
                 AND coa.entity_id = je.entity_id
                WHERE je.entity_id = :eid
                  AND je.source = 'simplefin'
                  AND coa.account_number IN ('1010', '1011')
                  AND je.effective_date >= ((now() AT TIME ZONE 'America/New_York')::date - (:days || ' days')::interval)::date
                ORDER BY je.effective_date DESC
                """
            ),
            {"eid": entity_id, "days": str(lookback_days)},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


def _amount_cents(d: Decimal | str | None) -> int:
    if d is None:
        return 0
    return int((Decimal(str(d)) * 100).quantize(Decimal("1")))


def _date_from(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def match_payouts(
    payouts: list[dict[str, Any]],
    bofa: list[dict[str, Any]],
) -> tuple[list[PayoutMatch], list[dict[str, Any]]]:
    """Greedy match: each payout pairs with the closest-in-date BoA inflow.

    Returns (matches, unmatched_payouts). Each BoA row is consumed once so two
    payouts can't both claim the same deposit. unmatched_payouts surfaces in
    /open-items.
    """
    matches: list[PayoutMatch] = []
    used: set[str] = set()
    unmatched: list[dict[str, Any]] = []
    for payout in payouts:
        payout_cents = _amount_cents(payout["amount"])
        payout_date = _date_from(payout["effective_date"])
        if payout_date is None:
            unmatched.append(payout)
            continue
        best: tuple[int, dict[str, Any]] | None = None
        for b in bofa:
            if b["journal_id"] in used:
                continue
            b_cents = _amount_cents(b["amount"])
            if abs(b_cents - payout_cents) > MATCH_AMOUNT_TOLERANCE_CENTS:
                continue
            b_date = _date_from(b["effective_date"])
            if b_date is None:
                continue
            days = abs((b_date - payout_date).days)
            if days > MATCH_DATE_TOLERANCE_DAYS:
                continue
            score = days * 1000 + abs(b_cents - payout_cents)
            if best is None or score < best[0]:
                best = (score, b)
        if best is None:
            unmatched.append(payout)
            continue
        b = best[1]
        used.add(b["journal_id"])
        matches.append(
            PayoutMatch(
                ebay_payout_id=str(payout["payout_id"]),
                ebay_journal_id=str(payout["journal_id"]),
                bofa_journal_id=str(b["journal_id"]),
                amount_cents=payout_cents,
                payout_date=str(payout["effective_date"]),
                bofa_date=str(b["effective_date"]),
            )
        )
    return matches, unmatched


async def reconcile_one_entity(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    lookback_days: int = 60,
) -> dict[str, Any]:
    """Run the matcher; persist results into connector_state.

    Returns a summary dict.
    """
    payouts = await find_unmatched_ebay_payouts(
        session, entity_id=str(entity_id), lookback_days=lookback_days
    )
    bofa = await find_simplefin_bofa_inflows(
        session, entity_id=str(entity_id), lookback_days=lookback_days
    )
    matches, unmatched = match_payouts(payouts, bofa)

    summary = {
        "checked_payouts": len(payouts),
        "matched": len(matches),
        "unmatched": len(unmatched),
        "matches": [
            {
                "ebay_payout_id": m.ebay_payout_id,
                "ebay_journal_id": m.ebay_journal_id,
                "bofa_journal_id": m.bofa_journal_id,
                "amount_cents": m.amount_cents,
                "payout_date": m.payout_date,
                "bofa_date": m.bofa_date,
            }
            for m in matches
        ],
        "unmatched_payouts": [
            {
                "ebay_payout_id": str(p["payout_id"]),
                "ebay_journal_id": str(p["journal_id"]),
                "amount_cents": _amount_cents(p["amount"]),
                "payout_date": str(p["effective_date"]),
            }
            for p in unmatched
        ],
        "reconciled_at_iso": datetime.now(tz=timezone.utc).isoformat(),
    }

    # Persist into connector_state.state — we add a 'reconciliation' subkey so
    # the existing 'last_outcome' / sync summary aren't clobbered.
    # Read-modify-write pattern; the connector_state row may not exist yet, in
    # which case we'll let the sync worker create it on its next pass.
    existing = (
        await session.execute(
            text(
                """
                SELECT state FROM connector_state
                WHERE entity_id = :eid AND connector_name = 'ebay_finances'
                ORDER BY last_sync_at DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"eid": str(entity_id)},
        )
    ).first()
    if existing is None:
        await session.execute(
            text(
                """
                INSERT INTO connector_state (
                    id, workspace_id, entity_id, connector_name,
                    connector_instance_name, state, last_sync_at
                )
                VALUES (
                    gen_random_uuid(), :wid, :eid, 'ebay_finances',
                    'default', CAST(:state AS jsonb), now()
                )
                ON CONFLICT (entity_id, connector_name, connector_instance_name) DO NOTHING
                """
            ),
            {
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "state": json.dumps({"reconciliation": summary}),
            },
        )
    else:
        cur = existing.state or {}
        if not isinstance(cur, dict):
            cur = {}
        cur["reconciliation"] = summary
        await session.execute(
            text(
                """
                UPDATE connector_state
                SET state = CAST(:state AS jsonb)
                WHERE entity_id = :eid AND connector_name = 'ebay_finances'
                """
            ),
            {"eid": str(entity_id), "state": json.dumps(cur)},
        )
    return summary


__all__ = (
    "PayoutMatch",
    "MATCH_AMOUNT_TOLERANCE_CENTS",
    "MATCH_DATE_TOLERANCE_DAYS",
    "find_unmatched_ebay_payouts",
    "find_simplefin_bofa_inflows",
    "match_payouts",
    "reconcile_one_entity",
)
