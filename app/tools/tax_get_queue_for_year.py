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
from app.workspace_session import workspace_session

_MAX_ITEMS = 500  # safety cap; 55-160 items typical per entity-year

# Per-string-field content cap. A handful of specialist memos run 8-11KB; without
# a bound, an entity-year's full queue (esp. once prior cpa_narrative memos
# accumulate) can serialize to ~880KB / ~220K tokens and overflow even a 200K
# frontier context. Head-truncate long fields (the memos lead with their TL;DR).
_MAX_CONTENT_CHARS = 6000


def _cap_str(s: str, n: int = _MAX_CONTENT_CHARS) -> str:
    return s if len(s) <= n else s[:n] + " …[truncated]"


def _cap_content(content: Any, n: int = _MAX_CONTENT_CHARS) -> Any:
    """Bound a queue item's content without breaking its shape: truncate long
    string values in a dict (or a bare string), leave non-strings untouched."""
    if isinstance(content, dict):
        return {k: (_cap_str(v, n) if isinstance(v, str) else v) for k, v in content.items()}
    if isinstance(content, str):
        return _cap_str(content, n)
    return content


def _is_cpa_narrative(item: dict) -> bool:
    """A prior CPA-narrative memo = this agent's OWN past output, not source data."""
    c = item.get("content")
    return (
        item.get("item_type") == "memo"
        and isinstance(c, dict)
        and str(c.get("topic", "")).startswith("cpa_narrative_")
    )


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
          "by_form_reference": {"<form_ref>": [item_id, ...]},  # ID index into by_status
          "escalations": [item_id, ...],     # ids of status=pending items (TO-DOs)
          "cpa_narratives": [{"id", "topic", "status", "created_at"}],  # refs only, no content
          "_meta": {...truncation/exclusion notes...},
          "_draft_notice": "...",
        }

    Full item content lives ONCE in ``by_status``; ``by_form_reference`` and
    ``escalations`` are id indexes into it (resolve ids against by_status).
    Prior cpa_narrative_* memos are excluded from the working set and returned
    as lightweight references, and long content fields are head-truncated — this
    keeps the payload within the model context window on mature entities.

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

    all_items = [dict(r) for r in rows]

    # Split the agent's OWN prior outputs (cpa_narrative_* memos) out of the
    # working set. They are this agent's past memos — not source data — and
    # inlining their full text (5-40KB each, ACCUMULATING every run) is the
    # unbounded-growth source that overflowed the context window on mature
    # entities. Return them as lightweight references only.
    narrative_refs = [
        {
            "id": it["id"],
            "topic": it["content"].get("topic") if isinstance(it.get("content"), dict) else None,
            "status": it["status"],
            "created_at": it["created_at"],
        }
        for it in all_items
        if _is_cpa_narrative(it)
    ]
    items = [it for it in all_items if not _is_cpa_narrative(it)]

    # Bound per-item content so a few very large specialist memos can't blow the
    # context budget (head-truncate long string fields; memos lead with TL;DRs).
    for it in items:
        it["content"] = _cap_content(it.get("content"))

    # Group by status — full items live HERE, once. This is the single source of
    # full item content.
    by_status: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        by_status[item["status"]].append(item)

    # by_form_reference and escalations are ID INDEXES into by_status, NOT second
    # full copies — duplicating full content across all three groupings is what
    # tripled the payload (~880KB). Resolve an id's content from by_status.
    by_form: dict[str, list[str]] = defaultdict(list)
    for item in items:
        by_form[item["form_reference"] or "general"].append(item["id"])

    # Escalations = pending item ids (the narrative agent treats these as TO-DOs).
    escalations = [item["id"] for item in by_status.get("pending", [])]

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
        # Reference-only (id/topic/status/created_at) — prior narratives are NOT
        # inlined; fetch a specific one with get_tax_narrative if needed.
        "cpa_narratives": narrative_refs,
        "_meta": {
            "working_items": len(items),
            "prior_cpa_narratives_excluded": len(narrative_refs),
            "content_field_truncated_to_chars": _MAX_CONTENT_CHARS,
            "note": (
                "Prior cpa_narrative_* memos are listed as references only (not "
                "inlined) and long content fields are head-truncated, to keep the "
                "payload within the model context window."
            ),
        },
        "_draft_notice": (
            "DRAFT — for CPA review only. "
            "This queue data was generated by the Accounting-Ops specialist agents "
            "and has NOT been reviewed or approved by a licensed tax professional."
        ),
    }
