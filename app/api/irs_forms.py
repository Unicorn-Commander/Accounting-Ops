"""IRS form pre-fill endpoint.

Generates partially-filled official IRS PDF forms from the tax_review_queue
and the cpa-export JSON package. The CPA downloads, reviews, completes, signs,
and submits. These PDFs are NEVER to be filed as-is — they carry a DRAFT
watermark on every page and a "do not file" banner on page 1.

Routes
------
  GET /api/workspaces/{wid}/entities/{eid}/irs-forms/{tax_year}/{form_name}.pdf

Path params
-----------
  workspace_id  UUID
  entity_id     UUID
  tax_year      int (e.g. 2023, 2024)
  form_name     one of:
                  1120            → Form 1120 page-1 income/deductions
                  1120-schedule-l → Schedule L balance sheet (same base PDF)
                  1125-e          → Form 1125-E officer compensation
                  1120-package    → zip of all three above

Query params
------------
  include_queue_overrides  bool (default true)
    Whether to apply tax_review_queue form_line_proposal amounts as overrides
    over the cpa-export package amounts.

Response
--------
  - application/pdf  for single forms
  - application/zip  for 1120-package

Role floor: viewer (same as tax_review_queue list).

Entity-scope (BOLA): mirrors app/api/approvals.py. A scoped member can only
access forms for their allowed entities.

Dependency on cpa-export
------------------------
If the CPA_EXPORT_URL sidecar is unreachable, the endpoint proceeds with
queue-only data (blanks where the cpa-export would have supplied amounts).
This graceful degradation means a partially-filled form is still returned
rather than a hard 503 — the CPA will see blank lines wherever the
cpa-export data was missing.
"""

from __future__ import annotations

import os
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.config import get_settings
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.tools._cpa_export_client import resolve_cpa_export_bearer
from app.workspace_session import workspace_session

router = APIRouter(tags=["irs-forms"])

_SUPPORTED_FORMS = frozenset({
    "1120", "1120-schedule-l", "1125-e", "1120-package"
})

_VALID_TAX_YEARS = frozenset(range(2020, 2030))


# ---------------------------------------------------------------------------
# Helper: fetch cpa-export JSON package
# ---------------------------------------------------------------------------

async def _fetch_cpa_package(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
) -> dict | None:
    """Call the cpa-export sidecar for the 1120 JSON package.

    Returns the package dict or None if the sidecar is unreachable / returns
    404 (no data yet for that entity-year). Never raises — caller degrades
    gracefully on None.
    """
    settings = get_settings()
    service_token = resolve_cpa_export_bearer()
    headers: dict[str, str] = {}
    if service_token:
        headers["Authorization"] = f"Bearer {service_token}"

    url = (
        f"{settings.cpa_export_url.rstrip('/')}/exports/1120/{tax_year}"
        f"?workspace_id={workspace_id}&entity_id={entity_id}"
    )
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code == 200:
            return resp.json()
        # 404 = no export data yet — treat as empty (not an error)
        return None
    except Exception:
        # Sidecar down: proceed with queue-only data
        return None


# ---------------------------------------------------------------------------
# Helper: fetch entity display name + EIN from DB
# ---------------------------------------------------------------------------

async def _fetch_entity_meta(
    session,
    entity_id: UUID,
) -> tuple[str, str | None]:
    """Return (display_name, ein_hint) for an entity. Falls back to id string.

    The EIN column is AES-256-GCM encrypted in the DB; we cannot decrypt it
    here without the master key context (which the form-fill endpoint doesn't
    carry). We return the display_name for the corp_name header field and None
    for the EIN — the CPA must fill the EIN manually on the paper form.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT display_name
                FROM entity
                WHERE id = :eid
                """
            ),
            {"eid": str(entity_id)},
        )
    ).mappings().first()
    if row:
        return row["display_name"] or str(entity_id), None
    return str(entity_id), None


# ---------------------------------------------------------------------------
# Helper: fetch tax_review_queue items for entity-year
# ---------------------------------------------------------------------------

