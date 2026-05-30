"""Tax filing submission tracker.

Records every paper, MyDORWAY, or e-file submission Aaron sends to a taxing
authority. The platform does NOT submit anything programmatically — every row
is created by an explicit user action via these endpoints.

Endpoints
---------
  POST   /api/workspaces/{wid}/tax-filing-submissions
         Record a new submission (form drop, MyDORWAY upload, etc.).

  GET    /api/workspaces/{wid}/tax-filing-submissions
         List submissions, optionally filtered by entity_id + tax_year +
         status (default: not-yet-closed).

  GET    /api/workspaces/{wid}/tax-filing-submissions/{id}
         Detail view for one submission with latest USPS tracking.

  PATCH  /api/workspaces/{wid}/tax-filing-submissions/{id}
         Update tracking number, status, actual response.

  POST   /api/workspaces/{wid}/tax-filing-submissions/{id}/refresh-tracking
         Force a USPS tracking refresh (rate-limited).

  POST   /api/internal/refresh-all-tracking
         Internal cron-triggered batch refresh — hits USPS for every
         submission in 'submitted' or 'in_transit' state with a tracking
         number. Admin role floor; intended for the periodic poller.

Role floors
-----------
  - POST create:                accountant (writes books)
  - GET list / detail:          viewer
  - PATCH update:               accountant
  - POST refresh-tracking:      viewer (refresh is read-only at USPS)
  - POST refresh-all-tracking:  admin (cron/batch path)
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.api.usps_tracking import lookup_tracking
from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(tags=["tax-filing-submissions"])


# ---------------------------------------------------------------------------
# Default expected-response windows (jurisdiction × filing_method) → days
# ---------------------------------------------------------------------------

_DEFAULT_RESPONSE_DAYS: dict[tuple[str, str], int] = {
    ("irs", "paper"):       56,  # 8 weeks for IRS paper-return ack
    ("irs", "efile"):       14,  # rare; we don't e-file, but defensive default
    ("scdor", "paper"):     42,  # 6 weeks for SCDOR paper-return ack
    ("scdor", "mydorway"):  30,  # 4 weeks for MyDORWAY upload ack
    ("other_state", "paper"):    56,
    ("other_state", "mydorway"): 30,
}


def _compute_expected_response_by(
    jurisdiction: str,
    filing_method: str,
    submitted_at: dt.datetime | None,
) -> dt.datetime | None:
    """Compute the expected_response_by timestamp."""
    if not submitted_at:
        return None
    days = _DEFAULT_RESPONSE_DAYS.get((jurisdiction, filing_method))
    if days is None:
        return None
    return submitted_at + dt.timedelta(days=days)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSubmissionBody(BaseModel):
    """Request body for POST /tax-filing-submissions."""
    entity_id: UUID
    jurisdiction: str = Field(
        description="'irs' | 'scdor' | 'other_state'",
    )
    tax_year: int | None = None
    submission_title: str
    forms_enclosed: list[str] = Field(default_factory=list)
    filing_method: str = Field(
        description="'paper' | 'mydorway' | 'efile' | 'fax' | 'eftps'",
    )
    tracking_number: str | None = None
    submitted_at: dt.datetime | None = None
    destination_office: str | None = None
    notes: str | None = None
    # If True, status starts 'submitted'; if False, 'draft' (envelope not yet mailed)
    is_mailed: bool = True


class PatchSubmissionBody(BaseModel):
    """Request body for PATCH /tax-filing-submissions/{id}."""
    tracking_number: str | None = None
    submitted_at: dt.datetime | None = None
    status: str | None = None  # validated against state machine in handler
    actual_response: str | None = None
    notes: str | None = None
    acknowledged_at: dt.datetime | None = None


class SubmissionResponse(BaseModel):
    id: str
    workspace_id: str
    entity_id: str
    entity_display_name: str | None
    jurisdiction: str
    tax_year: int | None
    submission_title: str
    forms_enclosed: list[str]
    filing_method: str
    tracking_number: str | None
    status: str
    submitted_at: str | None
    expected_response_by: str | None
    last_tracking_status: str | None
    last_tracking_at: str | None
    delivered_at: str | None
    acknowledged_at: str | None
    actual_response: str | None
    destination_office: str | None
    notes: str | None
    created_at: str
    updated_at: str


def _row_to_response(row: dict[str, Any]) -> SubmissionResponse:
    """Coerce a DB row dict into a SubmissionResponse."""
    def _iso(v: Any) -> str | None:
        if isinstance(v, dt.datetime):
            return v.isoformat()
        return v

    return SubmissionResponse(
        id=str(row["id"]),
        workspace_id=str(row["workspace_id"]),
        entity_id=str(row["entity_id"]),
        entity_display_name=row.get("entity_display_name"),
        jurisdiction=row["jurisdiction"],
        tax_year=row.get("tax_year"),
        submission_title=row["submission_title"],
        forms_enclosed=row.get("forms_enclosed") or [],
        filing_method=row["filing_method"],
        tracking_number=row.get("tracking_number"),
        status=row["status"],
        submitted_at=_iso(row.get("submitted_at")),
        expected_response_by=_iso(row.get("expected_response_by")),
        last_tracking_status=row.get("last_tracking_status"),
        last_tracking_at=_iso(row.get("last_tracking_at")),
        delivered_at=_iso(row.get("delivered_at")),
        acknowledged_at=_iso(row.get("acknowledged_at")),
        actual_response=row.get("actual_response"),
        destination_office=row.get("destination_office"),
        notes=row.get("notes"),
        created_at=_iso(row.get("created_at")) or "",
        updated_at=_iso(row.get("updated_at")) or "",
    )


# ---------------------------------------------------------------------------
# POST — create
# ---------------------------------------------------------------------------

@router.post("/api/workspaces/{workspace_id}/tax-filing-submissions")
async def create_submission(
    workspace_id: UUID,
    body: CreateSubmissionBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SubmissionResponse:
    """Record a new tax submission.

    The caller MUST have already physically submitted the package (envelope at
    USPS, MyDORWAY upload, e-file submit). This endpoint does not send
    anything — it only records the fact of submission for tracking + deadline
    bookkeeping.
    """
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, body.entity_id)

    # Validate jurisdiction + filing_method
    if body.jurisdiction not in ("irs", "scdor", "other_state"):
        raise HTTPException(
            status_code=400,
            detail=f"invalid jurisdiction '{body.jurisdiction}'",
        )
    if body.filing_method not in ("paper", "mydorway", "efile", "fax", "eftps"):
        raise HTTPException(
            status_code=400,
            detail=f"invalid filing_method '{body.filing_method}'",
        )

    submitted_at = body.submitted_at
    if body.is_mailed and not submitted_at:
        submitted_at = dt.datetime.now(tz=dt.timezone.utc)

    expected_response_by = _compute_expected_response_by(
        body.jurisdiction, body.filing_method, submitted_at
    )

    initial_status = "submitted" if body.is_mailed else "draft"

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO tax_filing_submission (
                        workspace_id, entity_id, jurisdiction, tax_year,
                        submission_title, forms_enclosed, filing_method,
                        tracking_number, status, submitted_at,
                        expected_response_by, destination_office, notes,
                        submitter_uc_uid
                    ) VALUES (
                        :wid, :eid, :juris, :ty,
                        :title, :forms, :method,
                        :track, :status, :sat,
                        :erb, :dest, :notes, :uc_uid
                    )
                    RETURNING
                        id, workspace_id, entity_id, jurisdiction, tax_year,
                        submission_title, forms_enclosed, filing_method,
                        tracking_number, status, submitted_at,
                        expected_response_by, last_tracking_status,
                        last_tracking_at, delivered_at, acknowledged_at,
                        actual_response, destination_office, notes,
                        created_at, updated_at
                    """
                ),
                {
                    "wid": str(workspace_id),
                    "eid": str(body.entity_id),
                    "juris": body.jurisdiction,
                    "ty": body.tax_year,
                    "title": body.submission_title,
                    "forms": body.forms_enclosed,
                    "method": body.filing_method,
                    "track": body.tracking_number,
                    "status": initial_status,
                    "sat": submitted_at,
                    "erb": expected_response_by,
                    "dest": body.destination_office,
                    "notes": body.notes,
                    "uc_uid": workspace.user_uc_uid,
                },
            )
        ).mappings().first()

        # Pull entity display name
        ent = (
            await session.execute(
                text("SELECT display_name FROM entity WHERE id = :eid"),
                {"eid": str(body.entity_id)},
            )
        ).mappings().first()
        row_dict = dict(row)
        row_dict["entity_display_name"] = ent["display_name"] if ent else None

    return _row_to_response(row_dict)


