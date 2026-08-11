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

from agents.sentinel.checks._helpers import (
    federal_1120_deadline,
    federal_return_deadline,
)
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["tax-status"])

TAX_YEARS = [2022, 2023, 2024, 2025]


# ---------------------------------------------------------------------------
# Deadline helpers
# ---------------------------------------------------------------------------


def _filing_deadline(tax_year: int, kind: str = "c_corp") -> dt.date:
    """Return the federal + SC filing deadline for this entity kind's tax year.

    1120 and 1040 filers are due the 15th day of the 4th month (April 15);
    flow-throughs filing 1120-S or 1065 are due the 15th day of the 3rd month
    (March 15). Advance to next Monday if that falls on a weekend (IRS / SCDOR
    practice mirrors federal). Kinds we prepare no return for fall back to
    April 15 so the dashboard still renders a sane date.
    """
    return federal_return_deadline(tax_year, kind) or federal_1120_deadline(tax_year)


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


# ---------------------------------------------------------------------------
# Audit 2026-05-31 Bug 12 — grouped aggregation across (entity_ids × years)
# ---------------------------------------------------------------------------
# The original implementation ran THREE awaited queries (queue summary, approval
# summary, latest specialist_run) per entity × per tax_year. At 50 entities × 5
# years that's 750 round-trips. These helpers run ONE query per data source over
# all visible entity_ids + the fixed TAX_YEARS set, returning a nested dict
# keyed (entity_id, tax_year) → metric. Total round-trips per request: 3.


async def _bulk_queue_summary(
    session: Any, entity_ids: list[str], tax_years: list[int]
) -> dict[tuple[str, int], dict[str, int]]:
    """One SQL pass that counts tax_review_queue rows by status across every
    (entity, year) the caller can see. Returns a dict keyed (entity, year)
    that includes a zero-filled summary for every cell the caller asked about
    (so the in-memory assemble step never has to check key existence)."""
    summary_zero = {"pending": 0, "reviewed": 0, "resolved": 0, "rejected": 0}
    out: dict[tuple[str, int], dict[str, int]] = {
        (eid, yr): dict(summary_zero) for eid in entity_ids for yr in tax_years
    }
    if not entity_ids or not tax_years:
        return out
    rows = (
        await session.execute(
            text(
                """
                SELECT entity_id::text AS entity_id,
                       tax_year,
                       status,
                       count(*)::int AS cnt
                FROM tax_review_queue
                WHERE entity_id = ANY(CAST(:eids AS uuid[]))
                  AND tax_year = ANY(CAST(:yrs AS int[]))
                GROUP BY entity_id, tax_year, status
                """
            ),
            {"eids": entity_ids, "yrs": tax_years},
        )
    ).mappings().all()
    for r in rows:
        key = (r["entity_id"], int(r["tax_year"]))
        if key not in out:
            continue
        st = r["status"]
        if st in out[key]:
            out[key][st] = int(r["cnt"])
    return out


async def _bulk_approval_summary(
    session: Any, entity_ids: list[str], tax_years: list[int]
) -> dict[tuple[str, int], dict[str, int]]:
    """One SQL pass that returns (pending, total) approval_queue counts for
    every (entity, year). Year derivation matches the per-call version
    (EXTRACT(YEAR FROM created_at))."""
    out: dict[tuple[str, int], dict[str, int]] = {
        (eid, yr): {"pending": 0, "total": 0}
        for eid in entity_ids
        for yr in tax_years
    }
    if not entity_ids or not tax_years:
        return out
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    entity_id::text                                    AS entity_id,
                    EXTRACT(YEAR FROM created_at)::int                 AS yr,
                    count(*) FILTER (WHERE status = 'pending')::int    AS pending,
                    count(*)::int                                      AS total
                FROM approval_queue
                WHERE entity_id = ANY(CAST(:eids AS uuid[]))
                  AND EXTRACT(YEAR FROM created_at) = ANY(CAST(:yrs AS int[]))
                GROUP BY entity_id, EXTRACT(YEAR FROM created_at)
                """
            ),
            {"eids": entity_ids, "yrs": tax_years},
        )
    ).mappings().all()
    for r in rows:
        key = (r["entity_id"], int(r["yr"]))
        if key not in out:
            continue
        out[key] = {"pending": int(r["pending"] or 0), "total": int(r["total"] or 0)}
    return out


async def _bulk_journal_summary(
    session: Any, entity_ids: list[str], tax_years: list[int]
) -> dict[tuple[str, int], int]:
    """One SQL pass returning the POSTED journal-entry count per (entity, year).

    Keyed on ``effective_date`` — the date the transaction actually belongs to
    — not on when the row was created. This is what ``books_status`` reads.

    Reversal/recategorization sources are counted: they are real postings, and
    the question here is only "does this year have books", not "what do they
    total".
    """
    out: dict[tuple[str, int], int] = {
        (eid, yr): 0 for eid in entity_ids for yr in tax_years
    }
    if not entity_ids or not tax_years:
        return out
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    entity_id::text                        AS entity_id,
                    EXTRACT(YEAR FROM effective_date)::int AS yr,
                    count(*)::int                          AS cnt
                FROM journal_entry
                WHERE entity_id = ANY(CAST(:eids AS uuid[]))
                  AND EXTRACT(YEAR FROM effective_date) = ANY(CAST(:yrs AS int[]))
                  AND status = 'posted'
                GROUP BY entity_id, EXTRACT(YEAR FROM effective_date)
                """
            ),
            {"eids": entity_ids, "yrs": tax_years},
        )
    ).mappings().all()
    for r in rows:
        key = (r["entity_id"], int(r["yr"]))
        if key in out:
            out[key] = int(r["cnt"] or 0)
    return out


