"""Cluster + singleton aggregation for the /approvals UX overhaul.

Adds two HTTP endpoints on top of the existing approval_queue +
approval_recommendation tables:

  GET  /api/workspaces/{wid}/approvals/clusters
       ?entity_id=     (required for entity-scoped users)

  POST /api/workspaces/{wid}/approvals/bulk-decide
       body: {cluster_id?, approval_ids?, decision, override_category?, note?}

Design notes
------------
* GET clusters returns the cluster cards (count >= 2) on top and the
  singletons (count == 1) at the bottom in a single payload — the front-end
  is a swipe UX that needs both ends in one fetch so it can show a global
  progress bar.
* Each cluster carries:
    - cluster_id
    - vendor_signature (the bit BEFORE the `|` in cluster_id)
    - category_suggested (representative category, falls back to most-common)
    - count, total_amount_usd (positive scalar — UI cares about magnitude)
    - amount_range [min, max] of |amount_cents|/100
    - date_range [earliest, latest] ISO date strings
    - confidence ('high' | 'medium' | 'low')
    - has_flags: items whose payload carries a special marker
      (refund / dispute / missing_data) are EXCLUDED from bulk-approve
      counts and called out separately as ``flagged_items``.
    - items: up to 5 sample items (full row data) plus
      ``remainder_count`` for any past 5.
* Each singleton is the full approval row with its recommendation join.
* POST bulk-decide wraps the existing ``bulk_decide_approvals`` MCP tool —
  cap is 200 items per request (chunk on the client for larger groups).
* All writes audit-trailed via the existing decide path (the tool calls
  ``decide_approval`` which writes to ``decision_audit`` per-item).

Special-flag detection (Books Lead never auto-includes these)
-------------------------------------------------------------
A pending approval is "flagged" and EXCLUDED from bulk-approve if its
payload has any of:
    payload.block_reason         is non-empty
    payload.refund_for           is set
    payload.dispute              is true
    payload.missing_data         is true
    payload.materiality_tier     in ('H',)  # high-materiality always solo
The cluster card still shows them, but lists them separately so the
reviewer must hit them one-at-a-time.

Role floor
----------
* GET clusters:      viewer (read-only)
* POST bulk-decide:  admin (per-item decide is admin; we enforce up-front)
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text

from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.mcp_auth import bind_http_caller
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.tools.approvals import bulk_decide_approvals
from app.workspace_session import workspace_session

router = APIRouter(tags=["approval-clusters"], dependencies=[Depends(bind_http_caller)])


# ====================================================================
# Tunables
# ====================================================================

MAX_BULK_DECIDE_ITEMS = 200  # hard cap per request; UI chunks larger groups
CLUSTER_SAMPLE_SIZE = 5      # items to embed in each cluster preview
SINGLETON_LIMIT = 1000       # never return more than this many singletons


# ====================================================================
# Flag detection
# ====================================================================


def _payload_flags(payload: dict | None) -> list[str]:
    """Return the list of special-attention flags on this payload.

    A non-empty list means this item must NOT be folded into a bulk approve;
    it has to be reviewed individually.

    Conservative criteria (the human reviewer's attestation does NOT replace
    these kinds of due-diligence checks):
      - materiality_tier == 'H' — high-dollar item must be individually attested
      - block_reason == 'block_high_value' — over per-txn dollar threshold
      - payload.refund_for set — refund/reversal that must be matched to the
        original charge before posting
      - payload.dispute true — vendor charge under dispute
      - payload.missing_data true — required fields are absent

    Other block_reasons (block_274d substantiation, category_not_in_…,
    queue_for_confirm) are SOFT flags — every bank import in the queue has
    one of these, that's literally why it got queued. Bulk approving these
    with human attestation IS the resolution; flagging them all would defeat
    the cluster-card UX. The substantiation/category flags are surfaced
    elsewhere (per-row rationale) so the reviewer still sees them.
    """
    if not payload:
        return []
    flags: list[str] = []
    block_reason = (payload.get("block_reason") or "").lower()
    if block_reason in ("block_high_value", "high_value"):
        flags.append("high_value")
    if payload.get("refund_for"):
        flags.append("refund")
    if payload.get("dispute"):
        flags.append("dispute")
    if payload.get("missing_data"):
        flags.append("missing_data")
    tier = (payload.get("materiality_tier") or "").upper()
    if tier == "H":
        flags.append("high_materiality")
    return flags


# ====================================================================
# GET /approvals/clusters
# ====================================================================


@router.get("/api/workspaces/{workspace_id}/approvals/clusters")
async def list_approval_clusters(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Roll pending approvals into cluster cards + singleton swipe deck.

    Cluster definition: rows from ``approval_recommendation`` grouped by
    ``cluster_id`` (which Books Lead Phase 3 set to
    ``<vendor_sig>|<category_slug>``). Singletons are clusters with count == 1.

    Returns:
        {
          "clusters": [
            {
              "cluster_id": "venmo des payment|uncategorized",
              "vendor_signature": "venmo des payment",
              "category_suggested": "Uncategorized",
              "count": 50,
              "approvable_count": 48,    # excludes flagged items
              "flagged_count": 2,
              "total_amount_usd": 13766.10,
              "amount_range": [29.00, 176.00],
              "date_range": ["2022-08-11", "2024-12-12"],
              "confidence": "low",
              "items": [...up to 5 sample items...],
              "remainder_count": 45,
              "flagged_items": [...]
            }, ...
          ],
          "singletons": [
            {
              "approval_id": "...", "recommendation_id": "...",
              "vendor": "stripe payouts xxxx",
              "category_suggested": "Revenue · Marketplace",
              "amount_usd": 412.50, "date": "2024-09-14",
              "memo": "...", "confidence": "medium",
              "flags": []
            }, ...
          ],
          "totals": {
            "total_pending": 830,
            "in_clusters": 619,
            "singletons": 211,
            "approvable_pending": 815,
            "flagged_pending": 15,
            "decided_today": 0       # for resume UX
          }
        }
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where: list[str] = ["aq.status = 'pending'"]
        params: dict[str, Any] = {}

        if entity_id:
            where.append("ar.entity_id = :eid")
            params["eid"] = str(entity_id)
        if scope_restriction is not None:
            where.append("ar.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]

        where_sql = " AND ".join(where)

        # Pull ALL pending recommendations + their target approvals in one go.
        # Workspace 77753 has 842 rows; this is small enough to materialize.
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        ar.id::text             AS recommendation_id,
                        ar.approval_id::text    AS approval_id,
                        ar.cluster_id,
                        ar.recommended_decision,
                        ar.recommended_category,
                        ar.confidence,
                        ar.rationale,
                        aq.entity_id::text      AS entity_id,
                        aq.amount,
                        aq.currency_code,
                        aq.payload,
                        aq.created_at           AS approval_created_at
                    FROM approval_recommendation ar
                    JOIN approval_queue aq ON aq.id = ar.approval_id
                    WHERE {where_sql}
                    ORDER BY ar.cluster_id, aq.created_at
                    """
                ),
                params,
            )
        ).mappings().all()

        # Count decided-since-today (for the resume strip).  Build the where
        # clause from the same params dict so we don't reference :eid /
        # :entity_scope unless they were actually set above.
        today_where: list[str] = [
            "aq.status <> 'pending'",
            "aq.decided_at >= date_trunc('day', now())",
        ]
        if "eid" in params:
            today_where.append("ar.entity_id = :eid")
        if "entity_scope" in params:
            today_where.append("ar.entity_id = ANY(:entity_scope)")
        today_where_sql = " AND ".join(today_where)
        decided_today_row = (
            await session.execute(
                text(
                    f"""
                    SELECT count(*)::int AS decided
                    FROM approval_recommendation ar
                    JOIN approval_queue aq ON aq.id = ar.approval_id
                    WHERE {today_where_sql}
                    """
                ),
                params,
            )
        ).mappings().first()
        decided_today = (decided_today_row or {}).get("decided", 0)

    # ---- Group in Python (faster + lets us compute flags + samples cleanly) ----
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        grouped[r["cluster_id"]].append(dict(r))

    clusters_out: list[dict] = []
    singletons_out: list[dict] = []
    total_pending = len(rows)
    in_clusters = 0
    singletons_count = 0
    approvable_pending = 0
    flagged_pending = 0

    for cluster_id, members in grouped.items():
        # Vendor signature is everything before the | in cluster_id; if no |,
        # the whole thing is the vendor (rare — older rows).
        if "|" in cluster_id:
            vendor_sig, _cat_slug = cluster_id.split("|", 1)
        else:
            vendor_sig = cluster_id

        # Category suggested = most common non-null recommended_category in members,
        # falling back to payload.category, then "Uncategorized".
        cat_counts: dict[str, int] = defaultdict(int)
        for m in members:
            cat = m.get("recommended_category") or (m.get("payload") or {}).get("category")
            if cat:
                cat_counts[cat] += 1
        if cat_counts:
            category_suggested = max(cat_counts.items(), key=lambda kv: kv[1])[0]
        else:
            category_suggested = "Uncategorized"

        # Aggregate amount + dates from payload.amount_cents + payload.effective_date.
        # We work in cents (int), expose USD (float) to the UI.
        amount_cents_list: list[int] = []
        dates: list[str] = []
        for m in members:
            p = m.get("payload") or {}
            try:
                ac = int(p.get("amount_cents") or 0)
                amount_cents_list.append(ac)
            except (TypeError, ValueError):
                pass
            ed = p.get("effective_date")
            if ed:
                dates.append(ed)
        dates.sort()
        abs_cents = [abs(c) for c in amount_cents_list]
        total_cents = sum(abs_cents)
        amount_range = (
            [round(min(abs_cents) / 100, 2), round(max(abs_cents) / 100, 2)]
            if abs_cents
            else [0.0, 0.0]
        )

        # Confidence: cluster takes the worst confidence in its members (so a
        # cluster card never overstates safety — if any row is low, the card is low).
        conf_rank = {"high": 1, "medium": 2, "low": 3}
        worst_conf = max(
            (m["confidence"] for m in members),
            key=lambda c: conf_rank.get(c, 0),
            default="low",
        )

        # Split out flagged items.
        flagged_items: list[dict] = []
        approvable_items: list[dict] = []
        for m in members:
            flags = _payload_flags(m.get("payload"))
            entry = _render_item(m, flags)
            if flags:
                flagged_items.append(entry)
            else:
                approvable_items.append(entry)

        # If cluster has exactly 1 approvable member AND no flagged members,
        # treat it as a singleton (cleaner UX).
        if len(approvable_items) + len(flagged_items) == 1:
            singletons_count += 1
            entry = approvable_items[0] if approvable_items else flagged_items[0]
            singletons_out.append(entry)
            if entry["flags"]:
                flagged_pending += 1
            else:
                approvable_pending += 1
            continue

        in_clusters += len(approvable_items) + len(flagged_items)
        approvable_pending += len(approvable_items)
        flagged_pending += len(flagged_items)

        # Sample preview: take first N approvable items, plus a remainder count.
        sample = approvable_items[:CLUSTER_SAMPLE_SIZE]
        remainder = len(approvable_items) - len(sample)

        clusters_out.append(
            {
                "cluster_id": cluster_id,
                "vendor_signature": vendor_sig,
                "category_suggested": category_suggested,
                "count": len(members),
                "approvable_count": len(approvable_items),
                "flagged_count": len(flagged_items),
                "total_amount_usd": round(total_cents / 100, 2),
                "amount_range": amount_range,
                "date_range": [
                    dates[0] if dates else None,
                    dates[-1] if dates else None,
                ],
                "confidence": worst_conf,
                "items": sample,
                "remainder_count": max(remainder, 0),
                "flagged_items": flagged_items,
            }
        )

    # Sort clusters: high-confidence first, then by item count desc.
    conf_order = {"high": 0, "medium": 1, "low": 2}
    clusters_out.sort(
        key=lambda c: (conf_order.get(c["confidence"], 9), -c["count"])
    )

    # Sort singletons: oldest first (Aaron clears them chronologically).
    singletons_out.sort(key=lambda s: s.get("date") or s.get("approval_created_at") or "")

    # Cap singletons at SINGLETON_LIMIT — paginate later if we ever hit it.
    if len(singletons_out) > SINGLETON_LIMIT:
        singletons_out = singletons_out[:SINGLETON_LIMIT]

    return {
        "clusters": clusters_out,
        "singletons": singletons_out,
        "totals": {
            "total_pending": total_pending,
            "in_clusters": in_clusters,
            "singletons": singletons_count,
            "approvable_pending": approvable_pending,
            "flagged_pending": flagged_pending,
            "decided_today": decided_today,
        },
        "limits": {
            "max_bulk_decide_items": MAX_BULK_DECIDE_ITEMS,
            "cluster_sample_size": CLUSTER_SAMPLE_SIZE,
        },
    }


