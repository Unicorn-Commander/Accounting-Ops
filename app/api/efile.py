"""E-file package API — generate the real e-file artifact for a finished return.

A work product that has been taken all the way (a finished 1040) can be turned
into a MeF-style electronic-filing PACKAGE: a well-formed, schema-shaped XML
return + a manifest a *registered transmitter* could submit. These routes GENERATE
that package from the work product's provenance-bearing ``inputs`` — they do NOT
transmit it. Live IRS/state transmission is gated on the org's EFIN / registered-
transmitter enrollment (see :mod:`app.efile.transmit` and
:mod:`app.submission.rail`); the package is ready, the wire is off.

Routes (entity-scoped + workspace-isolated via RLS; role floor: accountant):

  POST /api/workspaces/{wid}/entities/{eid}/work-products/{wpId}/efile-package
       Generate the package and return its manifest + warnings + an explicit
       "generated, not transmitted" note.

  GET  /api/workspaces/{wid}/entities/{eid}/work-products/{wpId}/efile-package.xml
       Stream the generated MeF-style XML (application/xml).

ANTI-FABRICATION: generation reuses the provenance idea — a package is only built
from a work product whose ``inputs`` are present, and any input that carries a
value but no ``source_ref`` is refused (422). Missing required fields do NOT block
generation; they come back as ``warnings`` (what a transmitter would reject).

This module defines ``router`` (no prefix; full paths in the decorators) for the
orchestrator to register in ``app.main`` alongside the other API routers.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.efile.mef import EfilePackage, build_1040_efile_package
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

_log = logging.getLogger("acctops.efile.api")
router = APIRouter(tags=["efile"])

# Kinds for which a federal e-file package is currently schema-mapped. (State
# packages are a separate, clearly-labeled stub — see app.efile.mef.)
_SUPPORTED_KINDS = {"1040"}


async def _fetch_work_product(session, entity_id: UUID, wp_id: UUID) -> dict | None:
    """Read the work product like the work-products API does (kind/tax_year/
    inputs/status under the workspace session). Returns a mapping or ``None``."""
    return (
        await session.execute(
            text(
                """
                SELECT id::text, entity_id::text, kind, tax_year, status, inputs
                FROM work_product
                WHERE id = :id AND entity_id = :eid
                """
            ),
            {"id": str(wp_id), "eid": str(entity_id)},
        )
    ).mappings().first()


def _generate_package(row: dict, as_of: str | None) -> EfilePackage:
    """Build the e-file package for a fetched work product row, mapping data
    problems to clear 4xx errors (never a fabricated figure)."""
    kind = (row.get("kind") or "").strip().lower()
    if kind not in _SUPPORTED_KINDS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"E-file package generation currently supports kind '1040'; "
                f"this work product is {row.get('kind')!r}."
            ),
        )
    inputs = row.get("inputs") or {}
    if not inputs:
        raise HTTPException(
            status_code=422,
            detail=(
                "This work product has no inputs; there is nothing to generate an "
                "e-file package from."
            ),
        )
    try:
        return build_1040_efile_package(row.get("tax_year"), inputs, as_of=as_of)
    except ValueError as e:
        # Provenance failure or malformed inputs — never emit a fabricated return.
        raise HTTPException(status_code=422, detail=str(e))


def _xml_path(entity_id: UUID, wp_id: str) -> str:
    return f"entities/{entity_id}/work-products/{wp_id}/efile-package.xml"


@router.post(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/work-products/{wp_id}/efile-package"
)
async def generate_efile_package(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    as_of: str | None = Query(
        default=None,
        description=(
            "Optional ISO-8601 timestamp for the return's ReturnTs. When omitted, "
            "a templated placeholder is left for the transmitter to fill at send "
            "time (the server never stamps wall-clock time)."
        ),
    ),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Generate (do NOT transmit) the e-file package for a finished work product."""
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = await _fetch_work_product(session, entity_id, wp_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Work product not found")

    package = _generate_package(row, as_of)

    _log.info(
        "efile.package generated wp=%s kind=%s status=%s lines=%s ws=%s entity=%s by=%s",
        row["id"], row.get("kind"), row.get("status"),
        package.manifest.get("line_count"),
        workspace.workspace_id, entity_id, workspace.user_uc_uid,
    )

    return {
        "ok": True,
        "work_product_id": row["id"],
        "kind": row.get("kind"),
        "tax_year": row.get("tax_year"),
        "status": row.get("status"),
        "manifest": package.manifest,
        "warnings": package.warnings,
        "transmitted": False,
        "xml_path": _xml_path(entity_id, row["id"]),
        "note": (
            "This is a GENERATED e-file package (MeF-style XML + manifest), NOT a "
            "transmission. Live IRS/state filing is gated on the org's EFIN / "
            "registered-transmitter enrollment and is currently disabled. Fetch the "
            "XML at the efile-package.xml route."
        ),
    }


@router.get(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/work-products/{wp_id}/efile-package.xml"
)
async def get_efile_package_xml(
    workspace_id: UUID,
    entity_id: UUID,
    wp_id: UUID,
    as_of: str | None = Query(default=None),
    disposition: str = Query("inline"),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> Response:
    """Stream the generated MeF-style XML return (application/xml)."""
    require_role(workspace, "accountant")
    assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = await _fetch_work_product(session, entity_id, wp_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Work product not found")

    package = _generate_package(row, as_of)

    _log.info(
        "efile.package.xml served wp=%s kind=%s ws=%s entity=%s by=%s",
        row["id"], row.get("kind"),
        workspace.workspace_id, entity_id, workspace.user_uc_uid,
    )

    disp = "attachment" if disposition == "attachment" else "inline"
    filename = f"{row.get('kind') or 'return'}-{row.get('tax_year') or ''}-efile.xml".replace("--", "-")
    headers = {
        "Content-Disposition": f'{disp}; filename="{filename}"',
        # Make the posture unmistakable on the wire: this is a generated package.
        "X-Efile-Notice": "generated-package-not-transmitted",
        "X-Efile-Sha256": str(package.manifest.get("sha256", "")),
    }
    return Response(content=package.xml, media_type=package.content_type, headers=headers)
