"""MCP tool: get_1099_payees — aggregate contractor payees above the threshold.

Used by the 1120 Preparer specialist to identify potential 1099-NEC filing
obligations and to support the contractor/professional-services deduction
lines on Form 1120.

Aggregates posted journal entries by ``payee`` (or ``description`` when payee
is null) for contractor-like categories. Returns payees with cumulative
payments at or above the threshold (default $600 = 60,000 cents).

Categories considered contractor-like (canonical platform names):
  - "Contractor · 1099-NEC"
  - "Contractor · Other"
  - "Legal · Services"
  - "Professional · Services"

These map to Form 1120 line 26 (Other deductions) per tax_mapping_1120.py.

DRAFT LABEL: Results carry ``_draft_notice``.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import entity_session

# Canonical category names that indicate contractor / professional-services
# payments requiring 1099-NEC analysis.  Kept as a module-level constant so
# the specialist (and tests) can reference the exact set without hard-coding.
CONTRACTOR_CATEGORIES: tuple[str, ...] = (
    "Contractor · 1099-NEC",
    "Contractor · Other",
    "Legal · Services",
    "Professional · Services",
)


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

    Scans posted journal entries in the given tax year for contractor-like
    categories and returns each payee (or description fallback) whose total
    payments reach or exceed ``threshold_cents`` (default 60,000 = $600).

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

    # Date range: full calendar year for tax_year
    # asyncpg requires actual date objects; passing strings yields
    # 'invalid input for query argument' DataError.
    from datetime import date as _date
    start_date = _date(tax_year, 1, 1)
    end_date = _date(tax_year, 12, 31)

    # Build category filter as positional placeholders.
    # We use ILIKE for resilience against minor capitalization differences, but
    # also include exact match — use IN for performance (exact match on indexed
    # column is fast; ILIKE would disable index use).
    # The categories are controlled constants so injection is not a concern, but
    # we use SQLAlchemy bound params anyway for the habit.
    cat_params = {f"cat_{i}": c for i, c in enumerate(CONTRACTOR_CATEGORIES)}
    cat_placeholders = ", ".join(f":cat_{i}" for i in range(len(CONTRACTOR_CATEGORIES)))

    params: dict[str, Any] = {
        "entity_id": str(entity_id),
        "start_date": start_date,
        "end_date": end_date,
        "threshold": float(threshold_cents) / 100.0,
        **cat_params,
    }

    # Aggregate by payee (COALESCE to description when payee is null).
    # amount is stored as DECIMAL(19,2) in dollars; multiply by 100 → cents.
    # We use ABS because outflows are stored as negative amounts; the 1099
    # threshold applies to the absolute payment amount.
    sql = text(
        f"""
        SELECT
            COALESCE(NULLIF(TRIM(payee), ''), TRIM(description), 'Unknown') AS payee_name,
            ROUND(SUM(ABS(amount)) * 100)::bigint AS total_amount_cents,
            COUNT(*)::int AS entry_count,
            MIN(effective_date)::text AS first_payment_date,
            MAX(effective_date)::text AS last_payment_date,
            array_agg(DISTINCT category ORDER BY category) AS categories_seen
        FROM journal_entry
        WHERE entity_id = :entity_id
          AND status = 'posted'
          AND effective_date >= :start_date
          AND effective_date <= :end_date
          AND category IN ({cat_placeholders})
        GROUP BY COALESCE(NULLIF(TRIM(payee), ''), TRIM(description), 'Unknown')
        HAVING SUM(ABS(amount)) >= :threshold
        ORDER BY SUM(ABS(amount)) DESC
        """
    )

    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, _eid):
        rows = (await session.execute(sql, params)).mappings().all()

    payees = []
    for r in rows:
        cats = r["categories_seen"]
        # asyncpg returns arrays as Python lists; normalize to list[str]
        if isinstance(cats, list):
            cats_list = [str(c) for c in cats if c is not None]
        else:
            cats_list = [str(cats)] if cats else []
        payees.append({
            "payee_name": _b(r["payee_name"]),
            "total_amount_cents": int(r["total_amount_cents"]) if r["total_amount_cents"] is not None else 0,
            "entry_count": int(r["entry_count"]),
            "first_payment_date": _b(r["first_payment_date"]),
            "last_payment_date": _b(r["last_payment_date"]),
            "categories_seen": cats_list,
        })

    return {
        "entity_id": str(entity_id),
        "tax_year": tax_year,
        "threshold_cents": threshold_cents,
        "payees": payees,
        "categories_checked": list(CONTRACTOR_CATEGORIES),
        "_draft_notice": (
            "DRAFT — for CPA review only. "
            "Payee aggregation is heuristic (category-based). "
            "Actual 1099-NEC filing obligations depend on entity type, services "
            "rendered, and IRS thresholds for the applicable year. "
            "CPA must verify each payee before filing."
        ),
    }
