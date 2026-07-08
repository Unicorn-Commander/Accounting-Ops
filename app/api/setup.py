"""Setup Assistant bootstrap + checklist persistence.

The Setup Assistant lives at the /setup web route which embeds a chat. The
chat-session creation requires an ``entity_id`` FK, so for the very-first-run
case (workspace with 0 entities), we need to pre-create a placeholder entity.

This endpoint creates that placeholder ONLY if the workspace has zero active
entities. Returns the bound entity_id either way. Idempotent.

Role floor: accountant (the user provisioning their first entity will be
either owner or admin; the floor is set conservatively).

Setup checklist endpoints (added 2026-05-31):
  GET   /api/workspaces/{wid}/setup-progress
  PATCH /api/workspaces/{wid}/setup-progress
The shape lives in the public.workspace.setup_progress JSONB column added by
migration 0011. The PATCH endpoint merges keys (per-step status, skipped flag,
completed_at) rather than replacing the whole document — so a stray
single-step write can't wipe earlier progress.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import bind_http_caller
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session

router = APIRouter(
    tags=["setup"],
    dependencies=[Depends(bind_http_caller)],
)


# ---------------------------------------------------------------------------
# Setup checklist constants
# ---------------------------------------------------------------------------

# The fixed, ordered set of steps shown in the Setup Assistant left rail. The
# order here drives the UI's step-1 / step-2 / step-3 labeling. Add new steps
# by appending — never insert in the middle (existing persisted JSON would
# silently mis-render).
SETUP_STEPS: tuple[str, ...] = (
    "business_profile",
    "entity",
    "tax_facts",
    "banking",
    "coa",
    "review",
)

STEP_STATUSES: tuple[str, ...] = (
    "not_started",
    "current",
    "blocked",
    "complete",
)

SETUP_PROGRESS_VERSION = 1


def _empty_progress() -> dict[str, Any]:
    """Build the default checklist shape returned when nothing is persisted."""
    return {
        "version": SETUP_PROGRESS_VERSION,
        "skipped": False,
        "completed_at": None,
        "steps": {
            step: {"status": "not_started", "updated_at": None}
            for step in SETUP_STEPS
        },
    }


class StepUpdate(BaseModel):
    status: Literal["not_started", "current", "blocked", "complete"]


class SetupProgressPatch(BaseModel):
    """Partial update to setup_progress.

    Merge semantics: any provided field overwrites that field on the persisted
    document; everything else is preserved. Per-step updates are merged into
    the steps dict, not the whole dict.
    """

    skipped: bool | None = None
    completed_at: str | None = None  # ISO timestamp or null
    steps: dict[str, StepUpdate] | None = Field(default=None)


@router.post("/api/workspaces/{workspace_id}/setup/bootstrap")
async def bootstrap_setup_entity(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Ensure at least one entity exists; create a placeholder if needed.

    If the workspace already has an entity, returns the first one. If not,
    creates a placeholder entity called "Setup Sandbox" + seeds a minimal
    chart of accounts, then returns the new entity_id.

    The Setup Assistant chat is bound to this entity_id. As the user walks
    through intake, the assistant calls ``create_entity_with_coa`` to add the
    real entity — at which point the placeholder can be renamed or left as-is
    (the user can delete it later via the entity admin surface).

    Returns:
        ``{"entity_id": "<uuid>", "display_name": ..., "kind": ...,
            "is_placeholder": <bool>}``

    Role floor: accountant.
    """
    require_role(workspace, "accountant")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Is there an existing entity we can bind to?
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id, display_name, kind::text AS kind "
                    "FROM entity WHERE status = 'active' ORDER BY created_at LIMIT 1"
                )
            )
        ).mappings().first()

        if existing:
            return {
                "entity_id": existing["id"],
                "display_name": existing["display_name"],
                "kind": existing["kind"],
                "is_placeholder": False,
            }

    # No entities — provision a placeholder via the same path
    # ``create_entity_with_coa`` uses. We import here to avoid a circular
    # import at module-load time.
    from app.tools.create_entity_with_coa import create_entity_with_coa

    result = await create_entity_with_coa(
        workspace_id=workspace.workspace_id,
        display_name="Setup Sandbox",
        legal_name="Setup Sandbox (Placeholder)",
        kind="other",
        country_code="US",
    )
    return {
        "entity_id": result["entity_id"],
        "display_name": result["display_name"],
        "kind": result["kind"],
        "is_placeholder": True,
    }


