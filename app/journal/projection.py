"""Best-effort FalkorDB projection of authoritative journal writes.

This module is the WRITE-SIDE bridge between the authoritative Postgres journal
(``app/journal/posting.py``) and the non-authoritative FalkorDB agent-memory
graph (``app/falkordb.py``). It holds NO ledger logic — posting.py stays the one
place double-entry/hash-chain semantics live. Each function here just maps a
successful posting *result* to the corresponding graph projection and schedules
it fire-and-forget via ``falkordb.project`` so:

  * it runs AFTER the authoritative Postgres commit (callers invoke these AFTER
    their ``workspace_session``/``entity_session`` block has committed, or after
    an explicit ``session.commit()``),
  * it adds ZERO latency (``project`` returns immediately),
  * a FalkorDB failure is logged + swallowed — never breaking the request.

Workspace + entity scoping is threaded through every call; the graph helpers
key off ``subgraph_for_workspace`` so data can never cross a workspace.
"""

from __future__ import annotations

from uuid import UUID

from app import falkordb
from app.journal.posting import CorrectionOutcome, PostingOutcome


def project_journal_entry_posted(
    *,
    workspace_id: str | UUID,
    entity_id: str | UUID,
    outcome: PostingOutcome,
    category: str,
    amount_cents: int,
    effective_date,
    source: str,
    source_external_id: str | None,
    payee: str | None,
    source_document_hash: str | None = None,
) -> None:
    """Project a freshly-posted JournalEntry node (best-effort, post-commit).

    Mirrors the ``journal_entry`` row that ``post_journal_entry`` just committed.
    When a source-document content hash is available, also materializes the
    ``(JournalEntry)-[:SUPPORTED_BY]->(SourceDocument)`` edge.

    Call this ONLY after the authoritative Postgres transaction has committed.
    """
    je_id = str(outcome.journal_entry_id)
    eff = effective_date.isoformat() if hasattr(effective_date, "isoformat") else (
        str(effective_date) if effective_date is not None else None
    )
    falkordb.project(
        falkordb.upsert_journal_entry(
            workspace_id,
            entity_id,
            je_id,
            category=category,
            amount_cents=amount_cents,
            effective_date=eff,
            source=source,
            source_external_id=source_external_id,
            payee=payee,
        ),
        label=f"journal_entry:{je_id}",
    )
    if source_document_hash:
        falkordb.project(
            falkordb.link_journal_entry_to_source(
                workspace_id, entity_id, je_id, source_document_hash
            ),
            label=f"je_supported_by:{je_id}",
        )


def project_recategorization(
    *,
    workspace_id: str | UUID,
    entity_id: str | UUID,
    outcome: CorrectionOutcome,
    orig_entry_id: str | UUID,
    amount_cents: int,
    effective_date,
    payee: str | None,
) -> None:
    """Project the reversal + restatement entries of an applied recategorization.

    ``_apply_recategorization`` appends two entries (a reversal pointing back at
    the original, and a re-stated entry on the new category). We mirror both as
    JournalEntry nodes; the reversal gets a ``(:JournalEntry)-[:REVERSES]->(...)``
    edge to the original so the correction lineage is walkable in the graph.

    Call this ONLY after the authoritative Postgres transaction has committed.
    """
    eff = effective_date.isoformat() if hasattr(effective_date, "isoformat") else (
        str(effective_date) if effective_date is not None else None
    )
    rev_id = str(outcome.reversal_entry_id)
    restated_id = str(outcome.restated_entry_id)
    # Reversal entry (flipped DR/CR), category = from_category, points at original.
    falkordb.project(
        falkordb.upsert_journal_entry(
            workspace_id,
            entity_id,
            rev_id,
            category=outcome.from_category,
            amount_cents=amount_cents,
            effective_date=eff,
            source="reversal",
            source_external_id=f"reversal:{orig_entry_id}",
            payee=payee,
            reverses_entry_id=str(orig_entry_id),
        ),
        label=f"recat_reversal:{rev_id}",
    )
    # Restated entry on the new category account.
    falkordb.project(
        falkordb.upsert_journal_entry(
            workspace_id,
            entity_id,
            restated_id,
            category=outcome.to_category,
            amount_cents=amount_cents,
            effective_date=eff,
            source="recategorization",
            source_external_id=f"recat:{orig_entry_id}",
            payee=payee,
        ),
        label=f"recat_restated:{restated_id}",
    )
