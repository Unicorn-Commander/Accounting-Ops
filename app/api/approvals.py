"""Approval queue: list pending approvals + approve/reject (apply) them.

This closes the loop on material actions that QUEUE but couldn't be finalized.
``post_correction`` (a material category recat) and ``add_specialty_rule`` (a
proposed compliance rule) each insert a ``'pending'`` ``approval_queue`` row with
the structured action in ``approval_queue.payload`` (migration 0006). These
endpoints let a human list those and decide them:

  GET  /api/workspaces/{wid}/approvals?entity_id=&status=  — list (accountant+)
  POST /api/workspaces/{wid}/approvals/{approval_id}/decide — approve/reject (admin)

On APPROVE the held action is APPLIED:
  * journal_recategorization -> apply the held recat via the SAME apply-primitive
    the auto-apply path uses (apply_held_recategorization): a reversal +
    restatement are appended, advancing the per-entity hash chain, and the
    approving human is stamped onto both appended entries
    (approving_human_uc_uid). Append-only + the chain are preserved.
  * compliance_rule -> activate the proposed rule (activate_proposed_rule moves
    it from the inactive holding profile into the entity's active "Approved
    rules" profile, so query_specialty_rules now surfaces it).

On REJECT the ledger / rules are left untouched.

In both cases the row is marked decided (status, decided_by_uc_uid, decided_at)
and a ``decision_audit`` row is appended. Everything is entity-scoped +
token-bound exactly like app/api/journal.py (require_role + resolve_workspace +
assert_entity_in_scope) and runs inside ONE workspace_session transaction, so a
failure mid-apply rolls back atomically — there is no half-applied recat and the
row is never marked decided unless the apply succeeded.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.auth import AuthContext, validate_bearer_token
from app.authz import assert_entity_in_scope
from app.config import get_settings
from app.db import SessionLocal
import datetime as dt

from app.journal import (
    CorrectionOutcome,
    PostingOutcome,
    PostingRefusal,
    apply_held_recategorization,
    post_journal_entry,
)
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.tools._audit import project_decision_audit, write_decision_audit
from app.tools.specialty_rules import activate_proposed_rule
from app.workspace_session import workspace_session

router = APIRouter(tags=["approvals"])


# ====================================================================
# List valid categories (used by the UI dropdown)
# ====================================================================


@router.get("/api/workspaces/{workspace_id}/approvals/categories")
async def list_approval_categories(
    workspace_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Return the list of valid category keys (CATEGORY_TO_ACCOUNT keys).

    Used by the approvals UI to populate the category-override dropdown for
    bank_transaction_import rows. Read floor is accountant (same as list).
    """
    from app.journal.posting import CATEGORY_TO_ACCOUNT  # local import avoids circular

    require_role(workspace, "accountant")
    return {"categories": sorted(CATEGORY_TO_ACCOUNT.keys())}


# ====================================================================
# List pending approvals
# ====================================================================


