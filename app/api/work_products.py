"""Work-products API — finished, submission-ready tax artifacts.

A WorkProduct (filled W-4P / ST-3 / 1040 / CP504 response, …) is the document the
platform produces when it takes a user ALL THE WAY. The PDF is RE-RENDERED ON
DEMAND from the row's ``inputs`` (each {value, source_ref, origin}) so there is
never a stale snapshot and the watermark always reflects the current ``status``.

Routes (all entity-scoped + workspace-isolated via RLS):

  GET  /api/workspaces/{wid}/entities/{eid}/work-products?kind=&tax_year=
       List work products for an entity. Role floor: viewer.

  GET  /api/workspaces/{wid}/entities/{eid}/work-products/{id}
       One work product's metadata. Role floor: viewer.

  GET  /api/workspaces/{wid}/entities/{eid}/work-products/{id}/document.pdf
       Re-render + stream the filled PDF (watermark by status). ?disposition=
       inline|attachment. Role floor: viewer. Audited on every fetch.

  POST /api/workspaces/{wid}/entities/{eid}/work-products/{id}/attest
       Self-certify a DRAFT (draft -> self_approved), ONLY when the workspace's
       EngagementPolicy allows it (review_mode=self_attest_allowed). This is the
       bypassable CPA gate: when no professional is available the user can finish
       the work product themselves; otherwise it stays a hard wall. Role floor:
       accountant.

ANTI-FABRICATION: before rendering, every input with a non-blank value must carry
a ``source_ref``; otherwise the render is refused (422). Missing data is left
blank (conservative), never guessed.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.engagement.policy import get_or_create_policy
from app.exports.form_prefill import WatermarkMode, W4PFill, generate_w4p_pdf
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.work_products.api")
router = APIRouter(tags=["work-products"])


# ---------------------------------------------------------------------------
# Render (re-render on demand from inputs; watermark by status)
# ---------------------------------------------------------------------------

_W4P_TEXT_KEYS = (
    "first_name_mi", "last_name", "address", "city_state_zip", "ssn",
    "filing_status",
    "step2a", "step2b", "step2c",
    "step3a_child", "step3b_other", "step3c_credits", "step3_total",
    "step4a_income", "step4b_deductions", "step4c_extra",
)


def _assert_provenance(inputs: dict[str, Any]) -> None:
    """Reject any non-blank input that lacks a source_ref (anti-fabrication)."""
    for key, rec in inputs.items():
        if not isinstance(rec, dict):
            raise ValueError(f"work-product input {key!r} is malformed (expected an object)")
        value = rec.get("value")
        if value is None or str(value).strip() == "":
            continue  # blank is allowed — conservative blank-fill
        if not rec.get("source_ref"):
            raise ValueError(
                f"work-product input {key!r} has a value but no source_ref — "
                "refusing to render a figure without provenance"
            )


def _watermark_for_status(status: str) -> WatermarkMode:
    if status == "self_approved":
        return WatermarkMode.SELF_CERTIFIED
    if status in ("pro_reviewed", "finalized"):
        return WatermarkMode.NONE
    return WatermarkMode.DRAFT


def _w4p_fill_from_inputs(tax_year: int, inputs: dict[str, Any]) -> W4PFill:
    kwargs: dict[str, str] = {}
    for key in _W4P_TEXT_KEYS:
        rec = inputs.get(key)
        if rec and rec.get("value") not in (None, ""):
            kwargs[key] = str(rec["value"])
    nw = inputs.get("no_withholding") or {}
    no_withholding = str(nw.get("value", "")).strip().lower() in ("true", "1", "yes")
    return W4PFill(tax_year=tax_year, no_withholding=no_withholding, **kwargs)


def _render_work_product(
    kind: str, tax_year: int | None, status: str, inputs: dict[str, Any]
) -> tuple[bytes, str]:
    """Render a work product to PDF bytes. Raises ValueError on bad/missing data."""
    _assert_provenance(inputs)
    mode = _watermark_for_status(status)
    if kind == "w-4p":
        fill = _w4p_fill_from_inputs(tax_year or 2026, inputs)
        return generate_w4p_pdf(fill, mode)
    if kind == "cp504":
        from app.exports.cp504_letter import render_cp504
        return render_cp504(tax_year, mode, inputs)
    if kind == "st-3":
        from app.exports.st3_fill import render_st3
        return render_st3(tax_year, mode, inputs)
    if kind == "1040":
        from app.exports.f1040_fill import render_1040
        return render_1040(tax_year, mode, inputs)
    raise ValueError(f"No renderer for work-product kind {kind!r} yet.")


# ---------------------------------------------------------------------------
# List + detail
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/entities/{entity_id}/work-products")
async def list_work_products(
    workspace_id: UUID,
    entity_id: UUID,
    kind: str | None = None,
    tax_year: int | None = None,
    limit: int = 100,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)
    limit = max(1, min(limit, 500))
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["entity_id = :eid"]
        params: dict[str, Any] = {"eid": str(entity_id), "limit": limit}
        if kind:
            where.append("kind = :kind")
            params["kind"] = kind
        if tax_year:
            where.append("tax_year = :ty")
            params["ty"] = tax_year
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT id::text, kind, tax_year, title, status, attestation_kind,
                           attested_by_uc_uid, attested_at, source_extraction_id::text,
                           created_at, updated_at
                    FROM work_product
                    WHERE {' AND '.join(where)}
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()
    return {"items": [dict(r) for r in rows]}


async def _fetch_work_product(session, entity_id: UUID, wp_id: UUID) -> dict | None:
    return (
        await session.execute(
            text(
                """
                SELECT id::text, entity_id::text, kind, tax_year, title, status,
                       attestation_kind, inputs, provenance,
                       source_extraction_id::text, created_at, updated_at
                FROM work_product
                WHERE id = :id AND entity_id = :eid
                """
            ),
            {"id": str(wp_id), "eid": str(entity_id)},
        )
    ).mappings().first()


@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/work-products/{wp_id}"
)
async def get_work_product(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = await _fetch_work_product(session, entity_id, wp_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Work product not found")
    return dict(row)


@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/work-products/{wp_id}/document.pdf"
)
async def get_work_product_pdf(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    disposition: str = Query("inline"),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = await _fetch_work_product(session, entity_id, wp_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Work product not found")

    try:
        pdf_bytes, content_type = _render_work_product(
            row["kind"], row["tax_year"], row["status"], row["inputs"] or {}
        )
    except ValueError as e:
        # Provenance failure or unsupported kind — never emit a fabricated figure.
        raise HTTPException(status_code=422, detail=str(e))

    # Audit every fetch (structured app log; PII-free identifiers only).
    _log.info(
        "work_product.pdf served wp=%s kind=%s status=%s ws=%s entity=%s by=%s",
        row["id"], row["kind"], row["status"],
        workspace.workspace_id, entity_id, workspace.user_uc_uid,
    )

    disp = "attachment" if disposition == "attachment" else "inline"
    filename = f"{row['kind']}-{row.get('tax_year') or ''}.pdf".replace("--", "-")
    headers = {"Content-Disposition": f'{disp}; filename="{filename}"'}
    if row["status"] == "draft":
        headers["X-Draft-Notice"] = "DRAFT — review required before signing"
    return Response(content=pdf_bytes, media_type=content_type, headers=headers)


# ---------------------------------------------------------------------------
# Self-attest (the bypassable CPA gate)
# ---------------------------------------------------------------------------


class AttestBody(BaseModel):
    confirm: bool = False
    notes: str | None = None


@router.post(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/work-products/{wp_id}/attest"
)
async def attest_work_product(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    body: AttestBody,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)
    if not body.confirm:
        raise HTTPException(
            status_code=400,
            detail="confirm must be true to self-certify this work product.",
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT status FROM work_product
                    WHERE id = :id AND entity_id = :eid
                    FOR UPDATE
                    """
                ),
                {"id": str(wp_id), "eid": str(entity_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Work product not found")

        policy = await get_or_create_policy(session, workspace.workspace_id)
        if not policy.can_self_attest:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Self-certification is not enabled for this workspace "
                    "(review_mode is not 'self_attest_allowed'). A licensed "
                    "professional review is required. If no professional is "
                    "available, an admin can enable self-attestation in the "
                    "engagement policy."
                ),
            )
        if row["status"] != "draft":
            raise HTTPException(
                status_code=409,
                detail=f"Work product is '{row['status']}'; only a draft can be self-certified.",
            )

        await session.execute(
            text(
                """
                UPDATE work_product
                SET status = 'self_approved',
                    attestation_kind = 'self',
                    attested_by_uc_uid = :uc,
                    attested_at = now(),
                    updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(wp_id), "uc": workspace.user_uc_uid},
        )

    _log.info(
        "work_product.attest wp=%s self-certified by=%s ws=%s",
        wp_id, workspace.user_uc_uid, workspace.workspace_id,
    )
    return {
        "ok": True,
        "id": str(wp_id),
        "status": "self_approved",
        "attestation_kind": "self",
    }
