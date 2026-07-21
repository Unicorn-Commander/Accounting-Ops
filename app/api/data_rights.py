"""Data-rights endpoints: export, erasure, retention.

Every route here is owner-gated. These are the operations that hand a customer
everything we hold, or destroy it — there is no read-only member use case for
either, and an accountant with `accountant` role on a client workspace should
not be able to erase that client's books.

Route shape follows the codebase convention: no router prefix, full
``/api/workspaces/{workspace_id}/...`` paths declared per route, and
``Depends(resolve_workspace)`` + an imperative ``require_role`` as the first
statement in the body.

NOTE ON AUDIT: every mutating route writes to ``decision_audit`` (the in-tenant
append-only trail). Erasure ADDITIONALLY writes a durable receipt into
``public.workspace`` — outside the tenant data — because the in-tenant audit row
is itself destroyed by the purge it records. See ``erasure.ErasureReceipt``.
"""
from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.data_rights import erasure as erasure_mod
from app.data_rights import export as export_mod
from app.data_rights import retention as retention_mod
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.tools._audit import write_decision_audit
from app.workspace_session import SHARED_SCHEMA, workspace_session

router = APIRouter(tags=["data-rights"])

EXPORT_JOBS_BUCKET = "exports"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _entity_ids(session, workspace_id: UUID) -> list[UUID]:
    rows = (
        await session.execute(
            text(f'SELECT id FROM {SHARED_SCHEMA}."entity" WHERE workspace_id = :wid'),  # noqa: S608
            {"wid": str(workspace_id)},
        )
    ).mappings().all()
    return [UUID(str(r["id"])) for r in rows]


async def _audit_all_entities(
    session,
    workspace: WorkspaceContext,
    *,
    tool_name: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any] | None,
    reasoning: str,
    outcome: str,
) -> int:
    """Write one decision_audit row per entity.

    ``decision_audit`` is entity-scoped, and a workspace-level action touches
    every entity's books, so one row each is the accurate record rather than an
    arbitrary single row. A workspace with no entities yet writes none — there is
    nothing entity-scoped to attribute it to, and the durable receipt (for
    erasure) covers the workspace level regardless.
    """
    written = 0
    for eid in await _entity_ids(session, workspace.workspace_id):
        await write_decision_audit(
            session,
            workspace_id=workspace.workspace_id,
            entity_id=eid,
            ctx=workspace,
            tool_name=tool_name,
            request_payload=request_payload,
            response_payload=response_payload,
            reasoning=reasoning,
            outcome=outcome,
        )
        written += 1
    return written


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
class RetentionPut(BaseModel):
    ledger_retention_days: int = Field(
        ...,
        description=(
            "How long ledger records are kept after an erasure request before "
            "they are purged. 0 means purge immediately on request."
        ),
    )


