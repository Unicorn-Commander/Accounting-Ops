"""MCP tool: flag_for_cpa_review — surface a flagged tax position for CPA review.

Used by the 1120 Preparer specialist to flag judgment calls that require CPA
attention before the position can be included in the return. Typical uses:

  - Line 26 splits: a category with multiple possible placements (e.g. SaaS · AI
    costs that might be COGS vs. Other deductions)
  - §163(j) interest limitation analysis
  - Related-party / inter-entity characterization
  - The 4 CPA-flagged categories from tax_mapping_1120.py (low-confidence
    mappings where alternative placements exist)
  - Contractor payments that may require 1099-NEC supplemental filing

Inserts a ``flagged_position`` row into ``tax_review_queue``.

Write tool — requires accountant role.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.workspace_session import workspace_session

_VALID_CONFIDENCE = frozenset({"low", "medium", "high"})


async def flag_for_cpa_review(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    form_reference: str,
    content: dict[str, Any],
    confidence: str,
    citations: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Flag a tax position or judgment call for CPA review.

    Inserts a ``flagged_position`` row into ``tax_review_queue``. Used by the
    specialist for line-level issues that can't be resolved mechanically.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Entity UUID.
        tax_year: Tax year (e.g. 2023).
        form_reference: Form/line reference, e.g. '1120-line-26', '1125-A-line-4',
            '1120-line-18'. Required for flagged positions.
        content: Structured flag payload. Recommended keys:
            - ``issue`` (str): What the flag is about.
            - ``affected_line`` (str): The 1120 line number (e.g. "26").
            - ``context`` (str): Relevant facts (amounts, categories, date range).
            - ``recommendation`` (str): Specialist's suggested approach for the CPA.
            Example:
              {"issue": "§163(j) limitation may cap interest deduction.",
               "affected_line": "18",
               "context": "Entity has $48,000 in Banking · Interest expense
                            for 2023. §163(j) limits business interest expense
                            to 30% of ATI for entities above the small business
                            exception threshold.",
               "recommendation": "CPA should verify whether the §163(j)
                                   small business exemption applies and compute
                                   the limitation if not."}
        confidence: Specialist's confidence that this IS an issue:
            'low' | 'medium' | 'high'. Use 'high' when the issue is
            definitively present; 'low' when it may not apply.
        citations: Optional list of citations, each
            ``{"type": "section"|"journal_entry"|"form_line", "value": "..."}``.

    Returns:
        {"ok": True, "item_id": "<uuid>", "item_type": "flagged_position",
         "form_reference": "...", "_draft_notice": "..."}

    DRAFT — for CPA review only.

    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")

    confidence = (confidence or "medium").strip().lower()
    if confidence not in _VALID_CONFIDENCE:
        raise ValueError(
            f"confidence must be 'low', 'medium', or 'high'; got {confidence!r}"
        )

    if not form_reference or not str(form_reference).strip():
        raise ValueError(
            "form_reference is required for flagged_position items "
            "(e.g. '1120-line-26', '1120-line-18')"
        )

    auth = current_auth()
    submitter_id = (auth.agent_id or auth.uc_uid) if auth else "unknown"
    submitter_kind = "agent" if (auth and auth.agent_id) else "human"

    citations_val = citations or []

    import json

    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO tax_review_queue (
                        workspace_id, entity_id, tax_year,
                        item_type, form_reference,
                        content, confidence, citations,
                        status, submitter_kind, submitter_id
                    ) VALUES (
                        :workspace_id, :entity_id, :tax_year,
                        'flagged_position', :form_reference,
                        :content::jsonb, :confidence, :citations::jsonb,
                        'pending', :submitter_kind, :submitter_id
                    )
                    RETURNING id::text AS id
                    """
                ),
                {
                    "workspace_id": str(workspace_id),
                    "entity_id": str(entity_id),
                    "tax_year": tax_year,
                    "form_reference": str(form_reference).strip(),
                    "content": json.dumps(content),
                    "confidence": confidence,
                    "citations": json.dumps(citations_val),
                    "submitter_kind": submitter_kind,
                    "submitter_id": submitter_id,
                },
            )
        ).mappings().first()

    return {
        "ok": True,
        "item_id": row["id"],
        "item_type": "flagged_position",
        "form_reference": str(form_reference).strip(),
        "tax_year": tax_year,
        "entity_id": str(entity_id),
        "_draft_notice": (
            "DRAFT — for CPA review only. "
            "This flagged position was identified by the 1120 Preparer specialist. "
            "No tax position has been established. "
            "CPA must review and resolve before inclusion in the return."
        ),
    }
