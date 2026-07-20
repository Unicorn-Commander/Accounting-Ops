"""MCP tool: get_form_1065_data — fetch the full Form 1065 export package.

Calls the accounting-ops-cpa-export sidecar:
    GET /exports/1065/{year}?workspace_id=…&entity_id=…

Auth mirrors ``tax_get_form_1120_data`` exactly (audit 2026-05-31 Bug 10) via
the shared ``_cpa_export_client``: a short-lived HMAC-signed internal assertion
bound to the validated caller travels in ``X-Internal-Assertion``, with the
service token as a transport handshake only.

DRAFT LABEL: every returned payload carries ``_draft_notice``.

Role floor: viewer (read-only access to tax data).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.tools._cpa_export_client import fetch_return_package


async def get_form_1065_data(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> dict[str, Any]:
    """Fetch the full Form 1065 (partnership) export package.

    Returns the JSON package produced by the cpa-export service:

      * ``income`` / ``deductions`` — page-1 lines 1a–21.
      * ``ordinary_business_income_cents`` — page 1 line 22.
      * ``schedule_k`` — the SEPARATELY-STATED items, excluded from ordinary
        business income because they keep their character in the partner's
        hands (§702(a)). Includes guaranteed payments (§707(c)), which are
        uniquely BOTH a page-1 line 10 deduction and a Schedule K line 4a/4b
        item.
      * ``schedule_l`` — balance sheet; assets equal liabilities plus capital.
        Note the 1065 numbers its balance sheet differently from the corporate
        forms (fixed assets at 9a/9b; all equity on line 21).
      * ``schedule_k1`` — one K-1 per partner, but ONLY when the partnership
        agreement's allocation basis has been recorded on the partner rows.

        A partnership may allocate disproportionately under §704(a), so pro rata
        is NOT assumed. When the basis is unrecorded, the agreement makes
        special allocations, percentages do not total 100%, or any interest
        changed mid-year (§706(d)), ``issued`` is false, ``requires_override``
        is true, and ``reason`` explains what is needed. The ENTITY-LEVEL
        figures are still valid in every refusal case — only the per-partner
        split is withheld.

        Guaranteed payments are never spread across partners; they appear under
        ``items_requiring_owner_attribution`` because they are paid to a NAMED
        partner and are that partner's ordinary and self-employment income.

      * ``review_flags`` — what the ledger cannot decide.

    DRAFT — for CPA review only. Do not file or share without professional review.

    Role floor: viewer (read-only access to tax data).
    """
    return await fetch_return_package(
        form_path="1065",
        form_label="1065",
        workspace_id=workspace_id,
        entity_id=entity_id,
        tax_year=tax_year,
    )