@router.get("/api/workspaces/{workspace_id}/approvals")
async def list_approvals(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    status_filter: str = "pending",
    limit: int = 100,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """List approval-queue items for a workspace (optionally one entity).

    Read floor is `accountant` — an accountant can see what is waiting on a
    human; only `admin` can decide (the decide endpoint enforces that). Defaults
    to status='pending'; pass status_filter=all to see decided history.

    Entity-scope (BOLA): a scoped member is restricted to their entities; an
    out-of-scope concrete entity_id -> 403; an unscoped member sees all. Mirrors
    app/api/journal.py:list_entries.
    """
    require_role(workspace, "accountant")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {"limit": min(limit, 500), "offset": offset}
        if entity_id:
            where.append("aq.entity_id = :eid")
            params["eid"] = str(entity_id)
        if scope_restriction is not None:
            where.append("aq.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]
        if status_filter and status_filter.lower() != "all":
            where.append("aq.status = :status")
            params["status"] = status_filter
        where_sql = " AND ".join(where)

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT aq.id::text, aq.entity_id::text,
                           e.display_name AS entity_display_name,
                           e.slug AS entity_slug,
                           aq.subject_type, aq.subject_id::text,
                           aq.amount, aq.currency_code,
                           aq.status, aq.reasoning, aq.requested_by_identity,
                           aq.decided_by_uc_uid, aq.decided_at, aq.created_at,
                           aq.payload
                    FROM approval_queue aq
                    LEFT JOIN entity e ON e.id = aq.entity_id
                    WHERE {where_sql}
                    ORDER BY aq.created_at DESC, aq.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()
        total = (
            await session.execute(
                text(f"SELECT count(*) FROM approval_queue aq WHERE {where_sql}"),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

    return {"approvals": [dict(r) for r in rows], "total": total}


# ====================================================================
# Approve / reject a pending approval
# ====================================================================


class DecideRequest(BaseModel):
    decision: str  # "approve" | "reject"
    note: str | None = None
    # Optional category override for bank_transaction_import approvals.
    # When present (and decision=="approve"), replaces payload["category"]
    # before the import is applied. Must be a key in CATEGORY_TO_ACCOUNT.
    category_override: str | None = None


class SkipRequest(BaseModel):
    """Body for POST /api/.../approvals/{id}/skip and /unskip.

    A skip is a durable "decide later" — the row stays out of the active
    pending queue but is NOT applied to the ledger. The user can return to
    /approvals?status=skipped and un-skip it (back to 'pending') any time.
    """

    note: str | None = None


@router.post("/api/workspaces/{workspace_id}/approvals/{approval_id}/decide")
async def decide_approval(
    workspace_id: UUID,
    approval_id: UUID,
    body: DecideRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
    auth: AuthContext = Depends(validate_bearer_token),
) -> dict:
    """Approve or reject a pending approval. APPROVE applies the held action.

    Role floor is `accountant`: deciding a single approval is a routine professional
    attestation by whoever reviews the books — the same floor that lists approvals
    and that surfaces the item in the cross-client work inbox, so a CPA/accountant
    serving a client can actually act on it. (Creating a standing, auto-applying
    rule via add_specialty_rule stays `admin`: it's broader-impact than deciding one
    item.) On APPROVE the structured
    action in approval_queue.payload is applied (recat -> reversal+restatement via
    the shared apply-primitive; rule -> activated). The row is then marked decided
    and a decision_audit row is written. On REJECT nothing in the ledger/rules
    changes; the row is marked rejected + audited.

    All work runs in ONE transaction (workspace_session). If the apply fails, the
    whole thing rolls back: the row stays 'pending', no ledger entry was posted,
    no audit claims success — fail-safe for financial data.
    """
    require_role(workspace, "accountant")

    decision = body.decision.strip().lower()
    if decision not in ("approve", "reject"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision must be 'approve' or 'reject'",
        )

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Lock the row so two concurrent deciders can't both apply it. FOR UPDATE
        # within this transaction; the second decider sees status != 'pending'
        # after the first commits and gets a clean 409.
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text AS entity_id, subject_type,
                           subject_id::text AS subject_id, status, payload, reasoning
                    FROM approval_queue
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": str(approval_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")

        entity_id = UUID(row["entity_id"])
        # Entity-scope: a scoped member can only decide approvals for their
        # entities. Out-of-scope -> 403 (same guard as the list endpoint).
        assert_entity_in_scope(workspace, entity_id)

        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Approval already {row['status']}",
            )

        subject_type = row["subject_type"]
        subject_id = UUID(row["subject_id"])
        payload = row["payload"] or {}
        # Collected inside the transaction; the graph projection runs AFTER the
        # workspace_session below commits (best-effort, never blocks the request).
        applied: dict = {}

        if subject_type == "journal_entry_post":
            if (not isinstance(auth, AuthContext) or not auth.may_autoprovision
                    or auth.agent_id is not None or auth.is_trusted_m2m
                    or auth.uc_uid != workspace.user_uc_uid
                    or auth.claims.get("azp") != get_settings().keycloak_client_id
                    or "clientId" in auth.claims or "client_id" in auth.claims
                    or type(auth.claims.get("auth_time")) is not int
                    or auth.claims.get("auth_time", 0) <= 0
                    or not (auth.claims.get("sid") or auth.claims.get("session_state"))
                    or str(auth.claims.get("preferred_username", "")).startswith("service-account-")):
                raise HTTPException(status_code=403, detail="Journal proposal requires a human session")

        # ---------------- REJECT ----------------
        if decision == "reject":
            await _mark_decided(
                session, approval_id=approval_id, new_status="rejected",
                decided_by=workspace.user_uc_uid,
            )
            response = {
                "ok": True,
                "approval_id": str(approval_id),
                "decision": "rejected",
                "subject_type": subject_type,
                "applied": False,
            }
            audit_id = await write_decision_audit(
                session,
                workspace_id=workspace_id,
                entity_id=entity_id,
                ctx=workspace,
                tool_name="approval_decision",
                request_payload={
                    "approval_id": str(approval_id), "decision": "reject",
                    "note": body.note,
                },
                response_payload=response,
                reasoning=body.note or "Rejected by human reviewer.",
                outcome="rejected",
            )
            decision_outcome = "rejected"

        else:
            # ---------------- APPROVE ----------------
            if subject_type == "journal_recategorization":
                applied = await _approve_recategorization(
                    session,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    subject_id=subject_id,
                    payload=payload,
                    queued_reasoning=row["reasoning"],
                    approving_human=workspace.user_uc_uid,
                    note=body.note,
                )
            elif subject_type == "compliance_rule":
                applied = await _approve_compliance_rule(
                    session,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    subject_id=subject_id,
                    payload=payload,
                )
            elif subject_type == "journal_entry_post":
                # These values come from authenticated workspace resolution, not
                # the decision body. Agent/PAT authority is not human attestation.
                if workspace.agent_id is not None or workspace.pat_scopes is not None:
                    raise HTTPException(status_code=403, detail="Journal proposal requires a human session")
                if body.category_override is not None:
                    raise HTTPException(status_code=422, detail="Journal proposal payload is immutable")
                from app.journal.pending_post import apply_proposal
                try:
                    applied = await apply_proposal(session, workspace_id=workspace_id,
                        entity_id=entity_id, payload=payload, approving_human=workspace.user_uc_uid)
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
            elif subject_type == "bank_transaction_import":
                applied = await _approve_bank_transaction_import(
                    session,
                    workspace_id=workspace_id,
                    entity_id=entity_id,
                    payload=payload,
                    approving_human=workspace.user_uc_uid,
                    note=body.note,
                    category_override=body.category_override,
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Cannot apply unknown subject_type '{subject_type}'",
                )

            # Apply succeeded (the helpers raise HTTPException on a refusal, which
            # rolls the transaction back). Mark decided + audit in the SAME tx.
            await _mark_decided(
                session, approval_id=approval_id, new_status="approved",
                decided_by=workspace.user_uc_uid,
            )
            response = {
                "ok": True,
                "approval_id": str(approval_id),
                "decision": "approved",
                "subject_type": subject_type,
                "applied": True,
                **applied,
            }
            audit_id = await write_decision_audit(
                session,
                workspace_id=workspace_id,
                entity_id=entity_id,
                ctx=workspace,
                tool_name="approval_decision",
                request_payload={
                    "approval_id": str(approval_id), "decision": "approve",
                    "note": body.note,
                },
                response_payload=response,
                reasoning=body.note or "Approved by human reviewer.",
                outcome="approved",
            )
            decision_outcome = "approved"

    # ---- FalkorDB projection (best-effort, AFTER the workspace_session commit) ----
    # Everything above is authoritative + already committed. Mirror it into the
    # graph fire-and-forget; any failure is swallowed in project()/record_*.
    _project_approval_decision(
        workspace_id=workspace_id,
        entity_id=entity_id,
        audit_id=audit_id,
        ctx=workspace,
        decision_outcome=decision_outcome,
        subject_type=subject_type,
        subject_id=subject_id,
        applied=applied,
        payload=payload,
        note=body.note,
    )
    return response


# --------------------------------------------------------------------
# Apply helpers (run on the decide endpoint's session/transaction)
# --------------------------------------------------------------------


def _project_approval_decision(
    *,
    workspace_id: UUID,
    entity_id: UUID,
    audit_id: UUID,
    ctx: WorkspaceContext,
    decision_outcome: str,
    subject_type: str,
    subject_id: UUID,
    applied: dict,
    payload: dict,
    note: str | None,
) -> None:
    """Best-effort FalkorDB projection of an approval decision (post-commit).

    Mirrors the authoritative writes the decide endpoint just committed:
      * the approval Decision node (approved/rejected), which OVERRIDES the prior
        decision about the same subject;
      * on an APPROVED ``compliance_rule`` → the now-ACTIVE ComplianceRule node
        (active=True), so get_active_compliance_rules surfaces it from the graph;
      * on an APPROVED ``journal_recategorization`` → the appended reversal +
        restatement JournalEntry nodes (with REVERSES lineage);
      * on an APPROVED ``bank_transaction_import`` → the posted JournalEntry node.

    Fire-and-forget via project(); every failure is swallowed there. Call ONLY
    after the workspace_session transaction has committed.
    """
    from app import falkordb

    # 1. The decision itself (always — approve or reject).
    project_decision_audit(
        workspace_id=workspace_id,
        entity_id=entity_id,
        audit_id=audit_id,
        ctx=ctx,
        tool_name="approval_decision",
        outcome=decision_outcome,
        reasoning=note or f"Approval {decision_outcome} by human reviewer.",
        subject_id=subject_id,
        overrides_subject_id=subject_id,
    )
    if decision_outcome != "approved":
        return

    # 2. Subject-specific ledger/rule projections.
    if subject_type == "compliance_rule":
        # The rule moved into an ACTIVE profile; re-project active=True so the
        # temporal read sees it. effective_from = now (activation time) is a safe
        # mirror; the authoritative effective_from lives in Postgres.
        falkordb.project(
            falkordb.upsert_compliance_rule(
                workspace_id,
                entity_id,
                str(applied.get("rule_id") or subject_id),
                kind=str(payload.get("kind") or "rule"),
                effective_from=dt.datetime.now(tz=dt.timezone.utc).isoformat(),
                effective_to=None,
                trigger=payload.get("trigger"),
                action=payload.get("action_enum"),
                source="ai_inferred",
                active=True,
            ),
            label=f"compliance_rule_active:{applied.get('rule_id') or subject_id}",
        )
    elif subject_type == "journal_recategorization":
        from app.journal.posting import CorrectionOutcome
        from app.journal.projection import project_recategorization

        rev_id = applied.get("reversal_entry_id")
        restated_id = applied.get("restated_entry_id")
        if rev_id and restated_id:
            outcome = CorrectionOutcome(
                reversal_entry_id=UUID(str(rev_id)),
                restated_entry_id=UUID(str(restated_id)),
                materiality_tier=str(applied.get("materiality_tier") or ""),
                from_category=str(applied.get("from_category") or ""),
                to_category=str(applied.get("to_category") or ""),
            )
            amt = payload.get("amount_cents")
            project_recategorization(
                workspace_id=workspace_id,
                entity_id=entity_id,
                outcome=outcome,
                orig_entry_id=subject_id,
                amount_cents=int(amt) if amt is not None else 0,
                effective_date=payload.get("effective_date"),
                payee=payload.get("payee"),
            )
    elif subject_type in ("bank_transaction_import", "journal_entry_post"):
        if subject_type == "journal_entry_post":
            payload = payload["request"]
        from app.journal.posting import PostingOutcome
        from app.journal.materiality import MaterialityVerdict
        from app.journal.projection import project_journal_entry_posted

        je_id = applied.get("journal_entry_id")
        amt = payload.get("amount_cents")
        if je_id and amt is not None:
            outcome = PostingOutcome(
                journal_entry_id=UUID(str(je_id)),
                materiality=MaterialityVerdict(
                    outcome="auto_post",
                    tier=str(applied.get("materiality_tier") or "L"),
                    reason="human_approved",
                ),
            )
            project_journal_entry_posted(
                workspace_id=workspace_id,
                entity_id=entity_id,
                outcome=outcome,
                category=str(payload.get("category") or ""),
                amount_cents=int(amt),
                effective_date=payload.get("effective_date"),
                source="bofa-statement",
                source_external_id=applied.get("source_external_id")
                or payload.get("source_external_id"),
                payee=payload.get("payee"),
            )


async def _mark_decided(
    session, *, approval_id: UUID, new_status: str, decided_by: str
) -> None:
    """Stamp status + decided_by_uc_uid + decided_at on the approval row."""
    await session.execute(
        text(
            """
            UPDATE approval_queue
            SET status = CAST(:status AS approval_status),
                decided_by_uc_uid = :uid,
                decided_at = now()
            WHERE id = :id
            """
        ),
        {"status": new_status, "uid": decided_by, "id": str(approval_id)},
    )


async def _approve_recategorization(
    session,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    subject_id: UUID,
    payload: dict,
    queued_reasoning: str,
    approving_human: str,
    note: str | None,
) -> dict:
    """Apply a held material recat. subject_id is the ORIGINAL entry id.

    Re-fetches the original entry LIVE, derives new_category from payload, and
    posts the reversal + restatement via the shared apply-primitive — stamping
    the approving human onto the appended entries. Raises HTTPException on any
    refusal so the outer transaction rolls back (nothing marked decided).
    """
    new_category = payload.get("new_category")
    if not new_category:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Approval payload missing new_category (queued before 0006 or "
                "malformed); cannot apply this recategorization automatically."
            ),
        )

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
            {"id": str(subject_id), "eid": str(entity_id)},
        )
    ).mappings().first()
    if original is None:
        raise HTTPException(
            status_code=404,
            detail=f"Original entry {subject_id} not found in this entity",
        )
    if original["status"] != "posted":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Original entry {subject_id} is '{original['status']}', not "
                "'posted'; it may have already been reversed/recategorized."
            ),
        )

    reasoning = (
        f"Human-approved recategorization (approval). {queued_reasoning}"
        + (f" Reviewer note: {note}" if note else "")
    )
    result = await apply_held_recategorization(
        session,
        workspace_id=str(workspace_id),
        entity_id=str(entity_id),
        original=dict(original),
        new_category=new_category,
        reasoning=reasoning,
        approving_human_uc_uid=approving_human,
    )
    if isinstance(result, PostingRefusal):
        # Roll the whole decision back: the recat could not be applied cleanly.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not apply recategorization: {result.reason}",
        )
    assert isinstance(result, CorrectionOutcome)
    return {
        "from_category": result.from_category,
        "to_category": result.to_category,
        "materiality_tier": result.materiality_tier,
        "reversal_entry_id": str(result.reversal_entry_id),
        "restated_entry_id": str(result.restated_entry_id),
    }


