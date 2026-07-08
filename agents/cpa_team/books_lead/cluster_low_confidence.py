"""Phase 3 — cluster pending low-confidence approvals into vendor groups.

The analyzer's existing ``cluster_id`` encodes the *transaction's*
description (which includes per-txn IDs and dates), so each cluster ends up
size 1. That defeats the cluster-card UX: the owner would see one card per
transaction even when 12 of them are the same recurring Anthropic API charge.

Phase 3 rewrites ``approval_recommendation.cluster_id`` for all pending
low-confidence rows so that *similar* transactions share a key:

  cluster_id = "{vendor_sig}|{recommended_category}"

(``vendor_sig`` is the normalized vendor signature from Phase 2;
``recommended_category`` lower-cases + dash-slug, falling back to
"uncategorized".)

This way the existing GET
``/api/workspaces/{wid}/approval-recommendations`` endpoint will report each
cluster's count + total + sample payees, and the owner can click ONE cluster
("approve all 12 Anthropic API charges as SaaS · AI?") rather than 12
individual approvals.

We deliberately do NOT change ``recommended_decision`` or ``confidence`` —
those remain ``'flag_for_review'`` / ``'low'`` so the cluster card lands in
the review queue, not the auto-apply path. The grouping is a UX improvement,
not a categorization shortcut.

For rows whose vendor signature can't be derived (very short or empty
descriptions) we fall back to ``cluster_id = "_singletons|<category_slug>"``
so they're still grouped by category at the bottom of the list.

Also — we update kept-medium rows from Phase 2 with the same vendor-grouped
cluster_id, so the medium cluster cards behave the same way (this is a
no-op if Phase 2 already ran).

Run:
    docker exec -t accounting_ops-app-1 \
        python -m agents.cpa_team.books_lead.cluster_low_confidence
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.workspace_session import workspace_session

from agents.cpa_team.books_lead.apply_high_confidence import ENTITY_ID, WORKSPACE_ID
from agents.cpa_team.books_lead.promote_medium_to_high import vendor_signature

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("books-lead.phase3")


_SLUG_PUNCT = re.compile(r"[^a-z0-9]+")


def category_slug(category: str | None) -> str:
    if not category:
        return "uncategorized"
    return _SLUG_PUNCT.sub("-", category.lower()).strip("-")[:48] or "uncategorized"


def vendor_cluster_key(description: str | None, category: str | None) -> str:
    """Build the new cluster_id from vendor signature + category slug."""
    sig = vendor_signature(description)
    cat = category_slug(category)
    if not sig:
        return f"_singletons|{cat}"
    # The pipe is safe in the existing TEXT column and grep-friendly in logs.
    return f"{sig}|{cat}"


@dataclass
class PendingRec:
    approval_id: UUID
    payload: dict[str, Any]
    recommended_category: str | None
    confidence: str
    rationale: str
    new_cluster_id: str


async def fetch_low_and_medium_pending(
    session: AsyncSession,
) -> list[PendingRec]:
    rows = (
        await session.execute(
            text(
                """
                SELECT aq.id::text                AS approval_id,
                       aq.payload                 AS payload,
                       ar.recommended_category    AS recommended_category,
                       ar.confidence              AS confidence,
                       ar.rationale               AS rationale
                FROM approval_queue aq
                JOIN approval_recommendation ar ON ar.approval_id = aq.id
                WHERE aq.entity_id = :eid
                  AND aq.status = 'pending'
                  AND aq.subject_type = 'bank_transaction_import'
                  AND ar.confidence IN ('low', 'medium')
                ORDER BY aq.created_at ASC
                """
            ),
            {"eid": str(ENTITY_ID)},
        )
    ).mappings().all()
    out: list[PendingRec] = []
    for r in rows:
        payload: dict = r["payload"] or {}
        desc = payload.get("description")
        cat = r["recommended_category"] or payload.get("category")
        new_key = vendor_cluster_key(desc, cat)
        out.append(
            PendingRec(
                approval_id=UUID(r["approval_id"]),
                payload=payload,
                recommended_category=cat,
                confidence=r["confidence"],
                rationale=r["rationale"] or "",
                new_cluster_id=new_key,
            )
        )
    return out


async def update_cluster_id(
    session: AsyncSession, *, approval_id: UUID, cluster_id: str
) -> None:
    await session.execute(
        text(
            """
            UPDATE approval_recommendation
            SET cluster_id = :cluster_id,
                submitter_kind = 'agent',
                submitter_id = COALESCE(submitter_id, 'books-lead/phase3')
            WHERE approval_id = :aid
            """
        ),
        {"cluster_id": cluster_id, "aid": str(approval_id)},
    )


@dataclass
class ClusterRollup:
    cluster_id: str
    vendor_sig: str
    category: str
    confidence_breakdown: dict[str, int]
    count: int
    total_amount_cents: int
    sample_descriptions: list[str]
    sample_approval_ids: list[str]
    date_range: tuple[str | None, str | None]  # (earliest, latest)


def build_rollups(pendings: list[PendingRec]) -> list[ClusterRollup]:
    grouped: dict[str, list[PendingRec]] = defaultdict(list)
    for p in pendings:
        grouped[p.new_cluster_id].append(p)

    rollups: list[ClusterRollup] = []
    for cluster_id, members in grouped.items():
        if "|" in cluster_id:
            vendor_sig, cat = cluster_id.split("|", 1)
        else:
            vendor_sig, cat = cluster_id, ""
        breakdown: dict[str, int] = defaultdict(int)
        total_cents = 0
        descs: list[str] = []
        aids: list[str] = []
        dates: list[str] = []
        for m in members:
            breakdown[m.confidence] += 1
            try:
                total_cents += int(m.payload.get("amount_cents") or 0)
            except (TypeError, ValueError):
                pass
            d = (m.payload.get("description") or "").strip()
            if d and d not in descs:
                descs.append(d[:120])
            aids.append(str(m.approval_id))
            ed = m.payload.get("effective_date")
            if ed:
                dates.append(ed)
        dates.sort()
        rollups.append(
            ClusterRollup(
                cluster_id=cluster_id,
                vendor_sig=vendor_sig,
                category=cat,
                confidence_breakdown=dict(breakdown),
                count=len(members),
                total_amount_cents=total_cents,
                sample_descriptions=descs[:3],
                sample_approval_ids=aids[:5],
                date_range=(dates[0] if dates else None, dates[-1] if dates else None),
            )
        )
    # Sort: largest cluster first
    rollups.sort(key=lambda c: -c.count)
    return rollups


async def main() -> int:
    log.info(
        "Phase 3 starting — workspace=%s entity=%s",
        WORKSPACE_ID,
        ENTITY_ID,
    )

    # ---- Fetch + plan ----
    async with workspace_session(SessionLocal, WORKSPACE_ID) as session:
        pendings = await fetch_low_and_medium_pending(session)
    log.info(
        "Loaded %d pending low+medium recommendations for clustering",
        len(pendings),
    )

    rollups = build_rollups(pendings)
    log.info("Built %d clusters", len(rollups))

    # ---- Write updated cluster_id per recommendation (one tx per batch) ----
    # We do all updates in ONE transaction — it's just a TEXT column rewrite
    # and we want the cluster view to flip atomically.
    async with workspace_session(SessionLocal, WORKSPACE_ID) as session:
        for p in pendings:
            await update_cluster_id(
                session,
                approval_id=p.approval_id,
                cluster_id=p.new_cluster_id,
            )

    # ---- Summary ----
    total_decisions_after = len(rollups)  # one cluster-card per cluster
    print()
    print("=" * 72)
    print("PHASE 3 SUMMARY — vendor clustering of low + medium pending approvals")
    print("=" * 72)
    print(f"  Pending recommendations clustered: {len(pendings)}")
    print(f"  Distinct vendor clusters built:    {len(rollups)}")
    print(
        "  the owner now sees ~"
        f"{total_decisions_after} cluster cards "
        "instead of one-per-transaction."
    )
    print()
    print("Top 30 clusters by size:")
    print()
    print(
        f"  {'size':>4}  {'L/M':>5}  {'amount':>12}  "
        f"{'date range':>21}  cluster_id"
    )
    print(f"  {'----':>4}  {'-----':>5}  {'------':>12}  "
          f"{'----------':>21}  {'---------'}")
    for c in rollups[:30]:
        lo = c.confidence_breakdown.get("low", 0)
        me = c.confidence_breakdown.get("medium", 0)
        amt = c.total_amount_cents / 100
        d0, d1 = c.date_range
        drange = f"{d0 or '?'}..{d1 or '?'}"
        print(
            f"  {c.count:>4}  {lo:>2}/{me:>2}  "
            f"{amt:>12,.2f}  {drange:>21}  {c.cluster_id}"
        )
    if len(rollups) > 30:
        print(f"  ...and {len(rollups) - 30} smaller clusters")

    # ---- Cluster-size distribution ----
    size_buckets: dict[str, int] = defaultdict(int)
    for c in rollups:
        if c.count >= 10:
            size_buckets[">=10"] += 1
        elif c.count >= 5:
            size_buckets["5-9"] += 1
        elif c.count >= 3:
            size_buckets["3-4"] += 1
        elif c.count == 2:
            size_buckets["2"] += 1
        else:
            size_buckets["1"] += 1
    print()
    print("Cluster size distribution:")
    for k in (">=10", "5-9", "3-4", "2", "1"):
        if k in size_buckets:
            print(f"  {k:>5}: {size_buckets[k]} clusters")

    multi_member = sum(1 for c in rollups if c.count >= 3)
    print()
    print(f"  Clusters with >= 3 members (the 'batch card' candidates): {multi_member}")
    decisions_in_multi = sum(c.count for c in rollups if c.count >= 3)
    singletons = sum(c.count for c in rollups if c.count < 3)
    print(
        f"  Pendings folded into multi-clusters: {decisions_in_multi}; "
        f"singletons (1-2 in a cluster): {singletons}"
    )
    print()
    print(
        "Net decision count after clustering ≈ "
        f"{multi_member} cluster cards + {singletons} singleton cards "
        f"= {multi_member + singletons}"
    )
    print("=" * 72)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
