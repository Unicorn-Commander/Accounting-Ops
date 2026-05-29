"""Multi-year / multi-entity tax status dashboard endpoint.

GET /api/workspaces/{wid}/tax-status?as_of=YYYY-MM-DD

Aggregates entity + approval_queue + tax_review_queue + specialist_run into a
single per-entity-per-year status matrix for the dashboard landing page.

Role floor: viewer (same as tax-review-queue list).

Filing deadline logic:
  April 15 of (tax_year + 1), advanced to next Monday if it falls on Saturday
  or Sunday. State deadline mirrors federal for now.

Filing status derivation (per entity-year, in priority order):
  1. 'overdue'    — today > deadline AND filing_status not 'filed'
  2. 'cpa_review' — queue has items AND pending_count == 0
  3. 'draft'      — queue has items AND pending_count > 0
  4. 'no_draft'   — queue is empty

Specialist run table: gracefully absent — any ProgrammingError querying
specialist_run is caught and treated as an empty result set.

Books status:
  'complete'    — approval_queue has no pending items for this entity-year
  'in_progress' — approval_queue has pending items
  'no_data'     — approval_queue has zero rows for this entity-year

Short-year detection:
  Entity incorporated_at is stored on the entity row (incorporated_at column).
  If incorporated_at > Jan 1 of the tax_year, that year is a short year.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["tax-status"])

TAX_YEARS = [2022, 2023, 2024, 2025]


# ---------------------------------------------------------------------------
# Deadline helpers
# ---------------------------------------------------------------------------


def _filing_deadline(tax_year: int) -> dt.date:
    """Return the federal + SC filing deadline for a C-corp tax year.

    Standard: April 15, year+1. Advance to next Monday if that falls on
    Saturday or Sunday (IRS / SCDOR practice mirrors federal).
    """
    d = dt.date(tax_year + 1, 4, 15)
    # 5 = Saturday, 6 = Sunday
    if d.weekday() == 5:   # Saturday → Monday (+2)
        d += dt.timedelta(days=2)
    elif d.weekday() == 6:  # Sunday → Monday (+1)
        d += dt.timedelta(days=1)
    return d


def _date_or_none(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        try:
            return dt.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Per-entity-year aggregation
# ---------------------------------------------------------------------------


async def _queue_summary(
    session: Any,
    entity_id: str,
    tax_year: int,
) -> dict[str, int]:
    """Count tax_review_queue rows by status for one entity-year."""
    rows = (
        await session.execute(
            text(
                """
                SELECT status, count(*)::int AS cnt
                FROM tax_review_queue
                WHERE entity_id = :eid AND tax_year = :yr
                GROUP BY status
                """
            ),
            {"eid": entity_id, "yr": tax_year},
        )
    ).mappings().all()
    summary = {"pending": 0, "reviewed": 0, "resolved": 0, "rejected": 0}
    for r in rows:
        s = r["status"]
        if s in summary:
            summary[s] = r["cnt"]
    return summary


async def _approval_summary(
    session: Any,
    entity_id: str,
    tax_year: int,
) -> dict[str, Any]:
    """Return pending count + total for the approval_queue for one entity-year.

    approval_queue has ``created_at`` but no ``period`` column (as of schema
    today).  The earlier COALESCE(period, created_at) form was the cause of
    /api/tax-status's 500: when ``period`` is missing the asyncpg statement
    fails AND aborts the surrounding transaction, so the very next call
    (``_queue_summary``) bombs with ``InFailedSQLTransactionError``.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT
                    count(*) FILTER (WHERE status = 'pending')::int  AS pending,
                    count(*)::int                                      AS total
                FROM approval_queue
                WHERE entity_id = :eid
                  AND EXTRACT(YEAR FROM created_at) = :yr
                """
            ),
            {"eid": entity_id, "yr": tax_year},
        )
    ).mappings().first()
    if row:
        return {"pending": row["pending"] or 0, "total": row["total"] or 0}
    return {"pending": 0, "total": 0}


async def _last_specialist_run(
    session: Any,
    entity_id: str,
    tax_year: int,
) -> dict | None:
    """Return the most recent specialist_run for one entity-year, or None.

    Gracefully returns None if the specialist_run table does not exist.
    """
    try:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text            AS specialist_id,
                        specialist_id       AS specialist_name,
                        started_at,
                        status
                    FROM specialist_run
                    WHERE entity_id = :eid AND tax_year = :yr
                    ORDER BY started_at DESC
                    LIMIT 1
                    """
                ),
                {"eid": entity_id, "yr": tax_year},
            )
        ).mappings().first()
        if row is None:
            return None
        return {
            "specialist_id": row["specialist_name"],
            "started_at": row["started_at"].isoformat()
            if hasattr(row["started_at"], "isoformat")
            else str(row["started_at"]),
            "status": row["status"],
        }
    except (ProgrammingError, Exception):  # noqa: BLE001
        return None


def _filing_status(
    queue_summary: dict[str, int],
    deadline: dt.date,
    today: dt.date,
) -> str:
    """Derive filing_status from queue counts + deadline."""
    total = sum(queue_summary.values())
    pending = queue_summary.get("pending", 0)

    if total == 0:
        if today > deadline:
            return "overdue"
        return "no_draft"

    # Has queue items
    if today > deadline and pending > 0:
        return "overdue"
    if pending == 0:
        return "cpa_review"
    return "draft"