# ---------------------------------------------------------------------------
# GET — list
# ---------------------------------------------------------------------------

class SubmissionListResponse(BaseModel):
    submissions: list[SubmissionResponse]
    counts: dict[str, int]


@router.get("/api/workspaces/{workspace_id}/tax-filing-submissions")
async def list_submissions(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    tax_year: int | None = None,
    jurisdiction: str | None = None,
    status: str | None = Query(
        None,
        description=(
            "Filter by status. Default: returns all non-'closed' rows. "
            "Pass 'all' for everything. Pass a specific status to filter."
        ),
    ),
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SubmissionListResponse:
    """List submissions for a workspace, optionally filtered."""
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    where = ["1=1"]
    params: dict[str, Any] = {"limit": min(limit, 500), "offset": offset}

    if entity_id:
        where.append("tfs.entity_id = :eid")
        params["eid"] = str(entity_id)
    if scope_restriction is not None:
        where.append("tfs.entity_id = ANY(:entity_scope)")
        params["entity_scope"] = [str(e) for e in scope_restriction]
    if tax_year is not None:
        where.append("tfs.tax_year = :ty")
        params["ty"] = tax_year
    if jurisdiction:
        where.append("tfs.jurisdiction = :juris")
        params["juris"] = jurisdiction
    if status:
        if status != "all":
            where.append("tfs.status = :status")
            params["status"] = status
    else:
        where.append("tfs.status <> 'closed'")

    where_sql = " AND ".join(where)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        tfs.id, tfs.workspace_id, tfs.entity_id,
                        e.display_name AS entity_display_name,
                        tfs.jurisdiction, tfs.tax_year,
                        tfs.submission_title, tfs.forms_enclosed,
                        tfs.filing_method, tfs.tracking_number, tfs.status,
                        tfs.submitted_at, tfs.expected_response_by,
                        tfs.last_tracking_status, tfs.last_tracking_at,
                        tfs.delivered_at, tfs.acknowledged_at,
                        tfs.actual_response, tfs.destination_office,
                        tfs.notes, tfs.created_at, tfs.updated_at
                    FROM tax_filing_submission tfs
                    LEFT JOIN entity e ON e.id = tfs.entity_id
                    WHERE {where_sql}
                    ORDER BY
                        CASE tfs.status
                            WHEN 'awaiting_followup' THEN 0
                            WHEN 'in_transit'        THEN 1
                            WHEN 'submitted'         THEN 2
                            WHEN 'delivered'         THEN 3
                            WHEN 'acknowledged'      THEN 4
                            WHEN 'draft'             THEN 5
                            WHEN 'closed'            THEN 6
                            ELSE 7
                        END,
                        tfs.expected_response_by ASC NULLS LAST,
                        tfs.submitted_at DESC NULLS LAST
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

        # Counts by status (always over the full filtered set, ignoring limit)
        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}
        count_rows = (
            await session.execute(
                text(
                    f"""
                    SELECT status, count(*) AS c
                    FROM tax_filing_submission tfs
                    WHERE {where_sql.replace(' AND tfs.status', ' AND TRUE -- tfs.status', 1) if False else where_sql}
                    GROUP BY status
                    """
                ),
                count_params,
            )
        ).mappings().all()

    counts = {r["status"]: int(r["c"]) for r in count_rows}
    return SubmissionListResponse(
        submissions=[_row_to_response(dict(r)) for r in rows],
        counts=counts,
    )


