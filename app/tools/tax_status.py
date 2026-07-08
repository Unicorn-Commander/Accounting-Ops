"""MCP tool: get_tax_status — multi-year multi-entity tax status matrix.

Mirrors GET /api/workspaces/{wid}/tax-status. Aggregates entity + approval_queue +
tax_review_queue + specialist_run into one per-entity-per-year status response
the dashboard uses.

Role floor: viewer.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from app.mcp_auth import authorize_workspace


async def get_tax_status(
    workspace_id: UUID,
    as_of: str | None = None,
) -> dict[str, Any]:
    """Return per-entity per-year tax status matrix.

    Mirrors the HTTP /tax-status endpoint. Useful for agents driving a tax
    workflow: a quick read of which entity-years are overdue, in CPA review,
    in draft, or have no draft yet.

    Args:
        workspace_id: Workspace UUID.
        as_of: Optional ISO YYYY-MM-DD anchor date; defaults to today.

    Returns:
        {
          "as_of": "YYYY-MM-DD",
          "entities": [
            {
              "entity_id": "<uuid>", "entity_slug": "...", "display_name": "...",
              "entity_type": "corp"|..., "jurisdictions": ["US", "SC"],
              "incorporated_at": "..." | null,
              "years": [
                {
                  "tax_year": 2023, "is_short_year": false,
                  "books_status": "complete"|"in_progress"|"no_data",
                  "pending_approvals": int,
                  "filing_status": "overdue"|"cpa_review"|"draft"|"no_draft",
                  "federal_filing_deadline": "YYYY-MM-DD",
                  "state_filing_deadline": "YYYY-MM-DD",
                  "last_specialist_run": {...} | null,
                  "tax_review_queue_summary": {"pending":..,"reviewed":..,"resolved":..}
                },
                ...
              ]
            },
            ...
          ]
        }

    Role floor: viewer.
    """
    workspace = await authorize_workspace(workspace_id, None, min_role="viewer")
    from app.api.tax_status import get_tax_status as _http_get_tax_status
    return await _http_get_tax_status(
        workspace_id=workspace_id,
        as_of=as_of,
        workspace=workspace,
    )
