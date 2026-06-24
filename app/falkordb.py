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
- (JournalEntry)-[:REVERSES]->(JournalEntry)   — recategorization reversal lineage
- (Decision)-[:OVERRIDES]->(Decision)
- (Decision)-[:APPLIED_RULE]->(ComplianceRule)
- (Vendor)-[:LEARNED_PATTERN]->(CategorizationRule)

Projection contract (see ``project()`` below): this module is a BEST-EFFORT,
ASYNCHRONOUS projection of the authoritative Postgres state. Postgres is the
source of truth; every write here is idempotent (MERGE/upsert) so a row can be
replayed (backfill) or a fire-and-forget projection retried without diverging.
A failure here is logged and swallowed — it never blocks a Postgres write.

CategorizationRule carries a ``confirmations`` counter that increments each time
a human re-affirms a vendor→category mapping, so the learned-pattern read can
rank by evidence.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable

try:
    from falkordb.asyncio import FalkorDB
except ImportError:  # pragma: no cover - exercised only without falkordb-py installed
    FalkorDB = None  # type: ignore[assignment]

from app.config import get_settings

_log = logging.getLogger("acctops.falkordb")


def _iso_to_ms(value: str | None) -> int | None:
    """ISO-8601 (anything ``datetime.fromisoformat`` accepts) -> epoch milliseconds, else None.

    FalkorDB build 41801 has no ``datetime()`` Cypher function, so temporal values
    are stored as integer epoch milliseconds and compared against ``timestamp()``.
    """
    if not value:
        return None
    try:
        from datetime import datetime as _dt

        return int(_dt.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Projection guard: graph is a PROJECTION of Postgres, never authoritative.
#
# The runtime treats FalkorDB as a best-effort, asynchronous mirror of the
# authoritative Postgres writes. A FalkorDB failure (graph down, network blip,
# falkordb-py missing) must NEVER block, delay, or fail a Postgres write. Every
# projection call site goes through ``project()`` so:
#   * it runs AFTER the authoritative commit (the caller awaits/commits PG first),
#   * it is fire-and-forget (scheduled on the loop) so it adds ZERO latency,
#   * any exception is swallowed + logged, never propagated to the request.
# ---------------------------------------------------------------------------

# Keep strong refs to in-flight projection tasks so the event loop doesn't GC
# them mid-flight (asyncio only holds weak refs to tasks). Tasks remove
# themselves on completion.
_PENDING_PROJECTIONS: set[asyncio.Task] = set()


async def _run_projection(coro: Awaitable[Any], *, label: str) -> None:
    """Await a projection coroutine, swallowing + logging any failure.

    The graph is non-authoritative: a failure here is logged at WARNING and
    discarded so the (already-committed) Postgres write is never affected.
    """
    try:
        await coro
    except Exception as exc:  # noqa: BLE001 - projection must never raise to caller
        _log.warning(
            "falkordb projection failed (non-authoritative, ignored): %s: %s",
            label,
            exc.__class__.__name__,
        )
        _log.debug("falkordb projection traceback for %s", label, exc_info=True)


def project(coro: Awaitable[Any], *, label: str = "projection") -> None:
    """Fire-and-forget a best-effort FalkorDB projection.

    THE single choke point every write-side projection goes through. Schedules
    ``coro`` on the running event loop and returns immediately, so projection
    adds no latency to the authoritative path. Exceptions are caught + logged in
    ``_run_projection`` — they never reach the caller.

    If falkordb-py isn't installed, or there is no running event loop (e.g. a
    synchronous CLI context), the coroutine is closed and the call is a no-op —
    again, never raising. Call this ONLY after the authoritative Postgres write
    has committed.
    """
    if FalkorDB is None:
        # No graph client available — close the coroutine to avoid a
        # "coroutine was never awaited" warning and no-op.
        if asyncio.iscoroutine(coro):
            coro.close()
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop (sync caller). Projection is best-effort; skip it
        # rather than spin up a loop (that would add latency + risk). Backfill
        # / scripts that want synchronous projection should await the helper
        # directly inside their own event loop instead of using project().
        if asyncio.iscoroutine(coro):
            coro.close()
        return
    task = loop.create_task(_run_projection(coro, label=label))
    _PENDING_PROJECTIONS.add(task)
    task.add_done_callback(_PENDING_PROJECTIONS.discard)


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
    db = FalkorDB(
        host=settings.falkordb_host,
        port=settings.falkordb_port,
        password=settings.falkordb_password or None,
    )
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
        # Index entity_id on all entity-scoped node types for fast scoping.
        # Vendor + CategorizationRule are added so the learned-pattern read path
        # (get_learned_category_for_vendor) and the LEARN write
        # (record_learned_pattern, which MERGEs Vendor by (entity_id, name))
        # scope efficiently within the workspace.
        for label in (
            "Vendor",
            "CategorizationRule",
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


def _node_props(node: object) -> dict:
    """Coerce a FalkorDB result node to a plain dict of its properties.

    This client build returns ``Node`` objects that are NOT directly
    ``dict()``-able (``dict(node)`` raises ``'Node' object is not iterable``);
    their fields live on ``node.properties``. Fall back to ``dict(node)`` for
    any build/shape that already supports mapping coercion."""
    props = getattr(node, "properties", None)
    return dict(props) if props is not None else dict(node)


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
              AND r.effective_from <= COALESCE($at, timestamp())
              AND (r.effective_to IS NULL OR r.effective_to > COALESCE($at, timestamp()))
            RETURN r
            ORDER BY r.kind
            """,
            params={"entity_id": str(entity_id), "at": _iso_to_ms(at_time)},
        )
        return [_node_props(row[0]) for row in result.result_set or []]


async def recent_decisions(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    limit: int = 10,
) -> list[dict]:
    """Return the most recent Decision nodes for an entity, newest first.

    Read-side companion to ``record_decision``. The CFO chat persona reads this
    so it can speak to "what we decided and why" — and, via the OVERRIDES edges,
    "what changed" (a recategorization or correction supersedes the prior call).

    Each returned dict carries the decision's own fields plus its OVERRIDES
    lineage in BOTH directions:

      * ``overrides`` — the verdict/reasoning of the prior Decision THIS one
        supersedes (``(d)-[:OVERRIDES]->(prev)``), or ``None``. Lets the agent
        narrate the change ("we moved X from category A to B because …").
      * ``superseded_by_id`` — the id of a LATER Decision that overrides THIS
        one (``(newer)-[:OVERRIDES]->(d)``), or ``None``. Lets the agent avoid
        speaking to a stale call as if it were current.

    Entity-scoped within the workspace subgraph: only Decisions carrying this
    ``entity_id`` are returned, so a decision never crosses entities (and never
    crosses workspaces — the subgraph is workspace-scoped).

    Best-effort + non-authoritative: Postgres ``decision_audit`` is the source
    of truth. The caller MUST treat an empty list (and any raised exception,
    which it should catch) as "no signal" and behave exactly as it would without
    this read.
    """
    limit = max(1, min(int(limit), 50))
    async with falkordb_session(workspace_id) as graph:
        result = await graph.query(
            """
            MATCH (d:Decision)
            WHERE d.entity_id = $entity_id
            OPTIONAL MATCH (d)-[:OVERRIDES]->(prev:Decision)
            OPTIONAL MATCH (newer:Decision)-[:OVERRIDES]->(d)
            RETURN d.id AS id,
                   d.agent_id AS agent_id,
                   d.verdict AS verdict,
                   d.reasoning AS reasoning,
                   d.subject_id AS subject_id,
                   d.rule_version AS rule_version,
                   d.created_at AS created_at,
                   prev.id AS overrides_id,
                   prev.verdict AS overrides_verdict,
                   prev.reasoning AS overrides_reasoning,
                   newer.id AS superseded_by_id
            ORDER BY d.created_at DESC
            LIMIT $limit
            """,
            params={"entity_id": str(entity_id), "limit": limit},
        )
        rows = result.result_set or []
        out: list[dict] = []
        seen: set = set()
        for row in rows:
            (
                d_id,
                agent_id,
                verdict,
                reasoning,
                subject_id,
                rule_version,
                created_at,
                overrides_id,
                overrides_verdict,
                overrides_reasoning,
                superseded_by_id,
            ) = row
            # The two OPTIONAL MATCH clauses can fan a single Decision out into
            # multiple rows if it overrides / is overridden by more than one
            # (rare — OVERRIDES is 1:1 in practice). Keep the first row per
            # decision id so LIMIT counts distinct decisions and lineage is
            # deterministic (newest-first thanks to the ORDER BY).
            if d_id in seen:
                continue
            seen.add(d_id)
            overrides = (
                {
                    "id": overrides_id,
                    "verdict": overrides_verdict,
                    "reasoning": overrides_reasoning,
                }
                if overrides_id is not None
                else None
            )
            out.append(
                {
                    "id": d_id,
                    "agent_id": agent_id,
                    "verdict": verdict,
                    "reasoning": reasoning,
                    "subject_id": subject_id,
                    "rule_version": rule_version,
                    "created_at": created_at,
                    "overrides": overrides,
                    "superseded_by_id": superseded_by_id,
                }
            )
        return out


async def notable_learned_patterns(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    limit: int = 10,
) -> list[dict]:
    """Return the highest-evidence learned vendor→category patterns for an entity.

    Read-side companion to ``record_learned_pattern``. Ranks by the
    ``confirmations`` counter (how many times a human re-affirmed the mapping)
    so the CFO chat persona can speak to the entity's established categorization
    habits ("Anthropic is consistently booked to SaaS · AI — 4 confirmations").

    Entity-scoped within the workspace subgraph. Returns
    ``[{"vendor", "category", "confirmations"}, ...]`` newest-evidence first;
    an empty list when there are no learned patterns. Best-effort — the caller
    catches + degrades on any exception.
    """
    limit = max(1, min(int(limit), 50))
    async with falkordb_session(workspace_id) as graph:
        result = await graph.query(
            """
            MATCH (v:Vendor)-[:LEARNED_PATTERN]->(r:CategorizationRule)
            WHERE v.entity_id = $entity_id
              AND r.entity_id = $entity_id
            RETURN v.name AS vendor,
                   r.category AS category,
                   COALESCE(r.confirmations, 1) AS confirmations
            ORDER BY confirmations DESC, v.name ASC
            LIMIT $limit
            """,
            params={"entity_id": str(entity_id), "limit": limit},
        )
        out: list[dict] = []
        for row in result.result_set or []:
            vendor, category, confirmations = row
            if not vendor or not category:
                continue
            out.append(
                {
                    "vendor": vendor,
                    "category": category,
                    "confirmations": int(confirmations) if confirmations is not None else 1,
                }
            )
        return out


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
    subject_id: str | None = None,
    overrides_decision_id: str | None = None,
    overrides_subject_id: str | None = None,
) -> None:
    """Append a Decision node to the workspace subgraph, scoped to entity.

    Decisions are immutable. Corrections create new Decision nodes with
    OVERRIDES relationships to the previous decision.

    MERGE (not CREATE) on the Decision id keeps the projection idempotent: the
    same row replayed by the backfill, or a retried fire-and-forget projection,
    converges instead of erroring on the uniqueness constraint. The
    authoritative ``decision_audit`` row in Postgres is the source of truth; this
    is its mirror.

    ``subject_id``: the id of the thing the decision is ABOUT (e.g. the
    journal_entry being recategorized). Stored on the node so prior decisions for
    the same subject can be found to wire the OVERRIDES edge.

    ``overrides_decision_id``: when this decision corrects/supersedes a known
    prior one, pass the prior Decision's id to materialize the documented
    ``(Decision)-[:OVERRIDES]->(Decision)`` edge. The prior node is MERGE-created
    if the projection hasn't seen it yet, so edge creation never depends on
    projection ordering. ``parent_run_id`` is also stored on the node for
    lineage; the two are independent (parent_run_id is a Brigade run pointer,
    OVERRIDES is decision-to-decision supersession).

    ``overrides_subject_id``: when the explicit prior id isn't known but this
    decision supersedes the most recent PRIOR decision about the same subject
    (the common recategorization case — Postgres doesn't link decision_audit rows
    to each other, but the graph can), pass the subject id. We then OVERRIDES the
    latest other Decision carrying that ``subject_id`` for this entity. Resolved
    entirely in-graph (no Postgres read); a no-op when no prior decision exists.
    """
    async with falkordb_session(workspace_id) as graph:
        await graph.query(
            """
            MERGE (d:Decision {id: $id})
            ON CREATE SET
                d.entity_id = $entity_id,
                d.agent_id = $agent_id,
                d.verdict = $verdict,
                d.reasoning = $reasoning,
                d.rule_version = $rule_version,
                d.model_version = $model_version,
                d.parent_run_id = $parent_run_id,
                d.subject_id = $subject_id,
                d.created_at = timestamp()
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
                "subject_id": str(subject_id) if subject_id is not None else None,
            },
        )
        if overrides_decision_id:
            # Materialize (this Decision)-[:OVERRIDES]->(prior Decision). MERGE
            # the prior node so the edge can be created regardless of whether
            # its own projection has landed yet (eventual consistency).
            await graph.query(
                """
                MERGE (d:Decision {id: $id})
                MERGE (prev:Decision {id: $prev_id})
                ON CREATE SET prev.entity_id = $entity_id
                MERGE (d)-[:OVERRIDES]->(prev)
                """,
                params={
                    "id": decision_id,
                    "prev_id": overrides_decision_id,
                    "entity_id": str(entity_id),
                },
            )
        elif overrides_subject_id:
            # Derive the prior decision from the graph: the most recent OTHER
            # Decision about the same subject for this entity. Idempotent — the
            # MERGE on the edge means a replay doesn't duplicate it.
            await graph.query(
                """
                MATCH (prev:Decision)
                WHERE prev.entity_id = $entity_id
                  AND prev.subject_id = $subject_id
                  AND prev.id <> $id
                WITH prev ORDER BY prev.created_at DESC LIMIT 1
                MATCH (d:Decision {id: $id})
                MERGE (d)-[:OVERRIDES]->(prev)
                """,
                params={
                    "id": decision_id,
                    "subject_id": str(overrides_subject_id),
                    "entity_id": str(entity_id),
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


async def upsert_journal_entry(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    journal_entry_id: str,
    *,
    category: str | None = None,
    amount_cents: int | None = None,
    effective_date: str | None = None,
    source: str | None = None,
    source_external_id: str | None = None,
    payee: str | None = None,
    reverses_entry_id: str | None = None,
) -> None:
    """Project a JournalEntry node + its (JournalEntry)-[:FOR_ENTITY]->(Entity) edge.

    Idempotent MERGE on the entry id — Postgres' append-only ``journal_entry``
    row is authoritative; this is its non-authoritative mirror, so the same row
    replayed by the backfill or a retried projection converges. Only a curated,
    non-PII subset of columns is mirrored (category/amount/dates/lineage) — no
    description/memo/reasoning is copied into the graph.

    ``reverses_entry_id``: when set (a reversal entry from a recategorization),
    materialize a ``(JournalEntry)-[:REVERSES]->(JournalEntry)`` edge to the
    entry it reverses, so the correction lineage is walkable in the graph.
    """
    async with falkordb_session(workspace_id) as graph:
        await graph.query(
            """
            MERGE (e:Entity {id: $entity_id})
            MERGE (j:JournalEntry {id: $je_id})
            SET j.entity_id = $entity_id,
                j.category = $category,
                j.amount_cents = $amount_cents,
                j.effective_date = $effective_date,
                j.source = $source,
                j.source_external_id = $source_external_id,
                j.payee = $payee
            MERGE (j)-[:FOR_ENTITY]->(e)
            """,
            params={
                "je_id": journal_entry_id,
                "entity_id": str(entity_id),
                "category": category,
                "amount_cents": amount_cents,
                "effective_date": effective_date,
                "source": source,
                "source_external_id": source_external_id,
                "payee": payee,
            },
        )
        if reverses_entry_id:
            await graph.query(
                """
                MERGE (j:JournalEntry {id: $je_id})
                MERGE (orig:JournalEntry {id: $orig_id})
                ON CREATE SET orig.entity_id = $entity_id
                MERGE (j)-[:REVERSES]->(orig)
                """,
                params={
                    "je_id": journal_entry_id,
                    "orig_id": reverses_entry_id,
                    "entity_id": str(entity_id),
                },
            )


async def upsert_compliance_rule(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    rule_id: str,
    *,
    kind: str,
    effective_from: str,
    effective_to: str | None = None,
    trigger: str | None = None,
    action: str | None = None,
    source: str | None = None,
    active: bool = True,
) -> None:
    """Project (idempotent upsert) a ComplianceRule node into the workspace subgraph.

    Mirrors the authoritative ``compliance_rule`` row so the temporal query in
    ``get_active_compliance_rules`` works against the graph. The node carries the
    same effective_from/effective_to interval the Postgres row holds.

    ``effective_from`` / ``effective_to`` are ISO-8601 strings (or None for an
    open-ended ``effective_to``); they are stored as integer epoch milliseconds
    (this FalkorDB build has no ``datetime()`` function) so the temporal comparison
    in ``get_active_compliance_rules`` (``r.effective_from <= timestamp()``) typechecks.
    ``active`` mirrors whether
    the rule's compliance_profile is active in Postgres — a proposed-but-not-yet-
    approved rule projects with active=false so a read can honor the same gate.
    """
    async with falkordb_session(workspace_id) as graph:
        await graph.query(
            """
            MERGE (r:ComplianceRule {id: $id})
            SET r.entity_id = $entity_id,
                r.kind = $kind,
                r.effective_from = $effective_from_ms,
                r.effective_to = $effective_to_ms,
                r.trigger = $trigger,
                r.action = $action,
                r.source = $source,
                r.active = $active
            """,
            params={
                "id": rule_id,
                "entity_id": str(entity_id),
                "kind": kind,
                "effective_from_ms": _iso_to_ms(effective_from),
                "effective_to_ms": _iso_to_ms(effective_to),
                "trigger": trigger,
                "action": action,
                "source": source,
                "active": active,
            },
        )


# --- Read-side: learned vendor → category patterns ----------------------


async def get_learned_category_for_vendor(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    vendor_name: str,
) -> dict | None:
    """Return the learned (Vendor)-[:LEARNED_PATTERN]->(CategorizationRule) for a vendor.

    The read-side proof that the graph is consulted at runtime: when a human has
    previously approved/overridden a categorization for this vendor, that lands
    in the graph as a CategorizationRule the vendor LEARNED_PATTERN-points at.
    This returns the highest-evidence pattern (most human confirmations) so a
    future transaction from the same vendor can be suggested confidently.

    Entity-scoped: only patterns whose Vendor + CategorizationRule carry this
    ``entity_id`` are considered, so a learned pattern never crosses entities
    within the workspace (and never crosses workspaces — the subgraph is
    workspace-scoped). Vendor name match is case-insensitive.

    Returns ``{"category", "confirmations", "source"}`` or ``None`` when there is
    no learned pattern. The caller MUST treat ``None`` (and any raised exception,
    which it should catch) as "no signal" and fall back to existing behavior.
    """
    name = (vendor_name or "").strip()
    if not name:
        return None
    async with falkordb_session(workspace_id) as graph:
        result = await graph.query(
            """
            MATCH (v:Vendor)-[p:LEARNED_PATTERN]->(r:CategorizationRule)
            WHERE v.entity_id = $entity_id
              AND r.entity_id = $entity_id
              AND toLower(v.name) = toLower($name)
            RETURN r.category AS category,
                   COALESCE(r.confirmations, 1) AS confirmations
            ORDER BY confirmations DESC
            LIMIT 1
            """,
            params={"entity_id": str(entity_id), "name": name},
        )
        rows = result.result_set or []
        if not rows:
            return None
        row = rows[0]
        category = row[0]
        confirmations = row[1]
        if category is None:
            return None
        return {
            "category": category,
            "confirmations": int(confirmations) if confirmations is not None else 1,
            "source": "falkordb:learned_pattern",
        }


async def record_learned_pattern(
    workspace_id: str | uuid.UUID,
    entity_id: str | uuid.UUID,
    vendor_name: str,
    category: str,
    *,
    rule_id: str | None = None,
    confirmed_by: str | None = None,
) -> None:
    """Teach the graph: a human confirmed ``vendor_name`` maps to ``category``.

    Materializes (Vendor {name})-[:LEARNED_PATTERN]->(CategorizationRule {category})
    scoped to the entity, and bumps a ``confirmations`` counter on the rule each
    time a human re-affirms the same mapping — so ``get_learned_category_for_vendor``
    can rank by evidence and a long-standing pattern outweighs a one-off.

    Idempotent on (entity_id, vendor_name, category): the node + edge MERGE, only
    the confirmation count and last-confirmed metadata advance. This is the
    LEARN side of the read-side proof; it is called (best-effort, via ``project``)
    whenever a human approves/overrides a categorization. PII-minimal: only the
    counterparty/vendor name (already on the bank descriptor) + the category land
    in the graph — no amounts, no descriptions.
    """
    name = (vendor_name or "").strip()
    cat = (category or "").strip()
    if not name or not cat:
        return
    # Deterministic rule identity per (entity, vendor, category) so re-confirming
    # the same mapping converges on one CategorizationRule node instead of
    # spawning duplicates. A caller-supplied rule_id (e.g. the Postgres
    # category_override id) takes precedence when present.
    rid = rule_id or str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{entity_id}|{name.lower()}|{cat.lower()}")
    )
    async with falkordb_session(workspace_id) as graph:
        await graph.query(
            """
            MERGE (v:Vendor {entity_id: $entity_id, name: $name})
            MERGE (r:CategorizationRule {id: $rid})
            ON CREATE SET r.entity_id = $entity_id,
                          r.category = $category,
                          r.confirmations = 1,
                          r.created_at = timestamp()
            ON MATCH SET r.confirmations = COALESCE(r.confirmations, 1) + 1
            SET r.category = $category,
                r.last_confirmed_at = timestamp(),
                r.last_confirmed_by = $confirmed_by
            MERGE (v)-[:LEARNED_PATTERN]->(r)
            """,
            params={
                "entity_id": str(entity_id),
                "name": name,
                "rid": rid,
                "category": cat,
                "confirmed_by": confirmed_by,
            },
        )
