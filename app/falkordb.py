"""FalkorDB client for Brigade-federated agent memory.

Workspace + entity isolation in the graph:
- One subgraph per WORKSPACE on the Brigade FalkorDB on bigboy
  (192.168.10.10:6379). Subgraph name: `workspace_<workspace_uuid_underscored>`.
- Entity is encoded as a property on every node + a labeled relationship,
  letting us scope graph queries to a single entity within a workspace.

Node types (Graphiti-aware temporal validity):
- Workspace            — the workspace metadata node
- Entity               — legal entity within a workspace
- Vendor               — workspace-shared counterparty registry
- Counterparty         — bank deposit sources, payout recipients (workspace-shared)
- CategorizationRule   — per-entity chart-of-accounts mapping w/ temporal intervals
- ComplianceRule       — specialty rules (FERS / 1099 / nexus / etc.)
- Decision             — every agent decision with reasoning trace
- JournalEntry         — immutable ledger entries (mirror of Postgres)
- SourceDocument       — receipts, invoices, statements (content-hash addressed)
- Exception            — human-overrides + learned exceptions
- Payout               — eBay, Stripe, etc. payout objects + recon links
- ReconLink            — many-to-many between journal entries and source events
- AgentSession         — session boundary for grouping decisions

Relationships:
- (Entity)-[:BELONGS_TO]->(Workspace)
- (JournalEntry)-[:FOR_ENTITY]->(Entity)
- (JournalEntry)-[:SUPPORTED_BY]->(SourceDocument)
- (Decision)-[:OVERRIDES]->(Decision)
- (Decision)-[:APPLIED_RULE]->(ComplianceRule)
- (Vendor)-[:LEARNED_PATTERN]->(CategorizationRule)
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


def subgraph_for_workspace(workspace_id: str | uuid.UUID) -> str:
    """Compute the FalkorDB subgraph name for a workspace.

    Matches the Postgres schema convention so workspace identity is consistent
    across stores.
    """
    return f"workspace_{str(workspace_id).replace('-', '_')}"


# Backwards-compat alias.
subgraph_for_tenant = subgraph_for_workspace


@asynccontextmanager
async def falkordb_session(
    workspace_id: str | uuid.UUID,
) -> AsyncIterator[Any]:
    """Open a FalkorDB connection scoped to the workspace's subgraph.

    Usage:
        async with falkordb_session(workspace_id) as graph:
            result = await graph.query(
                "MATCH (v:Vendor)-[:LEARNED_PATTERN]->(r:CategorizationRule) "
                "WHERE v.name = $name RETURN r LIMIT 1",
                params={"name": "Anthropic, PBC"}
            )
    """
    if FalkorDB is None:
        raise RuntimeError(
            "falkordb-py is not installed. Run `pip install falkordb` or check pyproject.toml"
        )
    settings = get_settings()
    db = FalkorDB(host=settings.falkordb_host, port=settings.falkordb_port)
    graph_name = subgraph_for_workspace(workspace_id)
    graph = db.select_graph(graph_name)
    try:
        yield graph
    finally:
        # FalkorDB asyncio client doesn't require explicit close;
        # connections come from a pool managed by the redis-py driver.
        pass


async def ensure_workspace_subgraph(workspace_id: str | uuid.UUID) -> str:
    """Idempotent: ensure the workspace's subgraph exists with required constraints.

    Creates uniqueness constraints on entity primary keys + entity_id property
    indexes for fast scoping. Called at workspace provisioning time.
    """
    if FalkorDB is None:
        return subgraph_for_workspace(workspace_id)
    async with falkordb_session(workspace_id) as graph:
        for label, prop in (
            ("Workspace", "id"),
            ("Entity", "id"),
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
            ("AgentSession", "id"),
        ):
            try:
                await graph.query(
                    f"CREATE CONSTRAINT FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
                )
            except Exception:
                pass
        # Index entity_id on all entity-scoped node types for fast scoping
        for label in (
            "ComplianceRule",
            "Decision",
            "JournalEntry",
            "Exception",
            "Payout",
            "ReconLink",
            "AgentSession",
        ):
            try:
                await graph.query(f"CREATE INDEX FOR (n:{label}) ON (n.entity_id)")
            except Exception:
                pass
    return subgraph_for_workspace(workspace_id)


# Common query helpers — all entity-scoped within a workspace -------------


async def get_active_compliance_rules(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    at_time: str | None = None,
) -> list[dict]:
    """Return ComplianceRule nodes active for this entity at the given time."""
    async with falkordb_session(workspace_id) as graph:
        result = await graph.query(
            """
            MATCH (r:ComplianceRule)
            WHERE r.entity_id = $entity_id
              AND r.effective_from <= COALESCE($at, datetime())
              AND (r.effective_to IS NULL OR r.effective_to > COALESCE($at, datetime()))
            RETURN r
            ORDER BY r.kind
            """,
            params={"entity_id": str(entity_id), "at": at_time},
        )
        return [dict(row[0]) for row in result.result_set or []]


async def record_decision(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    *,
    decision_id: str,
    agent_id: str,
    verdict: str,
    reasoning: str,
    rule_version: str | None = None,
    model_version: str | None = None,
    parent_run_id: str | None = None,
) -> None:
    """Append a Decision node to the workspace subgraph, scoped to entity.

    Decisions are immutable. Corrections create new Decision nodes with
    OVERRIDES relationships to the previous decision.
    """
    async with falkordb_session(workspace_id) as graph:
        await graph.query(
            """
            CREATE (d:Decision {
                id: $id,
                entity_id: $entity_id,
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
                "entity_id": str(entity_id),
                "agent_id": agent_id,
                "verdict": verdict,
                "reasoning": reasoning,
                "rule_version": rule_version,
                "model_version": model_version,
                "parent_run_id": parent_run_id,
            },
        )


async def link_journal_entry_to_source(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    journal_entry_id: str,
    source_document_hash: str,
) -> None:
    """Materialize the (JournalEntry)-[:SUPPORTED_BY]->(SourceDocument) edge.

    Closes the IRS purchase/payment/purpose triangle at write time.
    """
    async with falkordb_session(workspace_id) as graph:
        await graph.query(
            """
            MERGE (j:JournalEntry {id: $je_id})
            ON CREATE SET j.entity_id = $entity_id
            MERGE (s:SourceDocument {content_hash: $hash})
            ON CREATE SET s.entity_id = $entity_id
            MERGE (j)-[:SUPPORTED_BY]->(s)
            """,
            params={
                "je_id": journal_entry_id,
                "entity_id": str(entity_id),
                "hash": source_document_hash,
            },
        )