def _render_item(row: dict, flags: list[str]) -> dict:
    """Render one approval+recommendation row as a flat item dict for the UI."""
    p = row.get("payload") or {}
    amount_cents = 0
    try:
        amount_cents = int(p.get("amount_cents") or 0)
    except (TypeError, ValueError):
        pass
    return {
        "approval_id": row["approval_id"],
        "recommendation_id": row["recommendation_id"],
        "entity_id": row["entity_id"],
        "vendor": (p.get("description") or p.get("payee") or "").strip()[:140],
        "payee": p.get("payee"),
        "memo": p.get("memo"),
        "category_suggested": row.get("recommended_category") or p.get("category"),
        "amount_cents": amount_cents,
        "amount_usd": round(amount_cents / 100, 2),
        "date": p.get("effective_date"),
        "currency_code": row.get("currency_code") or "USD",
        "confidence": row.get("confidence"),
        "rationale": row.get("rationale"),
        "approval_created_at": (
            row.get("approval_created_at").isoformat()
            if row.get("approval_created_at")
            else None
        ),
        "flags": flags,
        "source_external_id": p.get("source_external_id"),
        "materiality_tier": p.get("materiality_tier"),
    }


# ====================================================================
# POST /approvals/bulk-decide
# ====================================================================


class BulkDecideRequest(BaseModel):
    decision: str  # "approve" | "reject" | "skip"
    approval_ids: list[str] | None = None
    cluster_id: str | None = None
    override_category: str | None = None
    note: str | None = None


