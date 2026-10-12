"""MCP tool: get_form_1120_data — fetch the full 1120 export package.

Calls the accounting-ops-cpa-export sidecar:
    GET /exports/1120/{year}?workspace_id=…&entity_id=…

Audit 2026-05-31 Bug 10: the previous design forwarded a static
``ACCTOPS_SERVICE_TOKEN`` to the sidecar, so the sidecar could not validate the
ACTUAL caller's identity / workspace / entity / role. If the service token's
own permissions ever drifted broader than the caller's the sidecar could
return data outside the caller's scope. The fix: after this tool's local
``authorize_workspace`` succeeds, mint a short-lived (≤5 min) HMAC-signed
internal assertion bound to the caller (uc_uid, workspace_id, entity_id, role,
scopes) and send it via the ``X-Internal-Assertion`` header. The sidecar
verifies the signature with the same master key and uses the assertion's
identity for its own authorization. The ``ACCTOPS_SERVICE_TOKEN`` bearer is
still attached as a transport handshake so a random caller can't even reach
the sidecar — but identity now flows through the assertion, not the token.

DRAFT LABEL: Every returned payload carries a top-level ``_draft_notice`` field
so any consumer knows this is for CPA review only.

Role floor: viewer (read-only access to tax data).
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.tools._cpa_export_client import fetch_return_package


async def get_form_1120_data(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> dict[str, Any]:
    """Fetch the full Form 1120 export package for an entity and tax year.

    Returns the JSON package produced by the cpa-export service, including
    the per-line amounts, Schedule L balance sheet, and the CPA-flagged
    categories list.

    DRAFT — for CPA review only. Do not file or share without professional review.

    Role floor: viewer (read-only access to tax data).
    """
    return await fetch_return_package(
        form_path="1120",
        form_label="1120",
        workspace_id=workspace_id,
        entity_id=entity_id,
        tax_year=tax_year,
    )
