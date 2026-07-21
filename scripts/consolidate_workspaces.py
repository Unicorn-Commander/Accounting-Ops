"""Consolidate schema-per-workspace tenants into the shared `books` schema.

One-time cutover step (see docs/TENANCY-CONSOLIDATION-RUNBOOK.md). Runs BETWEEN
tenant migrations 0025 (composite uniques) and 0026 (FORCE RLS):

    ALTER SCHEMA "<primary>" RENAME TO books;                  # done by the runbook
    alembic -c alembic-tenant.ini -x tenant=books upgrade 0025_prep_shared_uniques
    python -m scripts.consolidate_workspaces                    # <-- this script
    alembic -c alembic-tenant.ini -x tenant=books upgrade head  # 0026 FORCE RLS

It copies every row of every workspace-scoped table from each SOURCE tenant
schema into `books`, preserving id / entity_id / prev_hash / this_hash and all
payload columns byte-for-byte (explicit column lists), in FK-parent-first order.
Because the journal hash chain is content-only (per-entity, ordered posted_at,id)
a verbatim copy cannot perturb it — verify with scripts/verify_consolidation.py
before and after.

Runs as the OWNER role (ADMIN_DATABASE_URL). At this point RLS is still
ENABLE-not-FORCE (0026 hasn't run), so the owner copies without needing the GUC.

The ONE insert-side trigger in the schema, `trg_je_monthly_balance` (AFTER
INSERT ON journal_entry -> re-derives monthly_account_balance), is disabled
during the copy so it does not double-count the monthly_account_balance rows we
copy verbatim. It is re-enabled in the same transaction.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_admin_database_url  # noqa: E402
from app.workspace_session import SHARED_SCHEMA  # noqa: E402

# The single insert-firing trigger that must be muted during the copy.
_JE_DERIVE_TRIGGER = "trg_je_monthly_balance"


def _sync_engine() -> Engine:
    url = get_admin_database_url()
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg2")
    return create_engine(url, future=True)


def _scoped_tables(conn, schema: str) -> list[str]:
    rows = conn.execute(
        text(
            """
            SELECT c.relname FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
                 AND a.attname = 'workspace_id' AND NOT a.attisdropped
            WHERE n.nspname = :s AND c.relkind = 'r'
            ORDER BY c.relname
            """
        ),
        {"s": schema},
    ).all()
    return [r[0] for r in rows]


def _fk_parents(conn, schema: str) -> dict[str, set[str]]:
    """child -> {parent, ...} for intra-schema FKs, excluding self-references."""
    rows = conn.execute(
        text(
            """
            SELECT c.conrelid::regclass::text AS child,
                   c.confrelid::regclass::text AS parent
            FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = :s
              AND c.contype = 'f'
              AND c.conrelid <> c.confrelid
            """
        ),
        {"s": schema},
    ).all()

    def bare(qualified: str) -> str:
        # regclass text may be schema-qualified and/or quoted; take the table.
        return qualified.split(".")[-1].strip('"')

    deps: dict[str, set[str]] = {}
    for child, parent in rows:
        deps.setdefault(bare(child), set()).add(bare(parent))
    return deps


def _topo_order(tables: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Parents before children (Kahn's algorithm over the scoped table set)."""
    tset = set(tables)
    remaining = dict((t, {p for p in deps.get(t, set()) if p in tset}) for t in tables)
    ordered: list[str] = []
    while remaining:
        ready = sorted(t for t, ps in remaining.items() if not ps)
        if not ready:
            raise RuntimeError(
                f"FK cycle among scoped tables: {sorted(remaining)} "
                "(self-refs are already excluded)"
            )
        for t in ready:
            ordered.append(t)
            del remaining[t]
        for ps in remaining.values():
            ps.difference_update(ready)
    return ordered