# ---------------------------------------------------------------------------
# GET — detail (with live tracking refresh)
# ---------------------------------------------------------------------------

@router.get("/api/workspaces/{workspace_id}/tax-filing-submissions/{submission_id}")
async def get_submission(
    workspace_id: UUID,
    submission_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SubmissionResponse:
    """Fetch detail for one submission. Does NOT auto-refresh tracking."""
    require_role(workspace, "viewer")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        tfs.id, tfs.workspace_id, tfs.entity_id,
                        e.display_name AS entity_display_name,
                        tfs.jurisdiction, tfs.tax_year,
                        tfs.submission_title, tfs.forms_enclosed,
                        tfs.filing_method, tfs.tracking_number, tfs.status,
                        tfs.submitted_at, tfs.expected_response_by,
                        tfs.last_tracking_status, tfs.last_tracking_at,
                        tfs.delivered_at, tfs.acknowledged_at,
                        tfs.actual_response, tfs.destination_office,
                        tfs.notes, tfs.created_at, tfs.updated_at
                    FROM tax_filing_submission tfs
                    LEFT JOIN entity e ON e.id = tfs.entity_id
                    WHERE tfs.id = :sid
                    """
                ),
                {"sid": str(submission_id)},
            )
        ).mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail="submission not found")
        assert_entity_in_scope(workspace, row["entity_id"])

    return _row_to_response(dict(row))


# ---------------------------------------------------------------------------
# PATCH — update
# ---------------------------------------------------------------------------

_VALID_STATUSES = (
    "draft", "submitted", "in_transit", "delivered",
    "acknowledged", "awaiting_followup", "closed",
)


@router.patch("/api/workspaces/{workspace_id}/tax-filing-submissions/{submission_id}")
async def update_submission(
    workspace_id: UUID,
    submission_id: UUID,
    body: PatchSubmissionBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SubmissionResponse:
    """Update tracking_number / status / actual_response / notes."""
    require_role(workspace, "accountant")

    set_clauses: list[str] = []
    params: dict[str, Any] = {"sid": str(submission_id)}

    if body.tracking_number is not None:
        set_clauses.append("tracking_number = :track")
        params["track"] = body.tracking_number
    if body.submitted_at is not None:
        set_clauses.append("submitted_at = :sat")
        params["sat"] = body.submitted_at
    if body.status is not None:
        if body.status not in _VALID_STATUSES:
            raise HTTPException(
                status_code=400, detail=f"invalid status '{body.status}'"
            )
        set_clauses.append("status = :status")
        params["status"] = body.status
    if body.actual_response is not None:
        set_clauses.append("actual_response = :resp")
        params["resp"] = body.actual_response
    if body.notes is not None:
        set_clauses.append("notes = :notes")
        params["notes"] = body.notes
    if body.acknowledged_at is not None:
        set_clauses.append("acknowledged_at = :ack")
        params["ack"] = body.acknowledged_at

    if not set_clauses:
        raise HTTPException(status_code=400, detail="no fields to update")

    set_sql = ", ".join(set_clauses)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Verify entity-scope first
        existing = (
            await session.execute(
                text("SELECT entity_id FROM tax_filing_submission WHERE id = :sid"),
                {"sid": str(submission_id)},
            )
        ).first()
        if not existing:
            raise HTTPException(status_code=404, detail="submission not found")
        assert_entity_in_scope(workspace, existing[0])

        row = (
            await session.execute(
                text(
                    f"""
                    UPDATE tax_filing_submission
                    SET {set_sql}
                    WHERE id = :sid
                    RETURNING
                        id, workspace_id, entity_id, jurisdiction, tax_year,
                        submission_title, forms_enclosed, filing_method,
                        tracking_number, status, submitted_at,
                        expected_response_by, last_tracking_status,
                        last_tracking_at, delivered_at, acknowledged_at,
                        actual_response, destination_office, notes,
                        created_at, updated_at
                    """
                ),
                params,
            )
        ).mappings().first()

        ent = (
            await session.execute(
                text("SELECT display_name FROM entity WHERE id = :eid"),
                {"eid": str(row["entity_id"])},
            )
        ).mappings().first()
        row_dict = dict(row)
        row_dict["entity_display_name"] = ent["display_name"] if ent else None

    return _row_to_response(row_dict)


# ---------------------------------------------------------------------------
# POST — refresh tracking for one submission
# ---------------------------------------------------------------------------

@router.post(
    "/api/workspaces/{workspace_id}/tax-filing-submissions/{submission_id}/refresh-tracking"
)
async def refresh_tracking(
    workspace_id: UUID,
    submission_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> SubmissionResponse:
    """Hit USPS for a fresh tracking status and persist to the row."""
    require_role(workspace, "viewer")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT tfs.id, tfs.entity_id, tfs.tracking_number,
                           tfs.filing_method, tfs.status
                    FROM tax_filing_submission tfs
                    WHERE tfs.id = :sid
                    """
                ),
                {"sid": str(submission_id)},
            )
        ).mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail="submission not found")
        assert_entity_in_scope(workspace, row["entity_id"])

        if row["filing_method"] != "paper":
            raise HTTPException(
                status_code=400,
                detail="refresh-tracking only applies to paper submissions",
            )
        if not row["tracking_number"]:
            raise HTTPException(
                status_code=400,
                detail="submission has no tracking_number to refresh",
            )

        result = await lookup_tracking(row["tracking_number"])
        # Compute next status
        new_status = row["status"]
        new_delivered_at = None
        if result.is_delivered and new_status in ("submitted", "in_transit"):
            new_status = "delivered"
            # USPS event_date format is "May 1, 2026"; we leave as-is for now
            # and the caller can manually set delivered_at on PATCH.
            new_delivered_at = dt.datetime.now(tz=dt.timezone.utc)
        elif (not result.is_delivered) and new_status == "submitted" and result.events:
            new_status = "in_transit"

        await session.execute(
            text(
                """
                UPDATE tax_filing_submission
                SET last_tracking_status = :status,
                    last_tracking_at = now(),
                    delivered_at = COALESCE(:delivered_at, delivered_at),
                    status = :new_status
                WHERE id = :sid
                """
            ),
            {
                "status": result.summary[:200] if result.summary else None,
                "delivered_at": new_delivered_at,
                "new_status": new_status,
                "sid": str(submission_id),
            },
        )

        refreshed = (
            await session.execute(
                text(
                    """
                    SELECT
                        tfs.id, tfs.workspace_id, tfs.entity_id,
                        e.display_name AS entity_display_name,
                        tfs.jurisdiction, tfs.tax_year,
                        tfs.submission_title, tfs.forms_enclosed,
                        tfs.filing_method, tfs.tracking_number, tfs.status,
                        tfs.submitted_at, tfs.expected_response_by,
                        tfs.last_tracking_status, tfs.last_tracking_at,
                        tfs.delivered_at, tfs.acknowledged_at,
                        tfs.actual_response, tfs.destination_office,
                        tfs.notes, tfs.created_at, tfs.updated_at
                    FROM tax_filing_submission tfs
                    LEFT JOIN entity e ON e.id = tfs.entity_id
                    WHERE tfs.id = :sid
                    """
                ),
                {"sid": str(submission_id)},
            )
        ).mappings().first()

    return _row_to_response(dict(refreshed))


# ---------------------------------------------------------------------------
# Daily job: flag awaiting_followup
# ---------------------------------------------------------------------------

async def daily_flag_overdue(workspace_id: UUID) -> int:
    """Flag any open submissions whose expected_response_by has lapsed.

    Returns the count of rows flipped to ``awaiting_followup``. Intended to be
    called by a cron job (one per workspace).
    """
    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    UPDATE tax_filing_submission
                    SET status = 'awaiting_followup'
                    WHERE status IN ('submitted', 'in_transit', 'delivered')
                      AND expected_response_by IS NOT NULL
                      AND expected_response_by < now()
                    RETURNING id
                    """
                )
            )
        ).all()
    return len(rows)