async def _approve_compliance_rule(
    session,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    subject_id: UUID,
    payload: dict,
) -> dict:
    """Activate a proposed compliance rule. subject_id is the rule id."""
    try:
        activated = await activate_proposed_rule(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            rule_id=subject_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "rule_id": activated["rule_id"],
        "active_profile_id": activated["profile_id"],
    }


async def _approve_bank_transaction_import(
    session,
    *,
    workspace_id: UUID,
    entity_id: UUID,
    payload: dict,
    approving_human: str,
    note: str | None,
    category_override: str | None = None,
) -> dict:
    """Apply a held bank-statement transaction import.

    The payload written by the ingest script carries:
      source_external_id, effective_date, amount_cents, category,
      category_status, categorization_source, debit_account_hint,
      credit_account_hint, materiality_tier, block_reason, llm_suggested.

    On APPROVE we call post_journal_entry() with user_confirmed=True and
    posted_by_uc_uid=<the approving human> — the same pattern as
    apply_held_recategorization.  Materiality is intentionally NOT
    re-evaluated: the gate's purpose is to decide whether human attestation
    is required; that attestation has now happened.

    On PostingRefusal → raise HTTPException(409) so the outer transaction
    rolls back and the row stays pending (consistent with the recat path).
    """
    required = ("source_external_id", "effective_date", "amount_cents", "category")
    missing = [k for k in required if k not in payload]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Approval payload missing fields: {missing}. "
                "Cannot apply this bank_transaction_import automatically."
            ),
        )

    source_external_id: str = payload["source_external_id"]
    effective_date = dt.date.fromisoformat(payload["effective_date"])
    amount_cents: int = int(payload["amount_cents"])
    # If the human picked a different category in the UI, validate + use it.
    if category_override:
        from app.journal.posting import CATEGORY_TO_ACCOUNT as _C2A
        if category_override not in _C2A:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"category_override '{category_override}' is not a valid "
                    "CATEGORY_TO_ACCOUNT key."
                ),
            )
        category: str = category_override
        category_status: str = "human_override"
    else:
        category = payload["category"]
        category_status = payload.get("category_status", "auto")

    # The external account id for the cash/bank side.  The ingest script stores
    # debit_account_hint / credit_account_hint as COA canonical hints (not the
    # external account id).  The authoritative external account id for BofA 1792
    # comes from the COA lookup in post_journal_entry via resolve_accounts_for_txn,
    # which needs linked_external_account_id on the COA row.  We pass the value
    # that was stored in the COA at provision time.  If the script wrote an
    # external_account_id key in the payload, use it; otherwise look it up from
    # the COA account linked to the source.
    external_account_id: str | None = payload.get("external_account_id")
    if not external_account_id:
        # Fall back: query the COA for the account linked to 'bofa-statement' source
        # for this entity.  This mirrors how post_journal_entry resolves the cash side.
        from sqlalchemy import text as _text
        cash_row = (
            await session.execute(
                _text(
                    """
                    -- Match either the explicit 'bofa-statement' link OR the
                    -- live 'simplefin' link (the COA actually carries 'simplefin'
                    -- for BofA accounts today). Belt-and-suspenders so legacy
                    -- approval_queue rows missing external_account_id in payload
                    -- can still resolve.
                    SELECT linked_external_account_id
                    FROM chart_of_account
                    WHERE entity_id = :eid
                      AND linked_external_source IN ('bofa-statement', 'simplefin')
                      AND is_active = true
                    LIMIT 1
                    """
                ),
                {"eid": str(entity_id)},
            )
        ).mappings().first()
        if cash_row:
            external_account_id = cash_row["linked_external_account_id"]

    reasoning = (
        f"Human-approved bank statement import (approval). "
        f"Source: {source_external_id}."
        + (f" Reviewer note: {note}" if note else "")
    )

    result = await post_journal_entry(
        session,
        workspace_id=str(workspace_id),
        entity_id=str(entity_id),
        source="bofa-statement",
        source_external_id=source_external_id,
        effective_date=effective_date,
        amount_cents=amount_cents,
        description=payload.get("description", source_external_id),
        payee=payload.get("payee"),
        memo=payload.get("memo"),
        category=category,
        category_status=category_status,
        entity_mapped=True,
        sign_matched=True,  # human attestation = gate cleared
        external_account_id=external_account_id,
        has_receipt_link=False,
        receipt_link_id=None,
        posted_by_uc_uid=approving_human,
        posted_by_agent_id=None,
        user_confirmed=True,  # human attestation bypasses materiality re-gate
        reasoning_trace=reasoning,
        model_version=payload.get("categorization_source", "human_approval"),
    )

    if isinstance(result, PostingRefusal):
        # Roll the whole decision back: the import could not be applied cleanly.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Could not apply bank_transaction_import: {result.reason}",
        )

    assert isinstance(result, PostingOutcome)
    return {
        "journal_entry_id": str(result.journal_entry_id),
        "materiality_tier": result.materiality.tier,
        "source_external_id": source_external_id,
    }


