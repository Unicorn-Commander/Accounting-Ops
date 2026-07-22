"""Per-request workspace + entity session.

Pattern (shared-table RLS+GUC — the suite-canonical model; migrations
tenant/0025+0026 consolidated the former schema-per-workspace layout into the
single fixed `books` schema):
- Single global AsyncEngine + default pool stays efficient.
- Per-request: BEGIN
      -> SET LOCAL search_path = books, public         (constant; not per-tenant)
      -> SET LOCAL app.current_workspace = '<workspace_uuid>'  (the isolation key)
      -> queries -> COMMIT
- SET LOCAL auto-resets at COMMIT/ROLLBACK; no pool bleed.
- Entity-level scoping is enforced in application code, not at the DB layer
  (workspace_id is the isolation boundary; entity_id is a row predicate).

Defense in depth:
- Every workspace-scoped table lives in `books` and carries a workspace_id
  column with a FORCE ROW LEVEL SECURITY policy `workspace_id =
  current_workspace_id()` (migrations tenant/0026). `current_workspace_id()`
  reads the `app.current_workspace` GUC this module sets per request; if the
  GUC is unset it returns NULL, so `workspace_id = NULL` is never true and the
  query sees ZERO rows (default-deny). Cross-workspace leakage is a SQL
  impossibility enforced by the engine, not by developer discipline.
- RLS is FORCEd, so it applies even to the table owner / admin role: a query
  that forgets to set the GUC sees nothing, regardless of which role runs it.
  The runtime role is additionally NOT the owner and NOT BYPASSRLS.

Why workspace via RLS + entity at row level:
- Workspace isolation is a hard requirement (HIPAA + customer separation);
  FORCE RLS on workspace_id makes cross-workspace leakage impossible at the
  engine layer while keeping one shared schema (bounded pg_catalog, one
  migration head — no per-tenant schema fan-out).
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


#: The single shared schema that holds every workspace's rows post-consolidation.
SHARED_SCHEMA = "books"


def schema_for_workspace(workspace_id: str | uuid.UUID) -> str:
    """DEPRECATED (shared-schema model): compute the legacy per-workspace schema name.

    Retained only for historical tooling/tests and to validate that a workspace
    id is a well-formed UUID. Runtime sessions now use the constant
    ``SHARED_SCHEMA`` (``books``); nothing should derive a search_path from this.
    """
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
      - search_path = books, public                   (constant shared schema)
      - app.current_workspace = <workspace_uuid>      (the RLS isolation key)
      - app.current_entity = <entity_uuid>            (when known; optional)

    SET LOCAL / set_config(..., is_local => true) auto-reset at COMMIT/ROLLBACK
    and cannot bleed into the next request that reuses this pooled connection.

    Isolation is enforced by FORCE RLS on workspace_id keyed on the
    app.current_workspace GUC (migrations tenant/0026), not by a per-workspace
    schema. The GUC value goes through set_config with bound parameters.
    """
    # Defensive: the GUC value below is what RLS keys on, so make sure the
    # workspace id is a well-formed UUID (raises on garbage). search_path is a
    # fixed literal (no interpolation) in the shared-schema model.
    uuid.UUID(str(workspace_id))
    await session.execute(text("SET LOCAL search_path = books, public"))
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
