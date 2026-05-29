"""MCP tool: get_tax_review_queue — fetch all tax_review_queue items for an entity-year.

Used by the CPA Narrative Generator to ingest the full queue (resolved, reviewed,
and escalated) for a given entity and tax year, grouped by form_reference so the
narrative agent can write coherent section-level prose.

Returns structured data the narrative agent ingests. All statuses are included by
default so the narrative can distinguish between "items we resolved" (decisions made)
and "items still pending" (TO-DOs for Morgan).

Role floor: viewer (same as the existing GET /tax-review-queue endpoint).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session  # noqa: F811 — used as context manager

_MAX_ITEMS = 500  # safety cap; 55-160 items typical per entity-year


async def get_tax_review_queue(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    include_resolved: bool = True,
) -> dict[str, Any]:
    """Return all tax_review_queue items for (entity, year) grouped by form_reference.

    Fetches the complete queue for the entity-year so the CPA Narrative Generator
    can synthesize decisions made vs. items still requiring Morgan's judgment.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        tax_year: Four-digit tax year (e.g. 2023).
        include_resolved: If True (default), return all statuses (pending,
            reviewed, resolved, rejected). If False, return only pending items.

    Returns:
        {
          "entity_id": str,
          "tax_year": int,
          "total": int,
          "by_status": {
              "pending": [...],
              "reviewed": [...],
              "resolved": [...],
              "rejected": [...],
          },
          "by_form_reference": {
              "<form_ref>": [...items...],
              ...
          },
          "escalations": [...items with status=pending and no resolution...],
          "cpa_narratives": [...items with topic starting with cpa_narrative_...],
          "_draft_notice": "...",
        }

    DRAFT — for CPA review only.

    Role floor: viewer.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")

    status_filter = "1=1" if include_resolved else "trq.status = 'pending'"

    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        trq.id::text,
                        trq.entity_id::text,
                        trq.tax_year,
                        trq.item_type,
                        trq.form_reference,
                        trq.content,
                        trq.confidence,
                        trq.citations,
                        trq.status,
                        trq.submitter_kind,
                        trq.submitter_id,
                        trq.reviewer_uc_uid,
                        trq.resolution_notes,
                        trq.created_at::text,
                        trq.resolved_at::text
                    FROM tax_review_queue trq
                    WHERE trq.entity_id = :entity_id
                      AND trq.tax_year = :tax_year
                      AND {status_filter}
                    ORDER BY trq.created_at ASC, trq.id ASC
                    LIMIT :limit
                    """
                ),
                {
                    "entity_id": str(entity_id),
                    "tax_year": tax_year,
                    "limit": _MAX_ITEMS,
                },
            )
        ).mappings().all()

    items = [dict(r) for r in rows]

    # Group by status
    by_status: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_status[item["status"]].append(item)

    # Group by form_reference (None → "general")
    by_form: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        key = item["form_reference"] or "general"
        by_form[key].append(item)

    # CPA narrative memos (topic starts with "cpa_narrative_")
    cpa_narratives = [
        it for it in items
        if it.get("item_type") == "memo"
        and isinstance(it.get("content"), dict)
        and str(it["content"].get("topic", "")).startswith("cpa_narrative_")
    ]

    # Escalations = pending items (the narrative agent treats these as TO-DOs)
    escalations = by_status.get("pending", [])

    return {
        "entity_id": str(entity_id),
        "tax_year": tax_year,
        "total": len(items),
        "by_status": {
            "pending": by_status.get("pending", []),
            "reviewed": by_status.get("reviewed", []),
            "resolved": by_status.get("resolved", []),
            "rejected": by_status.get("rejected", []),
        },
        "by_form_reference": dict(by_form),
        "escalations": escalations,
        "cpa_narratives": cpa_narratives,
        "_draft_notice": (
            "DRAFT — for CPA review only. "
            "This queue data was generated by the Accounting-Ops specialist agents "
            "and has NOT been reviewed or approved by a licensed tax professional."
        ),
    }