# ====================================================================
# Skip / un-skip — durable "decide later" state (no ledger impact)
# ====================================================================
#
# These flip the approval_queue.status between 'pending' and 'skipped' WITHOUT
# applying the held action. Every flip writes a decision_audit row so the trail
# is preserved. Role floor: accountant (can see + skip; only admin can decide).
# This matches the principle that "skip" is not an attestation — it's a
# triage state for a queue, accessible to anyone who can see the queue.


@router.post("/api/workspaces/{workspace_id}/approvals/{approval_id}/skip")
async def skip_approval(
    workspace_id: UUID,
    approval_id: UUID,
    body: SkipRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Mark a pending approval as 'skipped'. NEVER posts to the ledger.

    Idempotent for already-skipped rows (no-op + 200). Errors:
      * 404 if not found
      * 409 if status is approved/rejected/canceled (you can only skip pending)
    """
    require_role(workspace, "accountant")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text AS entity_id,
                           subject_type, status
                    FROM approval_queue
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": str(approval_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")

        if row["subject_type"] == "journal_entry_post":
            raise HTTPException(status_code=409, detail="Journal proposals require explicit individual review")

        entity_id = UUID(row["entity_id"])
        assert_entity_in_scope(workspace, entity_id)

        if row["status"] == "skipped":
            # Idempotent: already skipped — write an audit anyway so the trail
            # records that the user reasserted the skip (useful if a note is
            # attached).
            response = {
                "ok": True,
                "approval_id": str(approval_id),
                "status": "skipped",
                "noop": True,
            }
            await write_decision_audit(
                session,
                workspace_id=workspace_id,
                entity_id=entity_id,
                ctx=workspace,
                tool_name="approval_skip",
                request_payload={"approval_id": str(approval_id), "note": body.note},
                response_payload=response,
                reasoning=body.note or "Re-skipped (was already skipped).",
                outcome="skipped",
            )
            return response

        if row["status"] != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot skip an approval in status '{row['status']}'",
            )

        await session.execute(
            text(
                """
                UPDATE approval_queue
                SET status = CAST('skipped' AS approval_status)
                WHERE id = :id
                """
            ),
            {"id": str(approval_id)},
        )
        response = {
            "ok": True,
            "approval_id": str(approval_id),
            "status": "skipped",
            "noop": False,
        }
        await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            ctx=workspace,
            tool_name="approval_skip",
            request_payload={"approval_id": str(approval_id), "note": body.note},
            response_payload=response,
            reasoning=body.note or "Marked for later decision (skipped).",
            outcome="skipped",
        )
        return response


@router.post("/api/workspaces/{workspace_id}/approvals/{approval_id}/unskip")
async def unskip_approval(
    workspace_id: UUID,
    approval_id: UUID,
    body: SkipRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Flip a 'skipped' approval back to 'pending'.

    Idempotent if already 'pending'. Errors:
      * 404 if not found
      * 409 if status is approved/rejected/canceled
    """
    require_role(workspace, "accountant")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text, entity_id::text AS entity_id, subject_type, status
                    FROM approval_queue
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": str(approval_id)},
            )
        ).mappings().first()
        if row is None:
            raise HTTPException(status_code=404, detail="Approval not found")

        if row["subject_type"] == "journal_entry_post":
            raise HTTPException(status_code=409, detail="Journal proposals require explicit individual review")

        entity_id = UUID(row["entity_id"])
        assert_entity_in_scope(workspace, entity_id)

        if row["status"] == "pending":
            response = {
                "ok": True,
                "approval_id": str(approval_id),
                "status": "pending",
                "noop": True,
            }
            return response

        if row["status"] != "skipped":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Cannot un-skip an approval in status '{row['status']}'",
            )

        await session.execute(
            text(
                """
                UPDATE approval_queue
                SET status = CAST('pending' AS approval_status)
                WHERE id = :id
                """
            ),
            {"id": str(approval_id)},
        )
        response = {
            "ok": True,
            "approval_id": str(approval_id),
            "status": "pending",
            "noop": False,
        }
        await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            ctx=workspace,
            tool_name="approval_unskip",
            request_payload={"approval_id": str(approval_id), "note": body.note},
            response_payload=response,
            reasoning=body.note or "Returned skipped item to pending queue.",
            outcome="reopened",
        )
        return response