# ---------------------------------------------------------------------------
# Setup checklist GET / PATCH
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/setup-progress")
async def get_setup_progress(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Return the workspace's persisted Setup Assistant checklist.

    Empty/None persistent value -> the default ``_empty_progress()`` shape is
    returned (all steps "not_started", not skipped, not complete). The caller
    can therefore treat the response as authoritative without null-checking
    individual fields.

    Read floor: viewer (any member can see where setup is). Writes go through
    PATCH which has a higher floor.
    """
    require_role(workspace, "viewer")
    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT setup_progress FROM public.workspace WHERE id = :id"
                    ),
                    {"id": str(workspace.workspace_id)},
                )
            ).first()
    persisted = row.setup_progress if row else None
    if not persisted:
        return _empty_progress()
    # Defensive merge so an old persisted doc (e.g. missing a newly-added
    # step) renders without exploding.
    defaults = _empty_progress()
    merged_steps = dict(defaults["steps"])
    for step_name, step_value in (persisted.get("steps") or {}).items():
        if step_name in merged_steps:
            merged_steps[step_name] = step_value
    return {
        "version": persisted.get("version", SETUP_PROGRESS_VERSION),
        "skipped": bool(persisted.get("skipped", False)),
        "completed_at": persisted.get("completed_at"),
        "steps": merged_steps,
    }


@router.patch("/api/workspaces/{workspace_id}/setup-progress")
async def patch_setup_progress(
    workspace_id: UUID,
    body: SetupProgressPatch,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Merge a partial update into the workspace's setup_progress JSONB.

    Per-step updates: only the supplied step keys are overwritten; other
    steps are preserved. ``updated_at`` is stamped server-side per step that
    changes.

    Top-level fields:
      * ``skipped`` (bool) - "Skip and continue" path
      * ``completed_at`` (ISO datetime or null) - flips when setup finishes

    Role floor: accountant. Setup is a write action and shouldn't be done
    by viewers or generic bookkeepers.
    """
    require_role(workspace, "accountant")

    # Validate step keys up front so a typo doesn't silently land an unknown
    # step in the JSON document (which would render as "?" in the UI).
    if body.steps:
        unknown = set(body.steps.keys()) - set(SETUP_STEPS)
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Unknown setup step(s): {sorted(unknown)}",
            )

    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()

    async with SessionLocal() as session:
        async with session.begin():
            row = (
                await session.execute(
                    text(
                        "SELECT setup_progress FROM public.workspace "
                        "WHERE id = :id FOR UPDATE"
                    ),
                    {"id": str(workspace.workspace_id)},
                )
            ).first()
            if row is None:
                raise HTTPException(status_code=404, detail="Workspace not found")

            current = row.setup_progress or _empty_progress()
            # Defensive: ensure the version + steps shape exists.
            current.setdefault("version", SETUP_PROGRESS_VERSION)
            current.setdefault("skipped", False)
            current.setdefault("completed_at", None)
            steps = dict(current.get("steps") or {})
            for step in SETUP_STEPS:
                steps.setdefault(step, {"status": "not_started", "updated_at": None})

            if body.steps:
                for step_name, payload in body.steps.items():
                    steps[step_name] = {
                        "status": payload.status,
                        "updated_at": now_iso,
                    }
            if body.skipped is not None:
                current["skipped"] = body.skipped
            if body.completed_at is not None:
                # Allow caller to explicitly clear (empty string -> None).
                current["completed_at"] = body.completed_at or None

            current["steps"] = steps

            await session.execute(
                text(
                    "UPDATE public.workspace "
                    "SET setup_progress = CAST(:p AS jsonb) "
                    "WHERE id = :id"
                ),
                {"p": _serialize(current), "id": str(workspace.workspace_id)},
            )
    return current


def _serialize(value: dict[str, Any]) -> str:
    """JSON-encode a dict for PostgreSQL CAST(... AS jsonb). Avoids needing
    asyncpg-specific JSON codec wiring on this path."""
    import json
    return json.dumps(value)