def _copy_columns(conn, schema: str, table: str) -> tuple[str, str]:
    """(insert_column_list, select_expr_list) for INSERT INTO target SELECT FROM source.

    Each per-workspace schema has its OWN copy of every ENUM type (enums are
    schema-scoped), so `<src>.entity_type` != `books.entity_type` even though the
    labels are identical. Enum columns are therefore cast through text to the
    TARGET enum type. Non-enum columns (incl. builtin arrays like text[]) copy
    verbatim.
    """
    rows = conn.execute(
        text(
            """
            SELECT column_name, data_type, udt_name, is_generated
            FROM information_schema.columns
            WHERE table_schema = :s AND table_name = :t
            ORDER BY ordinal_position
            """
        ),
        {"s": schema, "t": table},
    ).all()
    insert_cols: list[str] = []
    select_exprs: list[str] = []
    for name, data_type, udt_name, is_generated in rows:
        # GENERATED ALWAYS columns are recomputed from their expression; a value
        # cannot be inserted into them (and they are derived, not source data).
        if is_generated == "ALWAYS":
            continue
        insert_cols.append(f'"{name}"')
        if data_type == "USER-DEFINED":
            # source enum -> text -> target-schema enum (labels are identical)
            select_exprs.append(f'"{name}"::text::"{schema}"."{udt_name}"')
        else:
            select_exprs.append(f'"{name}"')
    return ", ".join(insert_cols), ", ".join(select_exprs)


def discover_sources(conn, target: str) -> list[str]:
    """Active tenant schemas (accounting_ops_<uuid>) that still exist, != target.

    After the primary is renamed to `books`, its old schema no longer exists, so
    it is excluded automatically; the archived tenant is excluded by status.
    """
    rows = conn.execute(
        text(
            """
            SELECT n.nspname
            FROM pg_namespace n
            JOIN public.workspace w ON w.postgres_schema = n.nspname
            WHERE n.nspname LIKE 'accounting\\_ops\\_%%'
              AND w.status = 'active'
              AND n.nspname <> :target
            ORDER BY n.nspname
            """
        ),
        {"target": target},
    ).all()
    return [r[0] for r in rows]


def consolidate(target: str, sources: list[str], dry_run: bool) -> None:
    engine = _sync_engine()
    with engine.connect() as conn:
        target_tables = _scoped_tables(conn, target)
        if not target_tables:
            raise RuntimeError(f"Target schema {target!r} has no scoped tables.")
        deps = _fk_parents(conn, target)
        order = _topo_order(target_tables, deps)
        print(f"target={target}  scoped_tables={len(order)}")
        print(f"sources={sources}")
        if dry_run:
            print("copy order (parents first):")
            for t in order:
                print(f"  {t}")
            print("DRY RUN — no rows copied.")
            return

    for src in sources:
        with engine.begin() as conn:
            print(f"\n=== copying {src} -> {target} ===")
            conn.execute(
                text(
                    f'ALTER TABLE "{target}".journal_entry '
                    f"DISABLE TRIGGER {_JE_DERIVE_TRIGGER}"
                )
            )
            try:
                for t in order:
                    insert_cols, select_exprs = _copy_columns(conn, target, t)
                    res = conn.execute(
                        text(
                            f'INSERT INTO "{target}"."{t}" ({insert_cols}) '
                            f'SELECT {select_exprs} FROM "{src}"."{t}"'
                        )
                    )
                    print(f"  {t}: +{res.rowcount}")
            finally:
                conn.execute(
                    text(
                        f'ALTER TABLE "{target}".journal_entry '
                        f"ENABLE TRIGGER {_JE_DERIVE_TRIGGER}"
                    )
                )
            print(f"=== {src} committed ===")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", default=SHARED_SCHEMA, help="Consolidated schema (default: books)")
    ap.add_argument(
        "--source",
        action="append",
        default=[],
        help="Source tenant schema to drain into target (repeatable). "
        "If omitted, auto-discovers active accounting_ops_<uuid> schemas.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print plan; copy nothing.")
    args = ap.parse_args()

    engine = _sync_engine()
    sources = list(args.source)
    if not sources:
        with engine.connect() as conn:
            sources = discover_sources(conn, args.target)
    if not sources and not args.dry_run:
        print("No source schemas to consolidate (nothing to do).")
        return
    consolidate(args.target, sources, args.dry_run)


if __name__ == "__main__":
    main()