async def _fetch_queue_items(
    session,
    entity_id: UUID,
    tax_year: int,
    item_type: str = "form_line_proposal",
    statuses: tuple[str, ...] = ("pending", "reviewed"),
) -> list[dict]:
    """Return queue items for an entity-year (form_line_proposal type).

    Includes both pending and reviewed items so recently-reviewed proposals
    are still reflected in the pre-fill. Rejected/resolved items are excluded.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    trq.id::text,
                    trq.item_type,
                    trq.form_reference,
                    trq.content,
                    trq.status
                FROM tax_review_queue trq
                WHERE trq.entity_id = :eid
                  AND trq.tax_year  = :yr
                  AND trq.item_type = :itype
                  AND trq.status    = ANY(:statuses)
                ORDER BY trq.created_at DESC
                """
            ),
            {
                "eid": str(entity_id),
                "yr": tax_year,
                "itype": item_type,
                "statuses": list(statuses),
            },
        )
    ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}"
    "/irs-forms/{tax_year}/{form_name}.pdf",
    response_class=Response,
    summary="Generate a pre-filled IRS form PDF (or zip for 1120-package)",
)
async def generate_irs_form(
    workspace_id: UUID,
    entity_id: UUID,
    tax_year: int,
    form_name: str,
    include_queue_overrides: bool = Query(
        True,
        description=(
            "Apply tax_review_queue form_line_proposal amounts on top of "
            "cpa-export amounts. Defaults to true."
        ),
    ),
    disposition: str = Query(
        "attachment",
        description=(
            "Content-Disposition mode. Pass 'inline' to view in a browser tab "
            "instead of triggering a download. Default: 'attachment'. "
            "Ignored for 1120-package (zip always downloads)."
        ),
    ),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    """Generate a DRAFT pre-filled IRS PDF for the given entity and tax year.

    The returned PDF has a DRAFT watermark on every page and a yellow
    "do not file" banner on page 1. Pass it to the CPA for review, completion,
    and signature — it is NOT ready to file as-is.

    - Form 1120       → income/deductions pre-filled from our data.
    - Schedule L      → balance sheet pre-filled from our data.
    - Form 1125-E     → officer compensation pre-filled from queue.
    - 1120-package    → zip of all three.
    """
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)

    if tax_year not in _VALID_TAX_YEARS:
        raise HTTPException(
            status_code=400,
            detail=f"tax_year must be between 2020 and 2029 (got {tax_year})",
        )

    fn = form_name.lower()
    if fn not in _SUPPORTED_FORMS:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown form '{form_name}'. "
                f"Supported: {', '.join(sorted(_SUPPORTED_FORMS))}."
            ),
        )

    # --- Fetch data in parallel (DB + cpa-export sidecar) ---
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Entity metadata
        entity_display_name, ein = await _fetch_entity_meta(session, entity_id)

        # Queue items (only if requested)
        if include_queue_overrides:
            queue_items = await _fetch_queue_items(session, entity_id, tax_year)
        else:
            queue_items = []

    # cpa-export package (independent of DB session)
    cpa_package = await _fetch_cpa_package(workspace_id, entity_id, tax_year)

    # --- Generate the PDF(s) ---
    try:
        from app.exports.form_prefill import generate_form_pdf
        pdf_bytes, content_type = generate_form_pdf(
            form_name=fn,
            tax_year=tax_year,
            entity_display_name=entity_display_name,
            ein=ein,
            queue_items=queue_items,
            cpa_package=cpa_package,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"IRS form PDF template missing: {exc}. "
                "Run scripts/download_irs_forms.sh on the server to download it."
            ),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Content-Disposition: attachment by default; inline if requested (PDFs only).
    # Zip files always use attachment — browsers can't render zip inline.
    slug = entity_display_name.lower().replace(" ", "-")[:40]
    if content_type == "application/zip":
        filename = f"irs-forms-{slug}-{tax_year}.zip"
        cd_mode = "attachment"
    else:
        filename = f"form-{fn}-{slug}-{tax_year}-draft.pdf"
        cd_mode = "inline" if disposition.lower().strip() == "inline" else "attachment"

    return Response(
        content=pdf_bytes,
        media_type=content_type,
        headers={
            "Content-Disposition": f'{cd_mode}; filename="{filename}"',
            "X-Draft-Notice": (
                "DRAFT - for CPA review only. "
                "Do not file without professional review and signature."
            ),
            "Cache-Control": "no-store",
        },
    )
