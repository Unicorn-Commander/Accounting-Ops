"""MCP tool: get_form_1120s_data — fetch the full Form 1120-S export package.

Calls the accounting-ops-cpa-export sidecar:
    GET /exports/1120s/{year}?workspace_id=…&entity_id=…

Auth mirrors ``tax_get_form_1120_data`` exactly (audit 2026-05-31 Bug 10): after
this tool's local ``authorize_workspace`` succeeds, mint a short-lived (≤5 min)
HMAC-signed internal assertion bound to the caller (uc_uid, workspace_id,
entity_id, role, scopes) and send it via ``X-Internal-Assertion``. The sidecar
verifies the signature with the same master key and makes its OWN authorization
decision from the assertion's identity. ``ACCTOPS_SERVICE_TOKEN`` remains a
transport handshake only.

DRAFT LABEL: every returned payload carries ``_draft_notice``.

Role floor: viewer (read-only access to tax data).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.tools._cpa_export_client import fetch_return_package


async def get_form_1120s_data(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> dict[str, Any]:
    """Fetch the full Form 1120-S (S corporation) export package.

    Returns the JSON package produced by the cpa-export service:

      * ``income`` / ``deductions`` — page-1 lines 1a–20, with gross receipts,
        cost of goods sold and gross profit broken out.
      * ``ordinary_business_income_cents`` — page 1 line 21.
      * ``schedule_k`` — the SEPARATELY-STATED items. These are deliberately
        EXCLUDED from ordinary business income because they keep their character
        in the shareholder's hands (§1366(a)(1)). Interest income, for example,
        is Schedule K line 4 and is NOT page-1 income the way it would be on a
        Form 1120.
      * ``schedule_l`` — balance sheet; assets equal liabilities plus capital.
      * ``schedule_k1`` — one K-1 per shareholder, allocated per share per day
        under §1377(a)(1). Every line foots to Schedule K to the cent. When
        shareholders are not recorded, ``issued`` is false and ``reason`` says
        why — no allocation is guessed.
      * ``review_flags`` — what the ledger cannot decide, including the
        reasonable-compensation check when distributions are paid with zero
        officer compensation.

    DRAFT — for CPA review only. Do not file or share without professional review.

    Role floor: viewer (read-only access to tax data).
    """
    return await fetch_return_package(
        form_path="1120s",
        form_label="1120-S",
        workspace_id=workspace_id,
        entity_id=entity_id,
        tax_year=tax_year,
    )
