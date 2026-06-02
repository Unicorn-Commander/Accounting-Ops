"""Backfill the FalkorDB agent-memory graph from authoritative Postgres state.

The graph is a PROJECTION of Postgres (see app/falkordb.py). This script
(re)builds that projection for existing data: for each ACTIVE workspace it
ensures the workspace subgraph + constraints exist, then idempotently replays
the authoritative rows into the graph via the SAME MERGE-based helpers the
runtime write-side projection uses:

  * journal_entry   -> (JournalEntry) nodes (+ FOR_ENTITY, REVERSES, and
                        SUPPORTED_BY edges where a source-document hash exists)
  * decision_audit  -> (Decision) nodes
  * compliance_rule -> (ComplianceRule) nodes (with effective_from/effective_to
                        so get_active_compliance_rules works), active mirrored
                        from the rule's compliance_profile.active

Because every helper is MERGE/upsert, this is SAFE TO RUN REPEATEDLY — a second
run converges instead of duplicating. Postgres stays authoritative throughout;
this only writes to FalkorDB, never to Postgres.

Unlike the runtime path (which fires projections best-effort via
``falkordb.project`` so they never add request latency), the backfill AWAITS
each projection directly inside its own event loop so the per-workspace counts
it prints reflect work that actually completed. A projection failure for one
workspace is logged and the script proceeds to the next workspace (and exits
non-zero) — it never leaves Postgres in a bad state because it never writes PG.

Usage:
    # All active workspaces (default):
    python -m scripts.backfill_falkordb

    # One or more specific workspaces:
    python -m scripts.backfill_falkordb <workspace_uuid> [<workspace_uuid> ...]

    # Include non-active workspaces too:
    python -m scripts.backfill_falkordb --all-statuses

Requires the same env the app uses (DATABASE_URL / ADMIN_DATABASE_URL,
FALKORDB_HOST / FALKORDB_PORT). Reads Postgres with the admin (owner) engine so
RLS never hides rows during the rebuild.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import falkordb  # noqa: E402
from app.config import get_admin_database_url  # noqa: E402
from app.workspace_session import workspace_session  # noqa: E402

log = logging.getLogger("backfill_falkordb")


# Read Postgres as the table-OWNING role so RLS doesn't hide rows during the
# rebuild (the runtime least-privilege role IS RLS-gated; the backfill is an
# admin/maintenance operation, mirroring scripts/provision_workspace.py).
@lru_cache
def _admin_engine() -> AsyncEngine:
    return create_async_engine(get_admin_database_url(), pool_pre_ping=True)


@lru_cache
def _admin_sessionmaker() -> async_sessionmaker:
    return async_sessionmaker(_admin_engine(), expire_on_commit=False)


@dataclass
class WorkspaceCounts:
    workspace_id: str
    journal_entries: int = 0
    supported_by_links: int = 0
    decisions: int = 0
    compliance_rules: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        base = (
            f"  workspace {self.workspace_id}: "
            f"journal_entries={self.journal_entries} "
            f"supported_by_links={self.supported_by_links} "
            f"decisions={self.decisions} "
            f"compliance_rules={self.compliance_rules}"
        )
        if self.errors:
            base += f"  ERRORS={len(self.errors)}"
        return base


async def _list_workspaces(*, all_statuses: bool) -> list[str]:
    """Return workspace ids to backfill (active-only unless --all-statuses)."""
    where = "" if all_statuses else "WHERE status = 'active'"
    async with _admin_sessionmaker()() as session:
        async with session.begin():
            rows = (
                await session.execute(
                    text(f"SELECT id::text AS id FROM public.workspace {where} ORDER BY created_at")
                )
            ).mappings().all()
    return [r["id"] for r in rows]


async def _backfill_journal_entries(session, workspace_id: str, counts: WorkspaceCounts) -> None:
    """Replay journal_entry rows into (JournalEntry) nodes + their edges.

    Only a curated, non-PII subset of columns is projected (entity/category/
    amount/dates/lineage/source_document_hash) — matching the runtime
    upsert_journal_entry projection. source_document_hash carries the per-entry
    SourceDocument identity used to MERGE the SUPPORTED_BY edge (skipping the
    chain-hash-hex placeholder the v2 schema stores when there is no real
    source document: it is never a real receipt content hash).
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, entity_id::text AS entity_id,
                       category,
                       (amount * 100)::bigint AS amount_cents,
                       effective_date::text AS effective_date,
                       source, source_external_id, payee,
                       source_document_hash,
                       source_document_id::text AS source_document_id,
                       reverses_entry_id::text AS reverses_entry_id
                FROM journal_entry
                ORDER BY posted_at ASC, id ASC
                """
            )
        )
    ).mappings().all()
    for r in rows:
        await falkordb.upsert_journal_entry(
            workspace_id,
            r["entity_id"],
            r["id"],
            category=r["category"],
            amount_cents=int(r["amount_cents"]) if r["amount_cents"] is not None else None,
            effective_date=r["effective_date"],
            source=r["source"],
            source_external_id=r["source_external_id"],
            payee=r["payee"],
            reverses_entry_id=r["reverses_entry_id"],
        )
        counts.journal_entries += 1
        # Only link a SourceDocument when there is a REAL source document on the
        # row. The schema stores the chain-hash hex in source_document_hash when
        # source_document_id is NULL (no receipt) — that is not a content hash,
        # so we gate on source_document_id being present.
        sdh = r["source_document_hash"]
        if r["source_document_id"] and sdh:
            await falkordb.link_journal_entry_to_source(
                workspace_id, r["entity_id"], r["id"], sdh
            )
            counts.supported_by_links += 1


async def _backfill_decisions(session, workspace_id: str, counts: WorkspaceCounts) -> None:
    """Replay decision_audit rows into (Decision) nodes.

    Maps the audit row to the Decision schema the same way the runtime
    projection (project_decision_audit) does: decision id = audit id, verdict =
    outcome, agent_id/model_version = the row's model_version (the agent id the
    writer stamped), subject_id pulled from request_payload->>'id' when present
    (the recategorize/categorize/link tools store the subject entry id there).
    OVERRIDES edges are intentionally NOT derived in the backfill (ordering of a
    bulk replay would make "latest prior" ambiguous); the runtime path wires
    them going forward.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, entity_id::text AS entity_id,
                       tool_name, outcome, reasoning_trace,
                       model_version,
                       request_payload->>'id' AS subject_id,
                       rule_version_applied::text AS rule_version
                FROM decision_audit
                ORDER BY created_at ASC, id ASC
                """
            )
        )
    ).mappings().all()
    for r in rows:
        await falkordb.record_decision(
            workspace_id,
            r["entity_id"],
            decision_id=r["id"],
            agent_id=r["model_version"] or r["tool_name"] or "unknown",
            verdict=r["outcome"] or "",
            reasoning=r["reasoning_trace"] or "",
            rule_version=r["rule_version"],
            model_version=r["model_version"],
            subject_id=r["subject_id"],
        )
        counts.decisions += 1


async def _backfill_compliance_rules(session, workspace_id: str, counts: WorkspaceCounts) -> None:
    """Replay compliance_rule rows into (ComplianceRule) nodes.

    ``active`` mirrors the rule's compliance_profile.active so the temporal read
    (get_active_compliance_rules) honors the same proposed-vs-approved gate the
    Postgres query does. effective_from/effective_to are projected as ISO
    strings; the helper wraps them in datetime() for the temporal comparison.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT r.id::text AS id, r.entity_id::text AS entity_id,
                       r.kind,
                       r.effective_from::text AS effective_from,
                       r.effective_to::text AS effective_to,
                       CAST(r.trigger AS text) AS trigger,
                       CAST(r.action AS text) AS action,
                       CAST(r.source AS text) AS source,
                       p.active AS active
                FROM compliance_rule r
                JOIN compliance_profile p ON p.id = r.profile_id
                ORDER BY r.effective_from ASC, r.id ASC
                """
            )
        )
    ).mappings().all()
    for r in rows:
        await falkordb.upsert_compliance_rule(
            workspace_id,
            r["entity_id"],
            r["id"],
            kind=r["kind"],
            effective_from=r["effective_from"],
            effective_to=r["effective_to"],
            trigger=r["trigger"],
            action=r["action"],
            source=r["source"],
            active=bool(r["active"]),
        )
        counts.compliance_rules += 1


async def backfill_workspace(workspace_id: str) -> WorkspaceCounts:
    """Ensure the subgraph + replay all three row types for one workspace."""
    counts = WorkspaceCounts(workspace_id=workspace_id)
    if falkordb.FalkorDB is None:
        counts.errors.append("falkordb-py not installed; nothing projected")
        return counts

    # Idempotent: constraints + indexes.
    try:
        await falkordb.ensure_workspace_subgraph(workspace_id)
    except Exception as exc:  # noqa: BLE001
        counts.errors.append(f"ensure_workspace_subgraph: {exc!r}")
        log.warning("ensure_workspace_subgraph failed for %s: %s", workspace_id, exc)
        return counts

    # Read Postgres in ONE workspace-scoped transaction (admin engine -> no RLS
    # hiding), then project. Each section is independently guarded so a failure
    # in one row type doesn't abort the others.
    async with workspace_session(_admin_sessionmaker(), workspace_id) as session:
        for name, fn in (
            ("journal_entry", _backfill_journal_entries),
            ("decision_audit", _backfill_decisions),
            ("compliance_rule", _backfill_compliance_rules),
        ):
            try:
                await fn(session, workspace_id, counts)
            except Exception as exc:  # noqa: BLE001 - record + continue
                counts.errors.append(f"{name}: {exc!r}")
                log.warning("backfill %s failed for %s: %s", name, workspace_id, exc)
    return counts


async def _amain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Backfill FalkorDB from Postgres.")
    parser.add_argument(
        "workspace_ids",
        nargs="*",
        help="Specific workspace UUIDs to backfill. Default: all active workspaces.",
    )
    parser.add_argument(
        "--all-statuses",
        action="store_true",
        help="Include non-active workspaces when no explicit ids are given.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.workspace_ids:
        workspace_ids = args.workspace_ids
    else:
        workspace_ids = await _list_workspaces(all_statuses=args.all_statuses)

    if not workspace_ids:
        print("No workspaces to backfill.")
        return 0

    if falkordb.FalkorDB is None:
        print("WARNING: falkordb-py is not installed — nothing will be projected.")

    print(f"Backfilling {len(workspace_ids)} workspace(s) into FalkorDB...")
    all_counts: list[WorkspaceCounts] = []
    for wid in workspace_ids:
        counts = await backfill_workspace(wid)
        all_counts.append(counts)
        print(counts.summary())
        for err in counts.errors:
            print(f"    ! {err}")

    totals = WorkspaceCounts(workspace_id="TOTAL")
    for c in all_counts:
        totals.journal_entries += c.journal_entries
        totals.supported_by_links += c.supported_by_links
        totals.decisions += c.decisions
        totals.compliance_rules += c.compliance_rules
        totals.errors.extend(c.errors)
    print("---")
    print(totals.summary())

    # Non-zero exit if any workspace had an error, so CI/cron can alert.
    return 1 if totals.errors else 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))


if __name__ == "__main__":
    main()