def _books_status(approval: dict[str, int]) -> str:
    if approval["total"] == 0:
        return "no_data"
    if approval["pending"] > 0:
        return "in_progress"
    return "complete"


# ---------------------------------------------------------------------------
# Main endpoint
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/tax-status")
async def get_tax_status(
    workspace_id: UUID,
    as_of: str | None = Query(None, description="YYYY-MM-DD; defaults to today"),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Return per-entity-per-year tax status matrix for the dashboard.

    Viewer floor. Aggregates entity + approval_queue + tax_review_queue +
    specialist_run into one response.

    Entity-scope: unscoped members (admin/owner) see all entities; scoped
    members see only their assigned entities. Mirrors tax_review.py.
    """
    require_role(workspace, "viewer")

    # as_of date
    today: dt.date
    if as_of:
        try:
            today = dt.date.fromisoformat(as_of)
        except ValueError:
            today = dt.date.today()
    else:
        today = dt.date.today()

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # ----------------------------------------------------------------
        # 1. Fetch all entities for this workspace
        # ----------------------------------------------------------------
        entity_rows = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text,
                        slug,
                        display_name,
                        legal_name,
                        kind                  AS entity_type,
                        country_code,
                        -- entity.incorporated_at was planned but the column
                        -- doesn't exist yet on either tenant schema.  Until a
                        -- migration adds it, the short-year detection below
                        -- treats every year as a full year (the safe default).
                        NULL::date            AS incorporated_at,
                        status
                    FROM entity
                    ORDER BY display_name
                    """
                )
            )
        ).mappings().all()

        # Scope restriction: if the caller is scoped, filter by their entities.
        # We do this in Python (the entity list is small) rather than adding
        # a parameterised WHERE clause that varies per call.
        from app.authz import assert_entity_in_scope  # noqa: PLC0415
        scope_restriction = assert_entity_in_scope(workspace, None)
        if scope_restriction is not None:
            scoped_ids = {str(e) for e in scope_restriction}
            entity_rows = [r for r in entity_rows if r["id"] in scoped_ids]

        # ----------------------------------------------------------------
        # 2. Build per-entity output
        # ----------------------------------------------------------------
        entities_out: list[dict] = []

        for ent in entity_rows:
            entity_id = ent["id"]
            incorporated_at = _date_or_none(ent.get("incorporated_at"))

            # Determine jurisdictions from country_code + kind
            country = (ent.get("country_code") or "US").upper()
            # For Magic Unicorn Inc (SC C-corp), expose both US + SC.
            # General rule: US entities get [US]; SC-registered get [US, SC].
            # We check entity slug/legal_name for SC indicator — this is a
            # heuristic until jurisdiction is a proper column.
            slug = (ent.get("slug") or "").lower()
            legal = (ent.get("legal_name") or "").lower()
            entity_type = (ent.get("entity_type") or "").lower()
            # Expose SC jurisdiction for the known SC entities
            is_sc = (
                "magic-unicorn" in slug
                or "magic unicorn" in legal
                or "sc" in slug
            )
            jurisdictions = [country, "SC"] if (country == "US" and is_sc) else [country]

            years_out: list[dict] = []
            for tax_year in TAX_YEARS:
                # Skip years before incorporation
                if incorporated_at and incorporated_at.year > tax_year:
                    continue

                is_short_year = bool(
                    incorporated_at
                    and incorporated_at.year == tax_year
                    and incorporated_at > dt.date(tax_year, 1, 1)
                )
                short_year_start: str | None = (
                    incorporated_at.isoformat() if is_short_year else None
                )

                deadline = _filing_deadline(tax_year)
                deadline_str = deadline.isoformat()

                # Queue summary (tax_review_queue)
                q_summary = await _queue_summary(session, entity_id, tax_year)

                # Approval summary (books status)
                appr = await _approval_summary(session, entity_id, tax_year)

                # Last specialist run
                last_run = await _last_specialist_run(session, entity_id, tax_year)

                # Derived statuses
                f_status = _filing_status(q_summary, deadline, today)
                b_status = _books_status(appr)

                years_out.append(
                    {
                        "tax_year": tax_year,
                        "is_short_year": is_short_year,
                        "short_year_start": short_year_start,
                        "books_status": b_status,
                        "pending_approvals": appr["pending"],
                        "filing_status": f_status,
                        "federal_filing_deadline": deadline_str,
                        "state_filing_deadline": deadline_str,
                        "last_specialist_run": last_run,
                        "tax_review_queue_summary": {
                            "pending": q_summary.get("pending", 0),
                            "reviewed": q_summary.get("reviewed", 0),
                            "resolved": q_summary.get("resolved", 0),
                        },
                    }
                )

            entities_out.append(
                {
                    "entity_id": entity_id,
                    "entity_slug": ent.get("slug"),
                    "display_name": ent.get("display_name"),
                    "entity_type": entity_type,
                    "jurisdictions": jurisdictions,
                    "incorporated_at": (
                        incorporated_at.isoformat() if incorporated_at else None
                    ),
                    "years": years_out,
                }
            )

    return {
        "as_of": today.isoformat(),
        "entities": entities_out,
    }
