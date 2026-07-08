"""Per-request workspace + entity session.

Pattern (per research synthesis):
- Single global AsyncEngine + default pool stays efficient.
- Per-request: BEGIN
      -> SET LOCAL search_path = "<workspace_schema>", public
      -> SET LOCAL app.current_workspace = '<workspace_uuid>'  (RLS backstop)
      -> queries -> COMMIT
- SET LOCAL auto-resets at COMMIT/ROLLBACK; no pool bleed.
- Entity-level scoping is enforced in application code, not at the DB layer
  (workspace_id is the schema boundary; entity_id is a row predicate).

Defense in depth (audit finding C3):
- search_path picks the *schema* (workspace boundary). If a query ever runs
  with a missing/stale search_path, it could touch another workspace's schema.
- The `app.current_workspace` GUC is read by Row-Level Security policies on
  every workspace-scoped table (migrations tenant/0005). Because each schema
  holds exactly one workspace's rows (workspace_id == that workspace), a query
  that lands in the wrong schema sees ZERO rows instead of another tenant's
  data: RLS filters `workspace_id = app.current_workspace`, and the wrong
  schema has no rows with this workspace_id. So the GUC + RLS turns a
  search_path mistake from a data leak into an empty result.
- The runtime role is NOT the table owner and is NOT BYPASSRLS, so RLS is
  fully enforced against it. RLS is ENABLEd (not FORCEd): the owner role keeps
  full visibility for migrations + operator maintenance; only the app's
  least-privilege role is gated. See migrations/tenant/0005 for the rationale.

Why workspace at schema level + entity at row level:
- Workspace isolation is a hard requirement (HIPAA + customer separation).
  Schema-per-workspace makes cross-workspace leakage a SQL impossibility.
- Entity is a sub-grouping within a workspace, used for FERS-style rules,
  per-entity P&L, CPA-handoff scoping. Row-level filter is enough.
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_WORKSPACE_SCHEMA_RE = re.compile(
    r"^accounting_ops_[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}$"
)


def schema_for_workspace(workspace_id: str | uuid.UUID) -> str:
    """Compute the Postgres schema name for a workspace UUID."""
    sid = str(workspace_id).lower()
    schema = f"accounting_ops_{sid.replace('-', '_')}"
    if not _WORKSPACE_SCHEMA_RE.match(schema):
        raise ValueError(
            f"Invalid workspace id: {workspace_id!r}. Expected lowercase UUID."
        )
    return schema


# Backwards-compat alias for any callers still using the old name.
schema_for_tenant = schema_for_workspace


async def set_workspace_search_path(
    session: AsyncSession,
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID | None = None,
) -> None:
    """Pin search_path + RLS GUCs to the workspace for the current transaction.

    Sets, transaction-locally:
      - search_path = "<workspace_schema>", public   (schema boundary)
      - app.current_workspace = <workspace_uuid>      (RLS backstop, required)
      - app.current_entity = <entity_uuid>            (when known; optional)

    SET LOCAL / set_config(..., is_local => true) auto-reset at COMMIT/ROLLBACK
    and cannot bleed into the next request that reuses this pooled connection.

    The schema name is validated as our naming convention by
    schema_for_workspace (raises on a non-UUID), so the f-string for
    search_path is injection-safe. The GUC values go through set_config with
    bound parameters.
    """
    schema = schema_for_workspace(workspace_id)
    await session.execute(text(f'SET LOCAL search_path = "{schema}", public'))
    # set_config(name, value, is_local) — is_local=true == SET LOCAL semantics.
    # Bound params keep the values fully parameterized.
    await session.execute(
        text("SELECT set_config('app.current_workspace', :wid, true)"),
        {"wid": str(workspace_id)},
    )
    if entity_id is not None:
        await session.execute(
            text("SELECT set_config('app.current_entity', :eid, true)"),
            {"eid": str(entity_id)},
        )


@asynccontextmanager
async def workspace_session(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str | uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """Context manager: open a session, begin transaction, pin workspace search_path.

    Usage:
        async with workspace_session(SessionLocal, workspace_id) as session:
            # all queries here run against workspace_id's schema
            ...
    """
    async with sessionmaker() as session:
        async with session.begin():
            await set_workspace_search_path(session, workspace_id)
            yield session


@asynccontextmanager
async def entity_session(
    sessionmaker: async_sessionmaker[AsyncSession],
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
) -> AsyncIterator[tuple[AsyncSession, uuid.UUID]]:
    """Same as workspace_session but yields (session, entity_id_uuid) tuple.

    Application code uses the entity_id to filter rows in queries against
    entity-scoped tables (journal_entry, compliance_rule, approval_queue, etc.).

    The workspace boundary is enforced at the schema level (impossible to
    accidentally read another workspace's data). The entity boundary is
    enforced by application-level filters in tool implementations.

    Usage:
        async with entity_session(SessionLocal, workspace_id, entity_id) as (s, eid):
            stmt = select(JournalEntry).where(JournalEntry.entity_id == eid)
            ...
    """
    entity_uuid = uuid.UUID(str(entity_id))
    async with sessionmaker() as session:
        async with session.begin():
            await set_workspace_search_path(session, workspace_id, entity_uuid)
            yield session, entity_uuid
