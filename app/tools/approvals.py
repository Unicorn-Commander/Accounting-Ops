"""MCP tools for the approval queue.

Approvals are the human-attestation gate for material ledger writes (e.g. a
high-materiality recategorization) and rule additions. ``list_approvals`` is a
read tool (viewer floor in the workspace; the underlying HTTP route requires
accountant — we mirror that for consistency). ``decide_approval`` applies an
approve/reject decision and APPLIES the held action on approve — it requires
admin role, the same floor the HTTP endpoint enforces.

Both tools authorize via ``authorize_workspace`` (token-bound) and reuse the
existing HTTP handlers (``decide_approval`` HTTP route) for the apply logic so
the MCP and HTTP surfaces can never diverge. Decisions append a
``decision_audit`` row exactly as the HTTP path does (the HTTP handler writes
it; the MCP tool delegates and inherits that).

Bulk-decide is conservative: it fails closed on any single-item refusal and
returns a per-item outcome so an agent can reconcile.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import workspace_session


_VALID_STATUS_FILTERS = frozenset({"pending", "approved", "rejected", "all"})

# Audit 2026-05-31 Bug 6: documented but-unenforced cap. Enforce at the TOP of
# bulk_decide_approvals so a buggy UI/agent submitting thousands of items can't
# tie up the per-item hash-chain advance or balloon a single audit window. The
# HTTP layer (when this tool is invoked over HTTP) surfaces this as 413 Payload
# Too Large via the route wrapper; the MCP layer surfaces it as a ValueError so
# the tool returns a clean error to the agent.
BULK_DECIDE_MAX_ITEMS = 200


async def list_pending_approvals(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    status_filter: str = "pending",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List approval-queue items for a workspace (optionally one entity).

    Read tool. Default status_filter='pending' so an agent sees only items
    awaiting human attestation. Mirrors GET /api/workspaces/{wid}/approvals.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Optional entity filter. If omitted, all entities the caller
            can reach are returned (entity_scope restriction still applies for
            scoped members).
        status_filter: One of 'pending', 'approved', 'rejected', or 'all'.
            Default 'pending'.
        limit: Page size (max 500). Default 100.
        offset: Page offset. Default 0.

    Returns:
        {
          "approvals": [
            {
              "id": "<uuid>", "entity_id": "<uuid>",
              "entity_display_name": "...", "entity_slug": "...",
              "subject_type": "journal_recategorization"|"compliance_rule"|
                              "bank_transaction_import",
              "subject_id": "<uuid>",
              "amount": "1234.56", "currency_code": "USD",
              "status": "pending"|"approved"|"rejected",
              "reasoning": "...", "requested_by_identity": "...",
              "decided_by_uc_uid": "...", "decided_at": "...",
              "created_at": "...",
              "payload": {...},
            },
            ...
          ],
          "total": int,
        }

    Role floor: accountant (mirrors the HTTP route).
    """
    sf = (status_filter or "pending").strip().lower()
    if sf not in _VALID_STATUS_FILTERS:
        raise ValueError(
            f"status_filter must be one of {sorted(_VALID_STATUS_FILTERS)}; got {status_filter!r}"
        )

    ctx = await authorize_workspace(workspace_id, entity_id, min_role="accountant")
    scope_restriction = assert_entity_in_scope(ctx, entity_id)

    async with workspace_session(SessionLocal, workspace_id) as session:
        where = ["1=1"]
        params: dict[str, Any] = {
            "limit": min(max(int(limit), 1), 500),
            "offset": max(int(offset), 0),
        }
        if entity_id:
            where.append("aq.entity_id = :eid")
            params["eid"] = str(entity_id)
        if scope_restriction is not None:
            where.append("aq.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]
        if sf != "all":
            where.append("aq.status = :status")
            params["status"] = sf
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

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    return {
        "approvals": [{k: _b(v) for k, v in dict(r).items()} for r in rows],
        "total": int(total),
    }


async def decide_approval(
    workspace_id: UUID,
    approval_id: UUID,
    decision: str,
    note: str | None = None,
    category_override: str | None = None,
) -> dict[str, Any]:
    """Approve or reject a single pending approval.

    On APPROVE the held action is applied (recat → reversal+restatement via the
    shared apply primitive; compliance_rule → activated; bank_transaction_import
    → posted with user_confirmed=True). On REJECT the ledger/rules are left
    untouched. Either way the row is marked decided and a ``decision_audit``
    row is appended atomically inside one transaction (the HTTP handler holds
    that invariant; this tool delegates to it).

    Args:
        workspace_id: Workspace UUID.
        approval_id: The approval_queue id.
        decision: 'approve' or 'reject' (case-insensitive).
        note: Optional reviewer note recorded in decision_audit.
        category_override: For bank_transaction_import approvals only — replace
            payload['category'] with this key (must be in CATEGORY_TO_ACCOUNT)
            before applying. Ignored for other subject types.

    Returns the response shape of the HTTP decide endpoint:
        {
          "ok": True,
          "approval_id": "<uuid>",
          "decision": "approved" | "rejected",
          "subject_type": "...",
          "applied": bool,
          ... (subject-specific fields on approve: reversal_entry_id,
                restated_entry_id, materiality_tier, journal_entry_id,
                source_external_id, rule_id, active_profile_id, ...)
        }

    Role floor: admin (same as HTTP).
    """
    # Delegate to the HTTP handler implementation so the apply path stays in
    # ONE place. We re-resolve the workspace through authorize_workspace so the
    # MCP caller's role + entity scope is enforced before any work.
    ctx = await authorize_workspace(workspace_id, None, min_role="admin")

    from app.api.approvals import DecideRequest, decide_approval as http_decide_approval

    decision_norm = (decision or "").strip().lower()
    if decision_norm not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")

    body = DecideRequest(
        decision=decision_norm,
        note=note,
        category_override=category_override,
    )
    # The HTTP handler's signature accepts (workspace_id, approval_id, body,
    # workspace). We pass our ctx as workspace.
    return await http_decide_approval(
        workspace_id=workspace_id,
        approval_id=approval_id,
        body=body,
        workspace=ctx,
    )


async def bulk_decide_approvals(
    workspace_id: UUID,
    decisions: list[dict[str, Any]],
    default_note: str | None = None,
) -> dict[str, Any]:
    """Decide multiple approvals in sequence (conservative, per-item).

    Each ``decisions`` entry must be a dict with:
        approval_id: str/UUID (required)
        decision: 'approve' | 'reject' (required)
        note: str | None (optional; falls back to default_note)
        category_override: str | None (bank_transaction_import only)

    Each item runs in its own request to ``decide_approval`` (its own
    transaction). On any per-item exception we capture the error and continue —
    the bulk call NEVER aborts the remaining items, but it also NEVER retries.
    Fail-closed on ambiguity: a malformed entry is skipped with an explicit
    'error' result, not silently ignored.

    Args:
        workspace_id: Workspace UUID.
        decisions: List of per-approval decision dicts (see above).
        default_note: Default reviewer note when an entry omits ``note``.

    Returns:
        {
          "results": [
            {"approval_id": "...", "ok": True,  "outcome": {...}},
            {"approval_id": "...", "ok": False, "error": "..."},
            ...
          ],
          "total": int,
          "approved": int,
          "rejected": int,
          "failed": int,
        }

    Role floor: admin (per-item decide requires admin; we enforce up-front so
    a non-admin call fails before iterating).

    Audit 2026-05-31 Bug 6: a hard ``BULK_DECIDE_MAX_ITEMS`` cap is now
    enforced. Reject BEFORE any auth work — even unauthenticated callers can
    DOS the auth chain otherwise — so cap enforcement is cheap.
    """
    # Cap check first — cheaper than any auth round-trip + protects the auth
    # path itself from being abused as a bulk-validation oracle.
    if not isinstance(decisions, list):
        raise ValueError("decisions must be a list of dicts")
    if len(decisions) > BULK_DECIDE_MAX_ITEMS:
        raise ValueError(
            f"bulk_decide_approvals accepts at most {BULK_DECIDE_MAX_ITEMS} "
            f"items per call; got {len(decisions)}. Split into smaller batches."
        )

    await authorize_workspace(workspace_id, None, min_role="admin")

    results: list[dict[str, Any]] = []
    approved = rejected = failed = 0

    for idx, item in enumerate(decisions):
        if not isinstance(item, dict):
            results.append(
                {"approval_id": None, "ok": False, "index": idx,
                 "error": "entry is not a dict"}
            )
            failed += 1
            continue
        approval_id = item.get("approval_id")
        decision = item.get("decision")
        if not approval_id or not decision:
            results.append(
                {"approval_id": str(approval_id) if approval_id else None,
                 "ok": False, "index": idx,
                 "error": "missing approval_id or decision"}
            )
            failed += 1
            continue

        note = item.get("note") or default_note
        category_override = item.get("category_override")
        try:
            outcome = await decide_approval(
                workspace_id=workspace_id,
                approval_id=UUID(str(approval_id)),
                decision=str(decision),
                note=note,
                category_override=category_override,
            )
            results.append(
                {"approval_id": str(approval_id), "ok": True, "outcome": outcome}
            )
            if outcome.get("decision") == "approved":
                approved += 1
            else:
                rejected += 1
        except Exception as exc:  # noqa: BLE001 - per-item conservative
            results.append(
                {"approval_id": str(approval_id), "ok": False, "index": idx,
                 "error": f"{type(exc).__name__}: {exc}"}
            )
            failed += 1

    return {
        "results": results,
        "total": len(decisions),
        "approved": approved,
        "rejected": rejected,
        "failed": failed,
    }


async def list_approval_recommendations(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    cluster_id: str | None = None,
    status: str = "pending",
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List approval recommendations (pre-analyzer output) for a workspace.

    Mirrors GET /api/workspaces/{wid}/approval-recommendations. Reads the
    ``approval_recommendation`` table joined with the live ``approval_queue``
    status so an agent can see what an earlier ``analyze_pending_approvals``
    run suggested.

    Args:
        workspace_id: Workspace UUID.
        entity_id: Optional entity filter.
        cluster_id: Optional — filter to a single recommendation cluster.
        status: 'pending' (default) restricts to items whose target approval is
            still pending; 'all' includes approved/rejected.
        limit: Page size (max 500). Default 100.
        offset: Page offset. Default 0.

    Returns:
        {
          "recommendations": [ ... ],
          "total": int,
          "clusters": [ {"cluster_id": "...", "count": int, ...}, ... ],
        }

    Role floor: viewer (mirrors HTTP).
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    scope_restriction = assert_entity_in_scope(ctx, entity_id)

    async with workspace_session(SessionLocal, workspace_id) as session:
        where: list[str] = ["1=1"]
        params: dict[str, Any] = {
            "limit": min(max(int(limit), 1), 500),
            "offset": max(int(offset), 0),
        }
        if entity_id:
            where.append("ar.entity_id = :eid")
            params["eid"] = str(entity_id)
        if scope_restriction is not None:
            where.append("ar.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]
        if cluster_id:
            where.append("ar.cluster_id = :cluster_id")
            params["cluster_id"] = cluster_id
        if status and status.lower() != "all":
            where.append("aq.status = :approval_status")
            params["approval_status"] = status.lower()
        where_sql = " AND ".join(where)

        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        ar.id::text AS recommendation_id,
                        ar.approval_id::text,
                        ar.cluster_id,
                        ar.recommended_decision,
                        ar.recommended_category,
                        ar.confidence,
                        ar.rationale,
                        ar.submitter_kind,
                        ar.submitter_id,
                        ar.created_at,
                        aq.entity_id::text AS entity_id,
                        aq.status AS approval_status,
                        aq.amount,
                        aq.currency_code,
                        aq.payload,
                        aq.created_at AS approval_created_at
                    FROM approval_recommendation ar
                    JOIN approval_queue aq ON aq.id = ar.approval_id
                    WHERE {where_sql}
                    ORDER BY ar.created_at DESC, ar.id DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

        total = (
            await session.execute(
                text(
                    f"""
                    SELECT count(*)
                    FROM approval_recommendation ar
                    JOIN approval_queue aq ON aq.id = ar.approval_id
                    WHERE {where_sql}
                    """
                ),
                {k: v for k, v in params.items() if k not in ("limit", "offset")},
            )
        ).scalar_one()

    def _b(v: Any) -> Any:
        if v is None or isinstance(v, (int, float, bool, str, dict, list)):
            return v
        return str(v)

    return {
        "recommendations": [
            {k: _b(v) for k, v in dict(r).items()} for r in rows
        ],
        "total": int(total),
    }