async def _bulk_last_specialist_run(
    session: Any, entity_ids: list[str], tax_years: list[int]
) -> dict[tuple[str, int], dict | None]:
    """One SQL pass that returns the LATEST specialist_run per (entity, year).

    Uses DISTINCT ON to pick the freshest row in a single scan. Gracefully
    falls back to ``None`` everywhere if the specialist_run table doesn't
    exist (caught the same way the per-call helper does).
    """
    out: dict[tuple[str, int], dict | None] = {
        (eid, yr): None for eid in entity_ids for yr in tax_years
    }
    if not entity_ids or not tax_years:
        return out
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT ON (entity_id, tax_year)
                        entity_id::text   AS entity_id,
                        tax_year,
                        specialist_id     AS specialist_name,
                        started_at,
                        status
                    FROM specialist_run
                    WHERE entity_id = ANY(CAST(:eids AS uuid[]))
                      AND tax_year = ANY(CAST(:yrs AS int[]))
                    ORDER BY entity_id, tax_year, started_at DESC
                    """
                ),
                {"eids": entity_ids, "yrs": tax_years},
            )
        ).mappings().all()
        for r in rows:
            key = (r["entity_id"], int(r["tax_year"]))
            if key not in out:
                continue
            started = r["started_at"]
            out[key] = {
                "specialist_id": r["specialist_name"],
                "started_at": started.isoformat()
                if hasattr(started, "isoformat")
                else str(started),
                "status": r["status"],
            }
    except (ProgrammingError, Exception):  # noqa: BLE001
        # Table missing or any other failure: return the zero-filled defaults.
        pass
    return out


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


def _books_status(approval: dict[str, int], journal: int) -> str:
    """Whether the books for this entity-year hold anything, and whether the
    intake queue is drained.

    ``journal`` is the count of POSTED journal entries whose effective_date
    falls in the tax year — i.e. the actual books.

    This used to be derived from approval-queue counts ALONE, which produced a
    flatly false answer in the ordinary case.  Two compounding reasons:

      1. An approval row is consumed when it is decided, so a fully reconciled
         year ends with an empty queue and reported ``no_data`` — exactly
         backwards.
      2. ``_bulk_approval_summary`` buckets by ``EXTRACT(YEAR FROM
         created_at)``, the row's INGEST date, not the transaction date.  Magic
         Unicorn Inc's 827 bank imports were all ingested on 2026-05-28, so
         every year from 2022 to 2025 counted zero approvals no matter what
         the books contained.

    Net effect: Magic Unicorn Inc TY2023 reported ``no_data`` while holding 438
    posted journal entries, and every reader — human and agent — concluded the
    books were empty. The journal is the source of truth for "is there data".
    """
    if journal > 0:
        return "in_progress" if approval["pending"] > 0 else "complete"
    # Nothing posted yet. Rows waiting in the queue still mean work in flight.
    if approval["total"] > 0:
        return "in_progress"
    return "no_data"


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
                        -- entity.incorporated_at DOES exist and IS populated
                        -- (Magic Unicorn Inc = 2022-05-11).  This used to be
                        -- hardcoded `NULL::date` behind a comment claiming the
                        -- column was never added, which meant short-year
                        -- detection could never fire and the QSBS §1202
                        -- milestone was fed a null — while Sentinel's own
                        -- checks (agents/sentinel/checks/_helpers.py) read the
                        -- real column and saw a different answer.
                        incorporated_at,
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
        # 2. Bulk-fetch all (entity × year) aggregates in 3 queries total
        #    (audit 2026-05-31 Bug 12). Was N*Y*3; now 3.
        # ----------------------------------------------------------------
        visible_entity_ids = [r["id"] for r in entity_rows]
        bulk_queue = await _bulk_queue_summary(session, visible_entity_ids, TAX_YEARS)
        bulk_approval = await _bulk_approval_summary(session, visible_entity_ids, TAX_YEARS)
        bulk_journal = await _bulk_journal_summary(session, visible_entity_ids, TAX_YEARS)
        bulk_specialist = await _bulk_last_specialist_run(
            session, visible_entity_ids, TAX_YEARS
        )

        # ----------------------------------------------------------------
        # 3. Build per-entity output from the bulk caches (no per-cell awaits)
        # ----------------------------------------------------------------
        entities_out: list[dict] = []

        for ent in entity_rows:
            entity_id = ent["id"]
            incorporated_at = _date_or_none(ent.get("incorporated_at"))

            # Determine jurisdictions from country_code + kind
            country = (ent.get("country_code") or "US").upper()
            # For an SC-registered C-corp, expose both US + SC.
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

                deadline = _filing_deadline(tax_year, entity_type)
                deadline_str = deadline.isoformat()

                key = (entity_id, tax_year)
                q_summary = bulk_queue.get(
                    key,
                    {"pending": 0, "reviewed": 0, "resolved": 0, "rejected": 0},
                )
                appr = bulk_approval.get(key, {"pending": 0, "total": 0})
                last_run = bulk_specialist.get(key)

                # Derived statuses
                f_status = _filing_status(q_summary, deadline, today)
                b_status = _books_status(appr, bulk_journal.get(key, 0))

                years_out.append(
                    {
                        "tax_year": tax_year,
                        "is_short_year": is_short_year,
                        "short_year_start": short_year_start,
                        "books_status": b_status,
                        # The evidence behind books_status, so a reader never
                        # has to take the derived word for it again.
                        "posted_journal_entries": bulk_journal.get(key, 0),
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
