"""Provision a new workspace + create entities + seed FalkorDB subgraph.

Shared-schema model (post tenant/0025+0026): every workspace's rows live in the
fixed `books` schema, isolated by FORCE RLS on workspace_id. Provisioning is
therefore rows-only:
  A) public schema: insert the workspace registry row (postgres_schema='books')
  B) seed entity rows into `books` (RLS WITH CHECK passes because
     workspace_session sets app.current_workspace)
Plus FalkorDB subgraph + initial entity rows. No CREATE SCHEMA, no per-workspace
alembic run — schema DDL is a one-time `alembic -x tenant=books upgrade head`.

Usage:
    python -m scripts.provision_workspace <workspace_uuid|new> "Display Name" \\
        [--slug auto] [--hipaa] [--tier shared|premium|enterprise] \\
        [--entity "Acme Inc:c_corp" --entity "Jane Personal:personal"]
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
    SHARED_SCHEMA,
    workspace_session,
)

try:
    from app.falkordb import ensure_workspace_subgraph  # noqa: E402
except Exception:  # pragma: no cover
    ensure_workspace_subgraph = None  # type: ignore[assignment]


# Provisioning is an ADMIN operation: the public registry and shared-schema seed
# INSERTs must run as the table-OWNING role (accounting_ops), NOT the
# least-privilege runtime role. Shared-schema DDL runs separately through the
# deploy-time tenant migration. We use a dedicated admin engine bound to
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
    uc_org_id: str | None = None,
) -> str:
    # Shared-schema model: no per-workspace schema is created; every workspace's
    # rows live in the fixed SHARED_SCHEMA and are isolated by FORCE RLS on
    # workspace_id. The registry column is kept populated (constant) for
    # backward-compat with any reader that still selects postgres_schema.
    schema = SHARED_SCHEMA
    subgraph = f"workspace_{str(workspace_id).replace('-', '_')}"
    bucket = f"accounting-ops-{workspace_id}"

    async with engine.begin() as conn:
        await conn.execute(
            text(
                # uc_org_id (migration 0012) is the UC platform org a workspace
                # meters inference under. COALESCE on conflict so re-provisioning
                # without an org never NULLs out an already-bridged workspace.
                """
                INSERT INTO public.workspace (
                    id, slug, display_name, postgres_schema,
                    falkordb_subgraph, garage_bucket,
                    hipaa_protected, isolation_tier, uc_org_id, status
                )
                VALUES (
                    :id, :slug, :display_name, :schema,
                    :subgraph, :bucket,
                    :hipaa, :tier, :uc_org_id, 'provisioning'
                )
                ON CONFLICT (id) DO UPDATE
                SET display_name = EXCLUDED.display_name,
                    hipaa_protected = EXCLUDED.hipaa_protected,
                    isolation_tier = EXCLUDED.isolation_tier,
                    uc_org_id = COALESCE(EXCLUDED.uc_org_id, public.workspace.uc_org_id)
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
                "uc_org_id": uc_org_id,
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
                ON CONFLICT (workspace_id, slug) DO UPDATE
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
    uc_org_id: str | None = None,
) -> tuple[str, list[uuid.UUID]]:
    """End-to-end workspace provisioning. Returns (schema_name, [entity_ids]).

    `entities` is a list of (display_name, kind) tuples. kind is one of
    c_corp / s_corp / llc / partnership / sole_proprietorship / personal / trust / non_profit / estate / other.

    Shared-schema model: provisioning inserts the workspace registry row + seed
    entity rows only (no CREATE SCHEMA, no per-schema migration). It still runs
    on the admin/owner engine so the registry write + the seed INSERT (under
    FORCE RLS, with app.current_workspace set by workspace_session) succeed.
    Pass `engine=None` (recommended) to use the admin engine bound to
    ADMIN_DATABASE_URL.
    """
    if engine is None:
        engine = _admin_engine()
    slug = slug or slugify(display_name)
    schema = await create_workspace_record(
        engine, workspace_id, display_name, slug, hipaa_protected, isolation_tier,
        uc_org_id=uc_org_id,
    )
    # Shared-schema model: tables already exist in SHARED_SCHEMA, so provisioning
    # inserts rows only — NO CREATE SCHEMA and NO per-schema alembic fan-out.
    # (Schema DDL is a one-time `alembic -x tenant=books upgrade head`, run at
    # deploy time via upgrade_all_workspaces / the bootstrap, not per workspace.)
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
    """Bring the tenant schema up to head.

    Shared-schema model: there is exactly ONE tenant schema (SHARED_SCHEMA), so
    this is a single `alembic -x tenant=books upgrade head` — no per-workspace
    fan-out. Kept as the deploy-time migration entrypoint (name preserved for
    back-compat with the entrypoint/Makefile). The `engine` arg is unused now
    (the alembic subprocess uses the admin URL directly) and accepted only so
    existing callers don't break.
    """
    results: list[tuple[str, bool]] = []
    try:
        await asyncio.to_thread(run_alembic_upgrade, SHARED_SCHEMA)
        results.append((SHARED_SCHEMA, True))
    except Exception as exc:
        print(f"FAILED {SHARED_SCHEMA}: {exc}", file=sys.stderr)
        results.append((SHARED_SCHEMA, False))
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
        help="Entity to create: 'Display Name:kind' (e.g. 'Acme Inc:c_corp'). Repeatable.",
    )
    parser.add_argument(
        "--uc-org-id",
        help="UC platform org (org_<uuid>) this workspace meters inference under. "
        "Sets workspace.uc_org_id so the gateway/virtual-key path bills the right "
        "customer (see docs/INFERENCE-AND-BILLING.md).",
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
        uc_org_id=args.uc_org_id,
    )
    print(f"provisioned workspace {wid} ({args.display_name})")
    print(f"  schema: {schema}")
    print(f"  tier:   {args.tier}")
    if args.uc_org_id:
        print(f"  uc_org: {args.uc_org_id}")
    if entity_ids:
        print(f"  entities: {len(entity_ids)}")
        for eid, (display, kind) in zip(entity_ids, entities):
            print(f"    - {display} ({kind}) -> {eid}")


if __name__ == "__main__":
    asyncio.run(_cli_main())
