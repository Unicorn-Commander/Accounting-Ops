"""MCP tool: get_1099_payees — aggregate contractor payees above the threshold.

Used by the 1120 Preparer specialist to identify potential 1099-NEC filing
obligations and to support the contractor/professional-services deduction
lines on Form 1120.

Delegates to the canonical ``form_1099_nec`` report (app/api/reports.py) for
aggregation: it sums the DEBIT side of posted journal entries booked to the
contract-labor GL accounts, NETS refunds (credits back to those accounts)
DOWN, and groups by counterparty. This is the SAME basis + netting rule the
HTTP report and ``app/tools/reports_extra.py::get_1099_nec_report`` use, so the
two 1099-NEC surfaces can never disagree (no more SUM(ABS) inflation, and a
payee with a NULL counterparty_id still groups into a single row via the
ledger's payee text instead of splitting per descriptor).

Returns payees with cumulative NETTED payments at or above the threshold
(default $600 = 60,000 cents).

DRAFT LABEL: Results carry ``_draft_notice``.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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


def _dollars_to_cents(amount: Any) -> int:
    """Convert a decimal-dollars value (str/Decimal/number) to integer cents.

    ``form_1099_nec`` returns each payee's netted total as a decimal-dollars
    STRING (e.g. "7542.29"); this tool's contract is integer cents. Use Decimal
    so the money stays exact, and round half-up to the nearest cent.
    """
    if amount is None:
        return 0
    try:
        cents = (Decimal(str(amount)) * 100).to_integral_value(rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return 0
    return int(cents)


async def get_1099_payees(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    threshold_cents: int = 60_000,
) -> dict[str, Any]:
    """Aggregate contractor payees at or above the 1099-NEC threshold.

    Delegates to ``form_1099_nec`` for aggregation (debit side of the
    contract-labor GL accounts, refunds netted down, grouped by counterparty).
    Returns each payee whose NETTED total payments reach or exceed
    ``threshold_cents`` (default 60,000 = $600). Because the netting is shared
    with the canonical report, a contractor refund reduces that contractor's
    total (no SUM(ABS) inflation) and a payee is reported ONCE even when its
    journal entries carry a NULL counterparty_id.

    Note:
        The shared ``form_1099_nec`` path always filters at the $600 (60,000
        cents) threshold. If ``threshold_cents`` is ABOVE 60,000 this tool
        post-filters the netted rows up to it. If ``threshold_cents`` is BELOW
        60,000 the shared path has already dropped the sub-$600 payees, so the
        tool cannot surface them — the delegated basis does not support
        sub-$600 thresholds.

        # VERIFY-AT-GATE: form_1099_nec hard-filters at $600 (NEC_THRESHOLD),
        # so a threshold_cents below 60,000 cannot surface sub-$600 payees
        # through this delegated path. Accepted: the 1099-NEC obligation itself
        # begins at $600, so a lower threshold has no filing meaning here.

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
                    "first_payment_date": null,
                    "last_payment_date": null,
                    "categories_seen": [],
                    "requires_human_review": false
                },
                ...
            ],
            "categories_checked": ["5220", "5630", "5635", "6300"],
            "_draft_notice": "..."
        }

    ``first_payment_date`` / ``last_payment_date`` / ``categories_seen`` are
    retained for return-shape compatibility but are not carried by the shared
    report (which groups by GL account + counterparty, not by date or category);
    they are null / empty. ``categories_checked`` now reflects the contract-labor
    GL accounts that define the basis. ``requires_human_review`` flags a payee
    whose name matches a known payment processor (likely 1099-K, not 1099-NEC).

    DRAFT — for CPA review only. The aggregation identifies filing CANDIDATES;
    the CPA must verify actual obligations (corporate-status and payment-method
    exemptions are not applied here).

    Role floor: viewer.
    """
    # Keep the tool's existing authorization as-is, but CAPTURE the authorized
    # context. form_1099_nec re-checks the role, enforces entity scope, and
    # opens its DB session from ``workspace.workspace_id`` — so it needs the
    # real WorkspaceContext (passing None crashes it). This mirrors
    # app/tools/reports_extra.py::get_1099_nec_report, the working reference.
    workspace = await authorize_workspace(workspace_id, entity_id, min_role="viewer")

    from app.api.reports import form_1099_nec as _http_1099_nec

    result = await _http_1099_nec(
        workspace_id=workspace_id,
        year=str(tax_year),
        entity_id=str(entity_id),
        workspace=workspace,
    )

    # form_1099_nec payee rows use keys: payee, total_amount (decimal-dollars
    # STRING), posting_count, requires_human_review. Map them into this tool's
    # documented shape (integer cents), preserving the report's descending order.
    form_payees = result.get("payees", [])
    payees: list[dict[str, Any]] = []
    for p in form_payees:
        total_cents = _dollars_to_cents(p.get("total_amount"))
        if total_cents >= threshold_cents:
            payees.append(
                {
                    "payee_name": _b(p.get("payee")),
                    "total_amount_cents": total_cents,
                    "entry_count": int(p.get("posting_count", 0) or 0),
                    "first_payment_date": None,
                    "last_payment_date": None,
                    "categories_seen": [],
                    "requires_human_review": bool(p.get("requires_human_review", False)),
                }
            )

    return {
        "entity_id": str(entity_id),
        "tax_year": tax_year,
        "threshold_cents": threshold_cents,
        "payees": payees,
        # Basis is now the shared report's contract-labor GL accounts, not the
        # old category-name heuristic.
        "categories_checked": list(result.get("basis_accounts", [])),
        "_draft_notice": (
            "DRAFT — for CPA review only. "
            "Payee aggregation is heuristic (category-based). "
            "Actual 1099-NEC filing obligations depend on entity type, services "
            "rendered, and IRS thresholds for the applicable year. "
            "CPA must verify each payee before filing."
        ),
    }