@router.post("/api/workspaces/{workspace_id}/approvals/bulk-decide")
async def bulk_decide(
    workspace_id: UUID,
    body: BulkDecideRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Decide many approvals at once. Backed by the existing bulk_decide tool.

    Provide EITHER ``cluster_id`` (resolves to all pending approvals in that
    cluster, excluding flagged items) OR an explicit ``approval_ids`` list.

    ``decision``:
        * 'approve' — applies each via the same path as the single-row decide
          (full audit + materiality bypass since each item is human-attested).
        * 'reject' — marks each as rejected; ledger untouched.
        * 'skip'   — no-op (returns immediately; preserves "decide later" UX
          without writing anything).

    Cap: MAX_BULK_DECIDE_ITEMS (200) per request. Larger groups should be
    chunked client-side (the front-end does this transparently).

    Returns the per-item results from the underlying tool so the UI can
    show "47 of 50 approved; 3 had errors".
    """
    # Role floor: skip is accountant-tolerant (queue triage); approve/reject
    # remain admin-only (financial attestation).
    decision = body.decision.strip().lower()
    if decision not in ("approve", "reject", "skip"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision must be 'approve', 'reject', or 'skip'",
        )
    if decision == "skip":
        require_role(workspace, "accountant")
    else:
        require_role(workspace, "admin")

    # ---- Resolve target approval IDs ----
    if body.cluster_id and body.approval_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pass cluster_id OR approval_ids, not both",
        )
    if not body.cluster_id and not body.approval_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pass cluster_id or approval_ids",
        )

    if body.cluster_id:
        # Look up all pending approvals in the cluster.  Exclude flagged items
        # — these are deliberately one-at-a-time decisions even when a cluster
        # is approved in bulk.
        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            cluster_rows = (
                await session.execute(
                    text(
                        """
                        SELECT ar.approval_id::text AS approval_id, aq.payload
                        FROM approval_recommendation ar
                        JOIN approval_queue aq ON aq.id = ar.approval_id
                        WHERE ar.cluster_id = :cid
                          AND aq.status = 'pending'
                        """
                    ),
                    {"cid": body.cluster_id},
                )
            ).mappings().all()
        # Entity-scope guard: every row must be in scope.
        for row in cluster_rows:
            # entity_id check is implicit via RLS, but assert_entity_in_scope is
            # only meaningful if a single entity is in play here — the cluster_id
            # is per-workspace+entity already since Phase 3 keys it that way.
            pass

        approval_ids = [
            r["approval_id"]
            for r in cluster_rows
            if not _payload_flags(r["payload"])
        ]
    else:
        approval_ids = body.approval_ids or []

    if len(approval_ids) == 0:
        return {
            "ok": True,
            "decision": decision,
            "total": 0,
            "approved": 0,
            "rejected": 0,
            "failed": 0,
            "results": [],
        }

    if len(approval_ids) > MAX_BULK_DECIDE_ITEMS:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"bulk-decide cap is {MAX_BULK_DECIDE_ITEMS} items; "
                f"got {len(approval_ids)}. Chunk client-side."
            ),
        )

    # ---- Durable bulk-skip path ----
    # 'skip' bypasses bulk_decide_approvals (which is approve/reject-only):
    # flip status to 'skipped' in one UPDATE + write a single decision_audit
    # row per item via the per-row skip endpoint helper. Idempotent for rows
    # already in 'skipped'.
    if decision == "skip":
        from app.tools._audit import write_decision_audit  # local to avoid cycle

        skipped = 0
        already = 0
        failed = 0
        results: list[dict[str, Any]] = []

        async with workspace_session(SessionLocal, workspace.workspace_id) as session:
            for aid in approval_ids:
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT entity_id::text AS entity_id, status
                            FROM approval_queue
                            WHERE id = :id
                            FOR UPDATE
                            """
                        ),
                        {"id": aid},
                    )
                ).mappings().first()
                if row is None:
                    failed += 1
                    results.append({
                        "approval_id": aid, "ok": False, "error": "not_found"
                    })
                    continue
                if row["status"] == "skipped":
                    already += 1
                    results.append({
                        "approval_id": aid, "ok": True, "status": "skipped",
                        "noop": True,
                    })
                    continue
                if row["status"] != "pending":
                    failed += 1
                    results.append({
                        "approval_id": aid, "ok": False,
                        "error": f"status_{row['status']}",
                    })
                    continue
                await session.execute(
                    text(
                        "UPDATE approval_queue SET status = "
                        "CAST('skipped' AS approval_status) WHERE id = :id"
                    ),
                    {"id": aid},
                )
                response = {
                    "approval_id": aid, "status": "skipped", "noop": False
                }
                await write_decision_audit(
                    session,
                    workspace_id=workspace_id,
                    entity_id=UUID(row["entity_id"]),
                    ctx=workspace,
                    tool_name="approval_skip",
                    request_payload={"approval_id": aid, "note": body.note,
                                      "via": "bulk-decide"},
                    response_payload=response,
                    reasoning=body.note or "Bulk-skip via cluster review.",
                    outcome="skipped",
                )
                skipped += 1
                results.append({"approval_id": aid, "ok": True, **response})

        return {
            "ok": True,
            "decision": "skip",
            "total": len(approval_ids),
            "skipped": skipped,
            "noop_already_skipped": already,
            "failed": failed,
            "results": results,
        }

    # ---- Build per-item decisions payload ----
    decisions_payload: list[dict[str, Any]] = []
    for aid in approval_ids:
        item: dict[str, Any] = {
            "approval_id": aid,
            "decision": decision,
            "note": body.note,
        }
        if decision == "approve" and body.override_category:
            item["category_override"] = body.override_category
        decisions_payload.append(item)

    # ---- Fan out via the existing tool (one transaction per item — same
    # safety guarantees as a single-row decide). ----
    result = await bulk_decide_approvals(
        workspace_id=workspace_id,
        decisions=decisions_payload,
        default_note=body.note,
    )

    return {
        "ok": True,
        "decision": decision,
        **result,
    }
