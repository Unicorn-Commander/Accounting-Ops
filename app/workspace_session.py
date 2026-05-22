"""Per-request workspace + entity session.

Pattern (per research synthesis):
- Single global AsyncEngine + default pool stays efficient.
- Per-request: BEGIN -> SET LOCAL search_path = "<workspace_schema>", public -> queries -> COMMIT
- SET LOCAL auto-resets at COMMIT/ROLLBACK; no pool bleed.
- Entity-level scoping is enforced in application code, not at the DB layer
  (workspace_id is the schema boundary; entity_id is a row predicate).

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
    session: AsyncSession, workspace_id: str | uuid.UUID
) -> None:
    """Pin search_path to the workspace's schema for the current transaction.

    SET LOCAL means the change auto-resets at COMMIT/ROLLBACK and cannot bleed
    into the next request that gets this pooled connection.
    """
    schema = schema_for_workspace(workspace_id)
    await session.execute(text(f'SET LOCAL search_path = "{schema}", public'))


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
            await set_workspace_search_path(session, workspace_id)
            yield session, entity_uuid
