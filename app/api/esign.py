"""E-signature API — send a finished work product for the client's signature
and track it to completion.

This is a REAL capability (the taxpayer signs their OWN document — no UPL or
IRS-transmission concern), gated behind a PER-WORKSPACE feature flag so a
workspace opts in explicitly.

FLAG GATE
=========
Creation requires the workspace's EngagementPolicy to ENABLE the ``esign``
channel: ``policy.channel_enabled("esign")`` — i.e. ``submission_channels.esign``
is truthy AND the global ``rail_kill_switch`` is off. (Chosen over the
"review_mode != autonomous_off" alternative because that would effectively
default-ON for every workspace, which is not a real per-workspace flag.) An
admin enables it by setting ``submission_channels.esign = true`` in the
engagement policy. Read / cancel / mark-signed of EXISTING requests are NOT
gated, so in-flight work can always be finished even if the flag is later off.

Routes (all nested under a work product; entity-scoped + workspace-isolated):

  POST .../work-products/{wp_id}/signature-requests
       Render the work product's current PDF and send it for signature. Role
       floor: accountant. Flag-gated.

  GET  .../work-products/{wp_id}/signature-requests
       List signature requests for the work product. Role floor: viewer.

  GET  .../work-products/{wp_id}/signature-requests/{id}?sync=true
       One request's status. With ?sync=true (role: accountant) it first polls
       the provider (best-effort) and, if the document is signed remotely,
       pulls + stores the completed PDF.

  POST .../work-products/{wp_id}/signature-requests/{id}/mark-signed
       Record completion. Accepts an uploaded signed PDF (multipart field
       ``signed_pdf``) — the Manual download→sign→re-upload path — or marks it
       signed with no stored artifact (a wet signature captured out-of-band).
       Role floor: accountant.

  POST .../work-products/{wp_id}/signature-requests/{id}/cancel
       Cancel a non-terminal request. Role floor: accountant.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import text

from app.api.work_products import _fetch_work_product, _render_work_product
from app.authz import assert_entity_in_scope
from app.config import get_settings
from app.db import SessionLocal
from app.engagement.policy import get_or_create_policy
from app.esign.provider import ProviderUnavailable
from app.esign.service import cancel, record_signed, request_signature, sync_status
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.esign.api")
router = APIRouter(tags=["esign"])

_ESIGN_CHANNEL = "esign"
_PREFIX = (
    "/api/workspaces/{workspace_id}/entities/{entity_id}"
    "/work-products/{wp_id}/signature-requests"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _fetch_signature_request(
    session, entity_id: UUID, wp_id: UUID, sr_id: UUID
) -> dict | None:
    return (
        await session.execute(
            text(
                """
                SELECT id::text, entity_id::text, work_product_id::text,
                       provider, external_id, status, signer_name, signer_email,
                       signing_url, signed_garage_key, signed_path,
                       created_at, updated_at
                FROM signature_request
                WHERE id = :id AND entity_id = :eid AND work_product_id = :wpid
                """
            ),
            {"id": str(sr_id), "eid": str(entity_id), "wpid": str(wp_id)},
        )
    ).mappings().first()


# ---------------------------------------------------------------------------
# Create + send
# ---------------------------------------------------------------------------


class CreateSignatureRequestBody(BaseModel):
    signer_name: str
    signer_email: str | None = None
    subject: str | None = None


@router.post(_PREFIX, status_code=201)
async def create_signature_request(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    body: CreateSignatureRequestBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)

    signer_name = (body.signer_name or "").strip()
    if not signer_name:
        raise HTTPException(status_code=400, detail="signer_name is required.")
    signer_email = (body.signer_email or "").strip() or None
    if signer_email and "@" not in signer_email:
        raise HTTPException(status_code=400, detail="signer_email is not a valid email.")

    try:
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            # --- per-workspace feature-flag gate ---
            policy = await get_or_create_policy(session, workspace.workspace_id)
            if not policy.channel_enabled(_ESIGN_CHANNEL):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "E-signature is not enabled for this workspace. An admin "
                        "can enable it by setting submission_channels.esign=true in "
                        "the engagement policy (and clearing the rail kill-switch)."
                    ),
                )

            wp = await _fetch_work_product(session, entity_id, wp_id)
            if wp is None:
                raise HTTPException(status_code=404, detail="Work product not found")

            # Re-render the current PDF (enforces anti-fabrication provenance).
            try:
                pdf_bytes, _content_type = _render_work_product(
                    wp["kind"], wp["tax_year"], wp["status"], wp["inputs"] or {}
                )
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

            subject = body.subject or f"Please sign: {wp.get('title') or wp['kind']}"
            result = await request_signature(
                session,
                workspace_id=workspace.workspace_id,
                entity_id=entity_id,
                work_product_id=wp_id,
                signer_name=signer_name,
                signer_email=signer_email,
                pdf_bytes=pdf_bytes,
                subject=subject,
            )
    except ProviderUnavailable as exc:
        # The insert was rolled back with the transaction — no orphan row.
        raise HTTPException(
            status_code=502, detail=f"E-signature provider unavailable: {exc}"
        )

    _log.info(
        "esign.request created sr=%s wp=%s ws=%s entity=%s by=%s provider=%s status=%s",
        result["id"], wp_id, workspace.workspace_id, entity_id,
        workspace.user_uc_uid, result["provider"], result["status"],
    )
    return result


# ---------------------------------------------------------------------------
# List + detail
# ---------------------------------------------------------------------------


@router.get(_PREFIX)
async def list_signature_requests(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    status_filter: str | None = Query(None, alias="status"),
    limit: int = 100,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)
    limit = max(1, min(limit, 500))
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["entity_id = :eid", "work_product_id = :wpid"]
        params: dict[str, Any] = {
            "eid": str(entity_id), "wpid": str(wp_id), "limit": limit
        }
        if status_filter:
            where.append("status = :st")
            params["st"] = status_filter.strip().lower()
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text, work_product_id::text, provider, external_id,
                           status, signer_name, signer_email, signing_url,
                           signed_garage_key, signed_path, created_at, updated_at
                    FROM signature_request
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
    return {"items": [dict(r) for r in rows]}


@router.get(_PREFIX + "/{sr_id}")
async def get_signature_request(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    sr_id: UUID,
    sync: bool = Query(False),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        if sync:
            # A sync may call the provider and write — require a writer role.
            require_role(workspace, "accountant")
            try:
                synced = await sync_status(
                    session, entity_id=entity_id, work_product_id=wp_id, sr_id=sr_id
                )
                # sync_status returns None ONLY when the request doesn't exist
                # (the existence check precedes any provider call).
                if synced is None:
                    raise HTTPException(
                        status_code=404, detail="Signature request not found"
                    )
            except ProviderUnavailable as exc:
                # Best-effort: fall back to the stored status rather than 502.
                _log.warning("esign.sync provider unavailable sr=%s: %s", sr_id, exc)
        row = await _fetch_signature_request(session, entity_id, wp_id, sr_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Signature request not found")
    return dict(row)


# ---------------------------------------------------------------------------
# Mark signed (Manual re-upload path) + cancel
# ---------------------------------------------------------------------------


@router.post(_PREFIX + "/{sr_id}/mark-signed")
async def mark_signed(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    sr_id: UUID,
    signed_pdf: UploadFile | None = File(default=None),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)

    signed_bytes: bytes | None = None
    if signed_pdf is not None:
        signed_bytes = await signed_pdf.read()
        max_bytes = get_settings().document_upload_max_bytes
        if len(signed_bytes) > max_bytes:
            raise HTTPException(
                status_code=413, detail="Signed PDF exceeds the maximum allowed size."
            )
        if not signed_bytes:
            signed_bytes = None

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        try:
            result = await record_signed(
                session,
                entity_id=entity_id,
                work_product_id=wp_id,
                sr_id=sr_id,
                signed_bytes=signed_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="Signature request not found")
    _log.info(
        "esign.mark_signed sr=%s wp=%s ws=%s by=%s stored=%s",
        sr_id, wp_id, workspace.workspace_id, workspace.user_uc_uid,
        bool(signed_bytes),
    )
    return result


@router.post(_PREFIX + "/{sr_id}/cancel")
async def cancel_signature_request(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    sr_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        try:
            result = await cancel(
                session, entity_id=entity_id, work_product_id=wp_id, sr_id=sr_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if result is None:
            raise HTTPException(status_code=404, detail="Signature request not found")
    _log.info(
        "esign.cancel sr=%s wp=%s ws=%s by=%s",
        sr_id, wp_id, workspace.workspace_id, workspace.user_uc_uid,
    )
    return result
