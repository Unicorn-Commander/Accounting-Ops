"""FalkorDB client for Brigade-federated agent memory.

Connects to the Brigade FalkorDB instance running on bigboy (192.168.10.10:6379)
over Tailscale. Per-tenant subgraph isolation: every query is scoped to the
tenant's subgraph name (computed from tenant_id).

Node types (Graphiti-aware temporal validity):
- Vendor          — counterparty registry, learned over time
- Counterparty    — bank deposit sources, payout recipients
- CategorizationRule — per-tenant chart-of-accounts mapping with effective intervals
- ComplianceRule  — specialty / FERS / 1099-NEC / nexus / etc. rules with temporal validity
- Decision        — every agent decision with reasoning trace
- JournalEntry    — immutable ledger entries (mirror of Postgres)
- SourceDocument  — receipts, invoices, bank statements (content-hash addressed)
- Exception       — human-overrides + learned exceptions
- Payout          — eBay, Stripe, etc. payout objects + recon links
- ReconLink       — many-to-many between journal entries and source events
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

try:
    from falkordb.asyncio import FalkorDB
except ImportError:  # pragma: no cover - exercised only without falkordb-py installed
    FalkorDB = None  # type: ignore[assignment]

from app.config import get_settings


def subgraph_for_tenant(tenant_id: str | uuid.UUID) -> str:
    """Compute the FalkorDB subgraph name for a tenant. Matches the schema convention."""
    return f"tenant_{str(tenant_id).replace('-', '_')}"


@asynccontextmanager
async def falkordb_session(
    tenant_id: str | uuid.UUID,
) -> AsyncIterator[Any]:
    """Open a FalkorDB connection scoped to the tenant's subgraph.

    Usage:
        async with falkordb_session(tenant_id) as graph:
            result = await graph.query("MATCH (v:Vendor) RETURN v LIMIT 10")
    """
    if FalkorDB is None:
        raise RuntimeError(
            "falkordb-py is not installed. Run `pip install falkordb` or check pyproject.toml"
        )
    settings = get_settings()
    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    graph_name = subgraph_for_tenant(tenant_id)
    graph = db.select_graph(graph_name)
    try:
        yield graph
    finally:
        # FalkorDB asyncio client doesn't require explicit close;
        # connections come from a pool managed by the redis-py driver.
        pass


async def ensure_tenant_subgraph(tenant_id: str | uuid.UUID) -> str:
    """Idempotent: ensure the tenant's subgraph exists with required constraints.

    Creates uniqueness constraints on entity primary keys so duplicate inserts
    don't proliferate. Called from scripts/provision_tenant.py at tenant creation.
    """
    if FalkorDB is None:
        return subgraph_for_tenant(tenant_id)
    async with falkordb_session(tenant_id) as graph:
        # Constraints — idempotent, no-ops if already present
        for label, prop in (
            ("Vendor", "id"),
            ("Counterparty", "id"),
            ("CategorizationRule", "id"),
            ("ComplianceRule", "id"),
            ("Decision", "id"),
            ("JournalEntry", "id"),
            ("SourceDocument", "content_hash"),
            ("Exception", "id"),
            ("Payout", "id"),
            ("ReconLink", "id"),
        ):
            try:
                await graph.query(
                    f"CREATE CONSTRAINT FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
                )
            except Exception:
                # Constraint already exists or FalkorDB version differs; safe to ignore
                pass
    return subgraph_for_tenant(tenant_id)


# Common query helpers -----------------------------------------------------


async def get_active_compliance_rules(
    tenant_id: str | uuid.UUID, at_time: str | None = None
) -> list[dict]:
    """Return ComplianceRule nodes active at the given time (default: now).

    Rules have effective_from / effective_to temporal validity intervals
    (Graphiti pattern). A rule is active if effective_from <= at_time AND
    (effective_to IS NULL OR effective_to > at_time).
    """
    async with falkordb_session(tenant_id) as graph:
        result = await graph.query(
            """
            MATCH (r:ComplianceRule)
            WHERE r.effective_from <= COALESCE($at, datetime())
              AND (r.effective_to IS NULL OR r.effective_to > COALESCE($at, datetime()))
            RETURN r
            ORDER BY r.kind
            """,
            params={"at": at_time},
        )
        return [dict(row[0]) for row in result.result_set or []]


async def record_decision(
    tenant_id: str | uuid.UUID,
    *,
    decision_id: str,
    agent_id: str,
    verdict: str,
    reasoning: str,
    rule_version: str | None = None,
    model_version: str | None = None,
    parent_run_id: str | None = None,
) -> None:
    """Append a Decision node to the tenant's subgraph.

    Decisions are immutable. Corrections create new Decision nodes with
    OVERRIDES relationships to the previous decision.
    """
    async with falkordb_session(tenant_id) as graph:
        await graph.query(
            """
            CREATE (d:Decision {
                id: $id,
                agent_id: $agent_id,
                verdict: $verdict,
                reasoning: $reasoning,
                rule_version: $rule_version,
                model_version: $model_version,
                parent_run_id: $parent_run_id,
                created_at: datetime()
            })
            """,
            params={
                "id": decision_id,
                "agent_id": agent_id,
                "verdict": verdict,
                "reasoning": reasoning,
                "rule_version": rule_version,
                "model_version": model_version,
                "parent_run_id": parent_run_id,
            },
        )
