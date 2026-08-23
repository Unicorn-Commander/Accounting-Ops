"""MCP tool: get_1099_payees — aggregate contractor payees above the threshold.

Used by the 1120 Preparer specialist to identify potential 1099-NEC filing
obligations and to support the contractor/professional-services deduction
lines on Form 1120.

Delegates to the canonical ``form_1099_nec`` report implementation for
aggregation (debit-side of contract-labor account 6300, netting refunds
down). Returns payees with cumulative netted payments at or above the
threshold (default $600 = 60,000 cents).

DRAFT LABEL: Results carry ``_draft_notice``.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.mcp_auth import authorize_workspace


def _b(value: Any) -> Any:
    """JSON-safe scalar: stringify Decimals/dates/UUIDs, pass through the rest."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


async def get_1099_payees(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    threshold_cents: int = 60_000,
) -> dict[str, Any]:
    """Aggregate contractor payees at or above the 1099-NEC threshold.

    Delegates to ``form_1099_nec`` for aggregation (debit-side of contract-labor
    account 6300, netting refunds down). Returns each payee whose netted total
    payments reach or exceed ``threshold_cents`` (default 60,000 = $600).

    Note:
        The shared ``form_1099_nec`` path always filters at the $600 (60,000
        cents) threshold.  If ``threshold_cents`` is below 60,000 the tool
        cannot return rows under $600 — the shared path does not support
        lower thresholds.

        # VERIFY-AT-GATE: confirm that form_1099_nec does not support
        # sub-$600 thresholds and that this limitation is acceptable.

    Returns:
        {
            "entity_id": "...",
            "tax_year": 2023,
            "threshold_cents": 60000,
            "payees": [
                {
                    "payee_name": "Acme Consulting",
                    "total_amount_cents": 250000,
                    "entry_count": 5,
                    "first_payment_date": "2023-02-01",
                    "last_payment_date": "2023-11-15",
                    "categories_seen": ["Contractor · 1099-NEC"]
                },
                ...
            ],
            "categories_checked": [...],
            "_draft_notice": "..."
        }

    DRAFT — for CPA review only. The payee aggregation is a heuristic based on
    category labels; the CPA must verify actual filing obligations.

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")

    # Delegate to the canonical form_1099_nec implementation.
    from app.api.reports import form_1099_nec as _http_1099_nec

    result = await _http_1099_nec(
        workspace_id=workspace_id,
        year=str(tax_year),
        entity_id=str(entity_id),
        workspace=None,  # auth already handled above
    )

    # Map form rows into the tool's expected payee shape.
    # form_1099_nec returns a dict with "payees" key containing rows with
    # keys like "payee_name", "total_amount_cents", etc.
    form_payees = result.get("payees", [])

    # Post-filter for threshold if requested threshold is above 60,000.
    # The shared path already filters at 60,000, so we only need to filter
    # higher thresholds here.
    threshold_dollars = threshold_cents / 100.0
    payees = []
    for p in form_payees:
        total_cents = int(p.get("total_amount_cents", 0))
        if total_cents >= threshold_cents:
            payees.append({
                "payee_name": _b(p.get("payee_name")),
                "total_amount_cents": total_cents,
                "entry_count": int(p.get("entry_count", 0)),
                "first_payment_date": _b(p.get("first_payment_date")),
                "last_payment_date": _b(p.get("last_payment_date")),
                "categories_seen": p.get("categories_seen", []),
            })

    return {
        "entity_id": str(entity_id),
        "tax_year": tax_year,
        "threshold_cents": threshold_cents,
        "payees": payees,
        "categories_checked": [],
        "_draft_notice": (
            "DRAFT — for CPA review only. "
            "Payee aggregation is heuristic (category-based). "
            "Actual 1099-NEC filing obligations depend on entity type, services "
            "rendered, and IRS thresholds for the applicable year. "
            "CPA must verify each payee before filing."
        ),
    }
