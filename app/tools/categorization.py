"""MCP write tool: re-categorize a posted journal entry (append-only).

Reuses the journal module's posting primitives — it does NOT reinvent ledger
semantics. The ledger is immutable (DB trigger + the runtime role's grant forbid
UPDATE/DELETE), so a category change is expressed as accounting corrections:
``app.journal.post_correction`` appends a reversing entry (pointing back via
``reverses_entry_id``) plus a re-stated entry on the new category account, both
advancing the per-entity SHA-256 hash chain. It runs through the SAME materiality
gate as a fresh post, so a material (M/H) correction queues to ``approval_queue``
for human attestation instead of auto-applying.

Authorization is identical to the read tools: ``authorize_workspace`` binds the
caller's token to the workspace + enforces role (accountant — the write floor the
HTTP categorize route requires) and entity_scope. The token, never the args,
decides reach. All work runs inside ``entity_session`` so the workspace
search_path + RLS GUC are set.
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from decimal import Decimal

from app.db import SessionLocal
from app.journal import CorrectionOutcome, CorrectionQueued, PostingRefusal, post_correction
from app.mcp_auth import authorize_workspace
from app.tools._audit import project_decision_audit, write_decision_audit
from app.workspace_session import entity_session


async def categorize_transaction(
    workspace_id: UUID,
    entity_id: UUID,
    id: UUID,
    category: str,
    reasoning: str,
) -> dict:
    """Categorize an entity-scoped transaction.

    Routes through the materiality gate: under-threshold auto-posts (appends a
    reversal + a re-stated entry on the new category account); over-threshold
    queues to approval_queue for human attestation. Always writes a
    decision_audit row.

    The transaction is identified by `id` (a posted journal_entry in this
    entity). The ledger is never mutated in place — the correction is appended,
    preserving the per-entity hash chain. Returns the outcome ('applied' with the
    reversal + restated entry ids, or 'queued_for_approval' with the approval id).
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")

    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, eid):
        original = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, amount, debit_account, credit_account,
                           category, effective_date, payee, description, memo,
                           status, receipt_link_id::text AS receipt_link_id
                    FROM journal_entry
                    WHERE id = :id AND entity_id = :eid
                    """
                ),
                {"id": str(id), "eid": str(eid)},
            )
        ).mappings().first()
        if original is None:
            raise LookupError(f"transaction {id} not found in entity {entity_id}")
        if original["status"] != "posted":
            raise ValueError(
                f"transaction {id} is '{original['status']}', not 'posted'; "
                "only posted entries can be recategorized."
            )
        if (original["category"] or "") == category:
            # No-op: already this category. Don't write a redundant correction.
            await write_decision_audit(
                session,
                workspace_id=workspace_id,
                entity_id=eid,
                ctx=ctx,
                tool_name="categorize",
                request_payload={
                    "id": str(id), "category": category, "reasoning": reasoning
                },
                response_payload={"outcome": "noop", "reason": "unchanged"},
                reasoning=reasoning,
                outcome="noop",
            )
            # A no-op writes a decision_audit row but changes nothing else, so we
            # don't project a graph Decision for it (keeps the graph free of
            # no-signal nodes). The Postgres audit row remains authoritative.
            return {
                "ok": True,
                "outcome": "noop",
                "id": str(id),
                "category": category,
                "reason": "Transaction already has this category.",
            }

        result = await post_correction(
            session,
            workspace_id=str(workspace_id),
            entity_id=str(eid),
            original=dict(original),
            new_category=category,
            reasoning=reasoning,
            actor_uc_uid=ctx.user_uc_uid,
            actor_agent_id=ctx.agent_id,
        )

        if isinstance(result, CorrectionOutcome):
            payload = {
                "ok": True,
                "outcome": "applied",
                "id": str(id),
                "from_category": result.from_category,
                "to_category": result.to_category,
                "materiality_tier": result.materiality_tier,
                "reversal_entry_id": str(result.reversal_entry_id),
                "restated_entry_id": str(result.restated_entry_id),
            }
            audit_outcome = "applied"
        elif isinstance(result, CorrectionQueued):
            payload = {
                "ok": True,
                "outcome": "queued_for_approval",
                "id": str(id),
                "from_category": result.from_category,
                "to_category": result.to_category,
                "materiality_tier": result.materiality_tier,
                "approval_id": str(result.approval_id),
                "reason": result.reason,
            }
            audit_outcome = "queued_for_approval"
        else:  # PostingRefusal
            assert isinstance(result, PostingRefusal)
            payload = {
                "ok": False,
                "outcome": result.outcome,
                "id": str(id),
                "reason": result.reason,
            }
            audit_outcome = result.outcome

        audit_id = await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=eid,
            ctx=ctx,
            tool_name="categorize",
            request_payload={
                "id": str(id), "category": category, "reasoning": reasoning
            },
            response_payload=payload,
            reasoning=reasoning,
            outcome=audit_outcome,
        )
        # Capture original fields needed for the recat-entry projection while the
        # row is still in scope (amount is NUMERIC; convert to cents).
        orig_amount_cents = int(
            (Decimal(str(original["amount"])) * 100).to_integral_value()
        )
        orig_effective_date = original["effective_date"]
        orig_payee = original["payee"]

    # ---- FalkorDB projection (best-effort, AFTER the entity_session commit) ----
    # The graph mirrors the authoritative Postgres writes that just committed:
    #   1. the agent Decision (this categorize call), which OVERRIDES the prior
    #      decision about the same entry (recategorization supersession);
    #   2. on an applied correction, the reversal + restatement JournalEntry
    #      nodes (with the REVERSES lineage edge).
    # Any failure is swallowed inside project()/record_* — never affects the call.
    project_decision_audit(
        workspace_id=workspace_id,
        entity_id=eid,
        audit_id=audit_id,
        ctx=ctx,
        tool_name="categorize",
        outcome=audit_outcome,
        reasoning=reasoning,
        subject_id=id,
        overrides_subject_id=id,
    )
    if isinstance(result, CorrectionOutcome):
        from app.journal.projection import project_recategorization

        project_recategorization(
            workspace_id=workspace_id,
            entity_id=eid,
            outcome=result,
            orig_entry_id=id,
            amount_cents=orig_amount_cents,
            effective_date=orig_effective_date,
            payee=orig_payee,
        )

    if not payload["ok"]:
        raise ValueError(f"categorize refused: {payload['reason']}")
    return payload
