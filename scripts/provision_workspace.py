"""Provision a new workspace + create entities + seed FalkorDB subgraph.

Two-phase per the research synthesis:
  A) public schema: CREATE SCHEMA + insert workspace registry row
  B) subprocess: alembic upgrade head against the new schema
Plus FalkorDB subgraph + constraints + initial entity rows.

Usage:
    python -m scripts.provision_workspace <workspace_uuid|new> "Display Name" \\
        [--slug auto] [--hipaa] [--tier shared|premium|enterprise] \\
        [--entity "Magic Unicorn Inc:c_corp" --entity "Aaron Personal:personal"]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import uuid
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_admin_database_url  # noqa: E402
from app.workspace_session import (  # noqa: E402
    schema_for_workspace,
    workspace_session,
)

try:
    from app.falkordb import ensure_workspace_subgraph  # noqa: E402
except Exception:  # pragma: no cover
    ensure_workspace_subgraph = None  # type: ignore[assignment]


# Provisioning is an ADMIN operation: CREATE SCHEMA + alembic DDL + the seed
# INSERTs must run as the table-OWNING role (accounting_ops), NOT the
# least-privilege runtime role. We use a dedicated admin engine bound to
# ADMIN_DATABASE_URL (falls back to DATABASE_URL for single-role setups).
@lru_cache
def _admin_engine() -> AsyncEngine:
    return create_async_engine(get_admin_database_url(), pool_pre_ping=True)


@lru_cache
def _admin_sessionmaker() -> async_sessionmaker:
    return async_sessionmaker(_admin_engine(), expire_on_commit=False)


def slugify(value: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return s[:96] or "workspace"


async def create_workspace_record(
    engine: AsyncEngine,
    workspace_id: uuid.UUID,
    display_name: str,
    slug: str,
    hipaa_protected: bool = False,
    isolation_tier: str = "shared",
) -> str:
    schema = schema_for_workspace(str(workspace_id))
    subgraph = f"workspace_{str(workspace_id).replace('-', '_')}"
    bucket = f"accounting-ops-{workspace_id}"

    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        await conn.execute(
            text(
                """
                INSERT INTO public.workspace (
                    id, slug, display_name, postgres_schema,
                    falkordb_subgraph, garage_bucket,
                    hipaa_protected, isolation_tier, status
                )
                VALUES (
                    :id, :slug, :display_name, :schema,
                    :subgraph, :bucket,
                    :hipaa, :tier, 'provisioning'
                )
                ON CONFLICT (id) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    hipaa_protected = EXCLUDED.hipaa_protected,
                    isolation_tier = EXCLUDED.isolation_tier
                """
            ),
            {
                "id": str(workspace_id),
                "slug": slug,
                "display_name": display_name,
                "schema": schema,
                "subgraph": subgraph,
                "bucket": bucket,
                "hipaa": hipaa_protected,
                "tier": isolation_tier,
            },
        )
    return schema


def run_alembic_upgrade(schema: str) -> None:
    project_root = Path(__file__).resolve().parent.parent
    # The alembic env reads get_settings().database_url. Point that at the
    # ADMIN (owner) URL for the subprocess so migrations run as the owner —
    # tables, triggers, grants, and RLS policies must be created by the owner,
    # not the runtime role.
    child_env = dict(os.environ)
    child_env["DATABASE_URL"] = get_admin_database_url()
    result = subprocess.run(
        [
            "alembic",
            "-c",
            "alembic-tenant.ini",
            "-x",
            f"tenant={schema}",
            "upgrade",
            "head",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=project_root,
        env=child_env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"alembic upgrade failed for {schema}:\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )


async def mark_workspace_active(engine: AsyncEngine, workspace_id: uuid.UUID) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE public.workspace SET status = 'active' WHERE id = :id"),
            {"id": str(workspace_id)},
        )


async def create_entity(
    workspace_id: uuid.UUID,
    entity_id: uuid.UUID,
    display_name: str,
    kind: str,
    *,
    legal_name: str | None = None,
    country_code: str = "US",
    functional_currency: str = "USD",
    hipaa_protected: bool = False,
) -> uuid.UUID:
    """Create an entity row within the workspace's schema. Idempotent on slug.

    Runs on the ADMIN sessionmaker (owner role) — provisioning is an admin op.
    workspace_session sets app.current_workspace so the RLS WITH CHECK on the
    entity INSERT passes regardless of which role runs it.
    """
    slug = slugify(display_name)
    async with workspace_session(_admin_sessionmaker(), workspace_id) as session:
        result = await session.execute(
            text(
                """
                INSERT INTO entity (
                    id, workspace_id, slug, display_name, legal_name, kind,
                    country_code, functional_currency, hipaa_protected, status
                )
                VALUES (
                    :id, :workspace_id, :slug, :display_name, :legal_name, CAST(:kind AS entity_type),
                    :country, :currency, :hipaa, 'active'
                )
                ON CONFLICT (slug) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    legal_name = EXCLUDED.legal_name,
                    hipaa_protected = EXCLUDED.hipaa_protected
                RETURNING id
                """
            ),
            {
                "id": str(entity_id),
                "workspace_id": str(workspace_id),
                "slug": slug,
                "display_name": display_name,
                "legal_name": legal_name,
                "kind": kind,
                "country": country_code.upper(),
                "currency": functional_currency.upper(),
                "hipaa": hipaa_protected,
            },
        )
        return result.scalar_one()


async def provision_workspace(
    engine: AsyncEngine | None,
    workspace_id: uuid.UUID,
    display_name: str,
    *,
    slug: str | None = None,
    hipaa_protected: bool = False,
    isolation_tier: str = "shared",
    entities: list[tuple[str, str]] | None = None,
) -> tuple[str, list[uuid.UUID]]:
    """End-to-end workspace provisioning. Returns (schema_name, [entity_ids]).

    `entities` is a list of (display_name, kind) tuples. kind is one of
    c_corp / s_corp / llc / partnership / sole_proprietorship / personal / trust / non_profit / estate / other.

    Provisioning is an ADMIN/owner operation (CREATE SCHEMA + DDL + seed rows).
    Pass `engine=None` (recommended) to use the admin engine bound to
    ADMIN_DATABASE_URL. A passed-in engine is honored for back-compat but MUST
    be an owner-role engine, or the CREATE SCHEMA + registry writes will fail.
    """
    if engine is None:
        engine = _admin_engine()
    slug = slug or slugify(display_name)
    schema = await create_workspace_record(
        engine, workspace_id, display_name, slug, hipaa_protected, isolation_tier
    )
    await asyncio.to_thread(run_alembic_upgrade, schema)
    if ensure_workspace_subgraph is not None:
        try:
            await ensure_workspace_subgraph(workspace_id)
        except Exception as exc:
            print(
                f"  FalkorDB subgraph setup skipped ({exc}); will retry on first agent call",
                file=sys.stderr,
            )

    entity_ids: list[uuid.UUID] = []
    if entities:
        for display, kind in entities:
            eid = uuid.uuid5(
                uuid.NAMESPACE_DNS, f"{workspace_id}:entity:{slugify(display)}"
            )
            await create_entity(workspace_id, eid, display, kind)
            entity_ids.append(eid)

    await mark_workspace_active(engine, workspace_id)
    return schema, entity_ids


async def upgrade_all_workspaces(engine: AsyncEngine | None = None) -> list[tuple[str, bool]]:
    """Re-run tenant migrations against every active workspace schema.

    Defaults to the admin engine; the alembic subprocess itself always uses the
    admin URL (run_alembic_upgrade), so this only needs to read the registry.
    """
    if engine is None:
        engine = _admin_engine()
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT postgres_schema FROM public.workspace WHERE status = 'active'"
                )
            )
        ).all()
    results: list[tuple[str, bool]] = []
    for (schema,) in rows:
        try:
            await asyncio.to_thread(run_alembic_upgrade, schema)
            results.append((schema, True))
        except Exception as exc:
            print(f"FAILED {schema}: {exc}", file=sys.stderr)
            results.append((schema, False))
    return results


async def _cli_main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision a new Accounting-Ops workspace + entities"
    )
    parser.add_argument(
        "workspace_id",
        help="UUID for the new workspace, or 'new' to generate one",
    )
    parser.add_argument("display_name", help="Human-readable workspace name")
    parser.add_argument("--slug", help="URL slug (auto if omitted)")
    parser.add_argument(
        "--hipaa", action="store_true", help="HIPAA-protected workspace"
    )
    parser.add_argument(
        "--tier",
        choices=["shared", "premium", "enterprise"],
        default="shared",
        help="Isolation tier",
    )
    parser.add_argument(
        "--entity",
        action="append",
        default=[],
        help="Entity to create: 'Display Name:kind' (e.g. 'Magic Unicorn Inc:c_corp'). Repeatable.",
    )
    args = parser.parse_args()

    if args.workspace_id == "new":
        wid = uuid.uuid4()
    else:
        wid = uuid.UUID(args.workspace_id)

    entities: list[tuple[str, str]] = []
    for raw in args.entity:
        if ":" not in raw:
            print(f"Skipping malformed --entity '{raw}' (need 'Name:kind')", file=sys.stderr)
            continue
        name, kind = raw.split(":", 1)
        entities.append((name.strip(), kind.strip()))

    schema, entity_ids = await provision_workspace(
        _admin_engine(),
        wid,
        args.display_name,
        slug=args.slug,
        hipaa_protected=args.hipaa,
        isolation_tier=args.tier,
        entities=entities,
    )
    print(f"provisioned workspace {wid} ({args.display_name})")
    print(f"  schema: {schema}")
    print(f"  tier:   {args.tier}")
    if entity_ids:
        print(f"  entities: {len(entity_ids)}")
        for eid, (display, kind) in zip(entity_ids, entities):
            print(f"    - {display} ({kind}) -> {eid}")


if __name__ == "__main__":
    asyncio.run(_cli_main())
