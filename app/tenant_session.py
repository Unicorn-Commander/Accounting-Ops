"""Per-request tenant session pattern. SET LOCAL search_path inside transaction.

Why this pattern (per research synthesis):
- `SET LOCAL` auto-resets at COMMIT/ROLLBACK; no pool bleed between requests.
- Single global AsyncEngine + default pool stays efficient.
- Cross-request schema rotation is safe without DISCARD ALL.
- Compatible with PgBouncer transaction-pooling if introduced later.
- Avoids the schema_translate_map compiled-cache pitfall (SQLA #5004).

The schema name MUST be validated against our naming convention before being
interpolated into SQL — schemas in pg are identifiers, not parameters, and
cannot be bound.
"""
from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


_TENANT_SCHEMA_RE = re.compile(
    r"^accounting_ops_[0-9a-f]{8}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{4}_[0-9a-f]{12}$"
)


def schema_for_tenant(tenant_id: str | uuid.UUID) -> str:
    """Compute the Postgres schema name for a tenant UUID.

    Examples
    --------
    >>> schema_for_tenant("8b3c4d5e-1234-5678-9abc-def012345678")
    'accounting_ops_8b3c4d5e_1234_5678_9abc_def012345678'
    """
    sid = str(tenant_id).lower()
    schema = f"accounting_ops_{sid.replace('-', '_')}"
    if not _TENANT_SCHEMA_RE.match(schema):
        raise ValueError(
            f"Invalid tenant id: {tenant_id!r}. Expected lowercase UUID."
        )
    return schema


async def set_tenant_search_path(session: AsyncSession, tenant_id: str | uuid.UUID) -> None:
    """Pin search_path to the tenant's schema for the current transaction.

    Call once at the start of a per-request transaction. SET LOCAL means the
    change auto-resets at COMMIT/ROLLBACK and cannot bleed into the next
    request that gets this pooled connection.
    """
    schema = schema_for_tenant(tenant_id)
    # We've validated the schema name above; pg identifiers cannot be bound.
    await session.execute(text(f'SET LOCAL search_path = "{schema}", public'))


@asynccontextmanager
async def tenant_session(
    sessionmaker: async_sessionmaker[AsyncSession],
    tenant_id: str | uuid.UUID,
) -> AsyncIterator[AsyncSession]:
    """Context manager: open a session, begin a transaction, set tenant search_path."""
    async with sessionmaker() as session:
        async with session.begin():
            await set_tenant_search_path(session, tenant_id)
            yield session