@router.get("/api/workspaces/{workspace_id}/data-rights/retention")
async def get_retention_config(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Effective retention configuration, and the documented default."""
    require_role(workspace, "admin")
    async with SessionLocal() as session:
        async with session.begin():
            cfg = await retention_mod.get_retention(session, workspace.workspace_id)
    return cfg.to_dict()


@router.put("/api/workspaces/{workspace_id}/data-rights/retention")
async def put_retention_config(
    workspace_id: UUID,
    body: RetentionPut,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Set the per-workspace retention override. Owner only."""
    require_role(workspace, "owner")
    # Validate BEFORE opening a connection: a bad value should cost nothing and
    # must not depend on the database being reachable to be rejected.
    try:
        retention_mod.validate_retention_days(body.ledger_retention_days)
    except retention_mod.RetentionConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    async with SessionLocal() as session:
        async with session.begin():
            cfg = await retention_mod.set_retention(
                session,
                workspace.workspace_id,
                ledger_retention_days=body.ledger_retention_days,
                actor_uc_uid=workspace.user_uc_uid,
            )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        await _audit_all_entities(
            session,
            workspace,
            tool_name="data_rights.set_retention",
            request_payload={"ledger_retention_days": body.ledger_retention_days},
            response_payload=cfg.to_dict(),
            reasoning="Workspace owner changed the data-retention policy.",
            outcome="applied",
        )
    return cfg.to_dict()


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
async def _run_export(workspace_id: UUID, job_id: UUID, requested_by: str, slug: str) -> None:
    """Background export. Opens its own sessions and NEVER raises.

    BackgroundTasks runs after the response, outside the request transaction, so
    the workspace GUC does not survive — this must open its own
    ``workspace_session``. A raise here would be invisible to the caller, so
    every failure is written into the job record instead.
    """
    started = dt.datetime.now(dt.timezone.utc)
    record: dict[str, Any] = {
        "id": str(job_id),
        "status": "running",
        "requested_by_uc_uid": requested_by,
        "started_at": started.isoformat(),
    }
    try:
        async with workspace_session(SessionLocal, workspace_id) as session:
            data, result = await export_mod.build_export(
                session,
                workspace_id,
                requested_by_uc_uid=requested_by,
                workspace_slug=slug,
            )
        bucket, key = await export_mod.store_export(data, workspace_id, job_id)
        record.update(
            status="completed",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            bucket=bucket,
            object_key=key,
            summary=result.manifest_summary(),
        )
    except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
        record.update(
            status="failed",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            error=f"{type(exc).__name__}: {exc}",
        )
    try:
        async with SessionLocal() as session:
            async with session.begin():
                await retention_mod.record_job(
                    session, workspace_id, bucket=EXPORT_JOBS_BUCKET, record=record
                )
    except Exception:  # noqa: BLE001 - nothing left to report to
        pass


@router.post(
    "/api/workspaces/{workspace_id}/data-rights/exports",
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_export(
    workspace_id: UUID,
    background_tasks: BackgroundTasks,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Start a full workspace export. Owner only. Returns a poll URL."""
    require_role(workspace, "owner")
    job_id = uuid4()
    now = dt.datetime.now(dt.timezone.utc)

    async with SessionLocal() as session:
        async with session.begin():
            await retention_mod.record_job(
                session,
                workspace.workspace_id,
                bucket=EXPORT_JOBS_BUCKET,
                record={
                    "id": str(job_id),
                    "status": "running",
                    "requested_by_uc_uid": workspace.user_uc_uid,
                    "started_at": now.isoformat(),
                },
            )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        await _audit_all_entities(
            session,
            workspace,
            tool_name="data_rights.export",
            request_payload={"job_id": str(job_id)},
            response_payload=None,
            reasoning="Workspace owner requested a full data export.",
            outcome="started",
        )

    background_tasks.add_task(
        _run_export, workspace.workspace_id, job_id, workspace.user_uc_uid, workspace.workspace_slug
    )
    return {
        "job_id": str(job_id),
        "status": "running",
        "poll_url": f"/api/workspaces/{workspace_id}/data-rights/exports/{job_id}",
    }


@router.get("/api/workspaces/{workspace_id}/data-rights/exports")
async def list_exports(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "owner")
    async with SessionLocal() as session:
        async with session.begin():
            jobs = await retention_mod.read_jobs(
                session, workspace.workspace_id, bucket=EXPORT_JOBS_BUCKET
            )
    return {"exports": jobs}


@router.get("/api/workspaces/{workspace_id}/data-rights/exports/{job_id}")
async def get_export(
    workspace_id: UUID,
    job_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "owner")
    async with SessionLocal() as session:
        async with session.begin():
            jobs = await retention_mod.read_jobs(
                session, workspace.workspace_id, bucket=EXPORT_JOBS_BUCKET
            )
    for job in jobs:
        if job.get("id") == str(job_id):
            if job.get("status") == "completed":
                job = {
                    **job,
                    "download_url": (
                        f"/api/workspaces/{workspace_id}/data-rights/exports/"
                        f"{job_id}/download"
                    ),
                }
            return job
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export job not found")


@router.get("/api/workspaces/{workspace_id}/data-rights/exports/{job_id}/download")
async def download_export(
    workspace_id: UUID,
    job_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    """Download the archive.

    The object key is read from the job record rather than reconstructed from
    user input, so a caller cannot address another workspace's object by
    crafting a job id — the record itself is workspace-scoped.
    """
    require_role(workspace, "owner")
    async with SessionLocal() as session:
        async with session.begin():
            jobs = await retention_mod.read_jobs(
                session, workspace.workspace_id, bucket=EXPORT_JOBS_BUCKET
            )
    job = next((j for j in jobs if j.get("id") == str(job_id)), None)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Export job not found")
    if job.get("status") != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Export is not ready (status: {job.get('status')}).",
        )

    from app.clients.garage_s3 import GarageNotConfigured, GarageS3Client, GarageUploadError

    try:
        body = await GarageS3Client().get_object(
            bucket=job["bucket"], object_key=job["object_key"]
        )
    except GarageNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Object storage is not configured on this deployment.",
        ) from exc
    except GarageUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not retrieve the export archive from object storage.",
        ) from exc

    return Response(
        content=body,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="accounting-ops-export-{job_id}.zip"'
            )
        },
    )


# --------------------------------------------------------------------------- #
# Erasure
# --------------------------------------------------------------------------- #
class ErasureRequest(BaseModel):
    confirm: str = Field(
        ...,
        description=(
            "Must exactly match the workspace slug. Proves the caller knows which "
            "workspace they are destroying."
        ),
    )
    acknowledge_irreversible: bool = Field(
        False, description="Must be true. Erasure cannot be undone."
    )
    immediate_ledger_purge: bool = Field(
        False,
        description=(
            "Skip the retention window and purge ledger records now. Offered so a "
            "customer can demand immediate destruction, as the privacy policy says "
            "they can."
        ),
    )


@router.post("/api/workspaces/{workspace_id}/data-rights/erasure")
async def request_erasure(
    workspace_id: UUID,
    body: ErasureRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Erase a workspace. OWNER ONLY. Irreversible. Requires explicit confirmation.

    Runs Tier 1 (credentials, documents, tax identifiers) synchronously, then
    Tier 2 (access revocation, membership removal, ledger purge). Every step
    reports its own status; the response is a receipt, not a boolean. See
    ``app/data_rights/erasure.py`` for the design rationale.
    """
    require_role(workspace, "owner")

    if not body.acknowledge_irreversible:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Erasure requires acknowledge_irreversible=true. Nothing was deleted."
            ),
        )
    try:
        erasure_mod.verify_confirmation(body.confirm, workspace.workspace_slug)
    except erasure_mod.ErasureConfirmationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    now = dt.datetime.now(dt.timezone.utc)
    receipt = erasure_mod.ErasureReceipt(
        workspace_id=str(workspace.workspace_id),
        requested_by_uc_uid=workspace.user_uc_uid,
        requested_at=now.isoformat(),
    )

    # Retention decides whether the ledger purge runs now or is deferred.
    async with SessionLocal() as session:
        async with session.begin():
            cfg = await retention_mod.get_retention(session, workspace.workspace_id)
    receipt.retention_days = cfg.ledger_retention_days
    purge_now = body.immediate_ledger_purge or cfg.ledger_retention_days == 0
    due_at = retention_mod.purge_due_at(now, cfg)
    receipt.ledger_purge_due_at = None if purge_now else due_at.isoformat()

    # --- Audit BEFORE destroying anything. The in-tenant trail is itself
    # purged in Tier 2, which is exactly why the durable receipt exists. -----
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        await _audit_all_entities(
            session,
            workspace,
            tool_name="data_rights.erasure",
            request_payload={
                "confirm": "<redacted>",
                "immediate_ledger_purge": body.immediate_ledger_purge,
            },
            response_payload=None,
            reasoning="Workspace owner requested erasure of all workspace data.",
            outcome="started",
        )

    # --- Tier 1 — runtime role, ordered by risk ----------------------------
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        receipt.steps.append(
            await erasure_mod.purge_credentials(session, workspace.workspace_id)
        )
        receipt.steps.append(
            await erasure_mod.purge_documents(session, workspace.workspace_id)
        )
        receipt.steps.append(
            await erasure_mod.purge_tax_identifiers(session, workspace.workspace_id)
        )

    # --- Tier 2 — admin role -----------------------------------------------
    receipt.steps.extend(await erasure_mod.revoke_access(workspace.workspace_id))

    if purge_now:
        receipt.steps.append(
            await erasure_mod.purge_workspace_ledger(workspace.workspace_id)
        )
    else:
        receipt.steps.append(
            erasure_mod.ErasureStep(
                "ledger_purged",
                erasure_mod.STATUS_DEFERRED,
                (
                    f"Scheduled for {due_at.isoformat()} "
                    f"({cfg.ledger_retention_days}-day retention window). Request "
                    "again with immediate_ledger_purge=true to destroy now."
                ),
            )
        )

    # --- Durable record, written LAST so it captures every step's outcome ---
    async with SessionLocal() as session:
        async with session.begin():
            await erasure_mod.record_erasure(session, workspace.workspace_id, receipt)

    return receipt.to_dict()


@router.get("/api/workspaces/{workspace_id}/data-rights/erasure")
async def get_erasure_record(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Read the erasure receipt.

    Note: once erasure has revoked access, this route returns 403 like every
    other workspace route, because membership resolution requires an active
    workspace. The receipt is returned in full by the POST for that reason —
    callers should keep it. Post-erasure retrieval is an operator action.
    """
    require_role(workspace, "owner")
    async with SessionLocal() as session:
        async with session.begin():
            rec = await erasure_mod.read_erasure_record(session, workspace.workspace_id)
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No erasure has been requested for this workspace.",
        )
    return rec


__all__ = ("router",)
