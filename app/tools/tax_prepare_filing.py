"""MCP tool: tax_prepare_filing — create a finished tax work product.

The generic creator for work products whose figures come from the caller's own
source documents (a CP504 notice, sales books for an ST-3, W-2s/1099s for a
1040). The dedicated W-4P path is ``tax_w4p.tax_fill_w4p`` (it sources identity
from the 1099-R extraction); this tool covers the forms where the agent/user
supplies the figures, each tagged with where it came from.

ANTI-FABRICATION: every non-blank field MUST carry a ``source_ref`` — the tool
refuses to store a value without one, and the work-products serve route refuses
to render one. Blank fields are simply omitted (conservative blank-fill).

Write tool — requires accountant role (creates a work_product row).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace, current_auth
from app.workspace_session import workspace_session

# w-4p has its own richer, 1099-R-aware tool (tax_fill_w4p).
_SUPPORTED_KINDS = {"cp504", "st-3", "1040"}


async def tax_prepare_filing(
    workspace_id: UUID,
    entity_id: UUID,
    kind: str,
    tax_year: int,
    fields: dict[str, Any],
    title: str | None = None,
) -> dict[str, Any]:
    """Create a DRAFT tax work product (a CP504 response letter, an SC ST-3, or a
    Form 1040) from provenance-bearing fields.

    Args:
        workspace_id, entity_id: scope.
        kind: 'cp504' | 'st-3' | '1040'.
        tax_year: tax year the work product is for.
        fields: {field_key: {"value": str, "source_ref": str, "origin"?: str}}.
            The valid field_keys depend on the form's renderer (see
            app/exports/cp504_letter.py, st3_fill.py, f1040_fill.py). Every
            non-blank value MUST include a source_ref describing where it came
            from (e.g. "CP504 notice", "W-2 box 1", "sales journal", a
            document_extraction id). NEVER invent a figure.
        title: optional human title.

    Returns:
        {"ok": True, "work_product_id", "kind", "tax_year", "status": "draft",
         "download_path", "_draft_notice"}.

    DRAFT — re-rendered with a DRAFT watermark until reviewed or self-certified.
    Role floor: accountant.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")  # noqa: F841

    kind = (kind or "").strip().lower()
    if kind not in _SUPPORTED_KINDS:
        raise ValueError(
            f"kind must be one of {sorted(_SUPPORTED_KINDS)} (W-4P uses "
            f"tax_fill_w4p); got {kind!r}"
        )

    # Normalize + validate provenance (mirrors the render-time guard so a bad
    # work product can never be stored, not just refused at render).
    inputs: dict[str, Any] = {}
    for key, rec in (fields or {}).items():
        if not isinstance(rec, dict):
            raise ValueError(
                f"field {key!r} must be an object {{value, source_ref, origin?}}"
            )
        value = rec.get("value")
        if value is None or str(value).strip() == "":
            continue  # conservative blank-fill — omit blanks
        if not rec.get("source_ref"):
            raise ValueError(
                f"field {key!r} has a value but no source_ref — never fabricate a "
                "figure; tag every value with where it came from"
            )
        inputs[key] = {
            "value": str(value),
            "source_ref": str(rec["source_ref"]),
            "origin": str(rec.get("origin") or "user_entered"),
        }

    auth = current_auth()
    creator = (auth.agent_id or auth.uc_uid) if auth else None
    title = (title or f"{kind.upper()} {tax_year}").strip()
    provenance = {"form": kind, "tax_year": tax_year, "prepared_via": "tax_prepare_filing"}

    async with workspace_session(SessionLocal, workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO work_product (
                        workspace_id, entity_id, kind, tax_year, title, status,
                        inputs, provenance, created_by_uc_uid
                    ) VALUES (
                        :wid, :eid, :kind, :ty, :title, 'draft',
                        CAST(:inputs AS jsonb), CAST(:prov AS jsonb), :creator
                    )
                    RETURNING CAST(id AS text) AS id
                    """
                ),
                {
                    "wid": str(workspace_id),
                    "eid": str(entity_id),
                    "kind": kind,
                    "ty": tax_year,
                    "title": title,
                    "inputs": json.dumps(inputs),
                    "prov": json.dumps(provenance),
                    "creator": creator,
                },
            )
        ).mappings().first()

    wp_id = row["id"]
    download_path = (
        f"/api/workspaces/{workspace_id}/entities/{entity_id}"
        f"/work-products/{wp_id}/document.pdf"
    )
    return {
        "ok": True,
        "work_product_id": wp_id,
        "kind": kind,
        "tax_year": tax_year,
        "status": "draft",
        "download_path": download_path,
        "field_count": len(inputs),
        "_draft_notice": (
            f"DRAFT — this {kind.upper()} carries a DRAFT watermark until reviewed "
            "by a professional or self-certified (if none is available). Review "
            "every figure and sign before submitting."
        ),
    }
