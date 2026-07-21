"""Verify the schema-per-workspace -> shared `books` consolidation (no data loss).

Two modes (see docs/TENANCY-CONSOLIDATION-RUNBOOK.md):

  baseline  — BEFORE cutover, read the per-workspace schemas and capture, per
              workspace: per-table rowcount + content checksum, and the journal
              hash-chain result per entity. Writes JSON.

                python -m scripts.verify_consolidation baseline --out /tmp/ao_baseline.json

  verify    — AFTER the rename+copy, read `books` and, per workspace from the
              baseline, recompute per-table rowcount + checksum (filtered to that
              workspace_id) and re-verify each entity's chain. Asserts byte-for-
              byte equality and clean chains. Optionally proves RLS is fail-closed
              as the runtime role.

                python -m scripts.verify_consolidation verify --baseline /tmp/ao_baseline.json [--rls-check]

Checksums use md5(string_agg(t::text, '' ORDER BY id)); UTC + ISO datestyle are
pinned so the row text is stable across sessions. Because source schemas and
`books` share identical column order (same DDL lineage), a row's t::text is
identical in both, so a per-workspace checksum match is a byte-for-byte proof.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_admin_database_url  # noqa: E402
from app.journal.hash_chain import chain_summary, verify_chain  # noqa: E402
from app.workspace_session import SHARED_SCHEMA  # noqa: E402


def _admin_url() -> str:
    url = get_admin_database_url()
    return url if "+asyncpg" in url else url.replace("postgresql://", "postgresql+asyncpg://", 1)


async def _pin_session(session, schema: str, workspace_id: str | None = None) -> None:
    await session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
    await session.execute(text("SET LOCAL datestyle = 'ISO, YMD'"))
    await session.execute(text(f'SET LOCAL search_path = "{schema}", public'))
    if workspace_id is not None:
        await session.execute(
            text("SELECT set_config('app.current_workspace', :w, true)"),
            {"w": workspace_id},
        )


async def _scoped_tables(session, schema: str) -> list[str]:
    rows = (
        await session.execute(
            text(
                """
                SELECT c.relname FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_attribute a ON a.attrelid = c.oid
                     AND a.attname='workspace_id' AND NOT a.attisdropped
                WHERE n.nspname=:s AND c.relkind='r' ORDER BY c.relname
                """
            ),
            {"s": schema},
        )
    ).all()
    return [r[0] for r in rows]


async def _table_stats(session, table: str, workspace_id: str | None) -> tuple[int, str | None]:
    """(rowcount, md5-checksum) for a table, optionally filtered to one workspace."""
    where = "WHERE workspace_id = :w" if workspace_id else ""
    params = {"w": workspace_id} if workspace_id else {}
    n = (
        await session.execute(
            text(f'SELECT count(*) FROM "{table}" {where}'), params
        )
    ).scalar_one()
    # Order by the whole-row text (PK-agnostic — some tables have no `id`
    # column, e.g. activity_event, closed_period). Identical row sets produce
    # identical sorted t::text on both sides, so the md5 is a byte-for-byte
    # equality proof independent of physical/PK ordering.
    chk = (
        await session.execute(
            text(
                f"SELECT md5(coalesce(string_agg(t::text, '' ORDER BY t::text), '')) "
                f'FROM "{table}" t {where}'
            ),
            params,
        )
    ).scalar_one()
    return int(n), chk


async def _entities(session, schema: str, workspace_id: str | None) -> list[str]:
    where = "WHERE workspace_id = :w" if workspace_id else ""
    params = {"w": workspace_id} if workspace_id else {}
    rows = (
        await session.execute(
            text(f"SELECT id::text FROM entity {where} ORDER BY id"), params
        )
    ).all()
    return [r[0] for r in rows]


async def _active_source_schemas(session) -> list[tuple[str, str]]:
    """(schema, workspace_id) for every active tenant with an existing schema."""
    rows = (
        await session.execute(
            text(
                r"""
                SELECT n.nspname, w.id::text
                FROM pg_namespace n
                JOIN public.workspace w ON w.postgres_schema = n.nspname
                WHERE n.nspname LIKE 'accounting\_ops\_%' AND w.status='active'
                ORDER BY n.nspname
                """
            )
        )
    ).all()
    return [(r[0], r[1]) for r in rows]


async def cmd_baseline(out: str) -> None:
    engine = create_async_engine(_admin_url())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    result: dict = {"workspaces": {}}
    async with sm() as s:
        async with s.begin():
            await _pin_session(s, "public")
            sources = await _active_source_schemas(s)
    print(f"baseline: {len(sources)} active source schema(s)")
    for schema, wid in sources:
        entry: dict = {"schema": schema, "tables": {}, "chains": {}}
        async with sm() as s:
            async with s.begin():
                await _pin_session(s, schema, wid)
                tables = await _scoped_tables(s, schema)
                for t in tables:
                    n, chk = await _table_stats(s, t, None)
                    entry["tables"][t] = {"rows": n, "md5": chk}
                for eid in await _entities(s, schema, None):
                    summary = chain_summary(await verify_chain(s, eid))
                    entry["chains"][eid] = summary
        total = sum(v["rows"] for v in entry["tables"].values())
        bad = [e for e, c in entry["chains"].items() if not c["ok"]]
        print(f"  {schema} ws={wid}: {len(tables)} tables, {total} rows, "
              f"{len(entry['chains'])} entities, chains_ok={not bad}")
        if bad:
            print(f"    !! BROKEN CHAINS at baseline: {bad}")
        result["workspaces"][wid] = entry
    Path(out).write_text(json.dumps(result, indent=2))
    print(f"wrote {out}")
    await engine.dispose()


async def cmd_verify(baseline_path: str, target: str, rls_check: bool) -> None:
    baseline = json.loads(Path(baseline_path).read_text())
    engine = create_async_engine(_admin_url())
    sm = async_sessionmaker(engine, expire_on_commit=False)
    failures: list[str] = []

    for wid, base in baseline["workspaces"].items():
        async with sm() as s:
            async with s.begin():
                await _pin_session(s, target, wid)
                for t, bstats in base["tables"].items():
                    n, chk = await _table_stats(s, t, wid)
                    if n != bstats["rows"]:
                        failures.append(f"{wid}/{t}: rows {n} != baseline {bstats['rows']}")
                    if chk != bstats["md5"]:
                        failures.append(f"{wid}/{t}: md5 mismatch (data not byte-identical)")
                for eid, bsum in base["chains"].items():
                    summary = chain_summary(await verify_chain(s, eid))
                    if not summary["ok"]:
                        failures.append(f"{wid}/entity {eid}: chain BROKEN post-migration")
                    elif summary["tampered_count"] != bsum["tampered_count"]:
                        failures.append(f"{wid}/entity {eid}: chain result changed")
        print(f"  verified ws={wid}: {len(base['tables'])} tables, {len(base['chains'])} chains")

    if rls_check:
        await _rls_fail_closed(target, baseline, failures)

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        await engine.dispose()
        sys.exit(1)
    print("\nPASS: rowcounts + checksums + journal chains all match baseline.")
    await engine.dispose()


async def _rls_fail_closed(target: str, baseline: dict, failures: list[str]) -> None:
    """As the runtime role: no GUC -> 0 rows; GUC set -> exactly one workspace."""
    import os

    runtime_url = os.environ.get("DATABASE_URL")
    if not runtime_url:
        failures.append("rls-check: DATABASE_URL (runtime role) not set")
        return
    if "+asyncpg" not in runtime_url:
        runtime_url = runtime_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    eng = create_async_engine(runtime_url)
    sm = async_sessionmaker(eng, expire_on_commit=False)
    async with sm() as s:
        async with s.begin():
            await s.execute(text(f'SET LOCAL search_path = "{target}", public'))
            # No GUC set -> default-deny.
            n = (await s.execute(text("SELECT count(*) FROM journal_entry"))).scalar_one()
            if n != 0:
                failures.append(f"rls-check: no-GUC read returned {n} rows (expected 0)")
    wid = next(iter(baseline["workspaces"]))
    async with sm() as s:
        async with s.begin():
            await s.execute(text(f'SET LOCAL search_path = "{target}", public'))
            await s.execute(
                text("SELECT set_config('app.current_workspace', :w, true)"), {"w": wid}
            )
            distinct = (
                await s.execute(text("SELECT count(DISTINCT workspace_id) FROM journal_entry"))
            ).scalar_one()
            if distinct not in (0, 1):
                failures.append(f"rls-check: GUC-scoped read saw {distinct} workspaces (expected <=1)")
    print("  rls fail-closed check ran (no-GUC=0 rows, GUC-scoped<=1 workspace)")
    await eng.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="mode", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--out", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--baseline", required=True)
    v.add_argument("--target", default=SHARED_SCHEMA)
    v.add_argument("--rls-check", action="store_true")
    args = ap.parse_args()

    if args.mode == "baseline":
        asyncio.run(cmd_baseline(args.out))
    else:
        asyncio.run(cmd_verify(args.baseline, args.target, args.rls_check))


if __name__ == "__main__":
    main()
