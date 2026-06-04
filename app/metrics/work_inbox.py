"""Cross-client CPA "work inbox" — the pure merge / sort / cap aggregator.

WHY THIS EXISTS
---------------
The "10x CPA" surface ``/clients`` shows cross-client *counts*, but every
actual actionable item (a pending approval, an open tax-review item) lives in a
*per-workspace* queue. To work the items a CPA has to switch workspaces ~20
times. ``GET /api/me/work-inbox`` (``app/api/work_inbox.py``) closes that gap by
iterating every workspace the caller can act in, pulling the OPEN items from
each (inside that workspace's ``workspace_session`` so RLS + search_path are
pinned), and returning ONE merged, stalest-first list.

The function here is intentionally PURE — exactly like
``app/metrics/ai_agreement.py``. It takes already-fetched items (the API does
all the DB work) and returns the merged/sorted/capped envelope. Keeping the
merge + cap + totals math DB-free makes the load-bearing logic
(sort order, cap accounting, the truncated/dropped contract) deterministically
unit-testable without a live stack.

THE CAP CONTRACT (load-bearing — NEVER silently truncate)
---------------------------------------------------------
Two caps protect the response from a runaway workspace:

  * ``max_per_workspace`` — at most N items from any single workspace. A CPA
    with one workspace holding 10,000 pending approvals must not crowd every
    other client out of the inbox. Per-workspace overflow is dropped *after*
    the stalest-first sort, so the OLDEST (most urgent) items from that
    workspace survive.
  * ``max_total`` — at most N items overall, applied AFTER per-workspace
    capping, again keeping the stalest.

Whenever EITHER cap drops an item, ``truncated`` is ``True`` and ``dropped`` is
the exact number of actionable items that exist but are not in ``items``. The
count is always visible so the UI can say "showing 300 of 412 — switch into the
busiest client to see the rest". ``dropped`` counts BOTH per-workspace and
total overflow (an item dropped by the per-workspace cap is counted once).

SORT ORDER
----------
``items`` is sorted by ``age_hours`` DESC — stalest first — because the oldest
unactioned item is the most overdue. Ties break by ``workspace_display_name``
then ``created_at`` (ISO string) so the order is deterministic for tests and
stable for the UI. The per-workspace cap uses the SAME ordering, so capping
keeps each workspace's oldest items.

OUTPUT SHAPE
------------
``build_work_inbox`` returns::

    {
      "items": [ WorkInboxItem-as-dict, ... ],   # stalest first, capped
      "totals": {
        "total": int,                            # len(items) AFTER capping
        "by_item_type": {"approval": int, "tax_review": int, ...},
        "by_workspace": [                        # desc by count, then name
            {"workspace_id": str, "display_name": str, "count": int}, ...
        ],
      },
      "truncated": bool,                         # True iff any item was dropped
      "dropped": int,                            # exact # of items not shown
    }

The ``by_item_type`` / ``by_workspace`` totals describe the RETURNED
(post-cap) population — they sum to ``total``, never to total+dropped — so the
UI badge ("12 approvals · 4 reviews") always matches what is on screen.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

# Default caps. The API passes these explicitly; tests override them to make the
# overflow paths small and obvious.
DEFAULT_MAX_TOTAL = 300
DEFAULT_MAX_PER_WORKSPACE = 100

# The item_type discriminator values this inbox understands. Surfaced as the
# stable keys in ``totals.by_item_type`` (both always present, even at 0, so the
# UI shape never shifts).
ITEM_TYPE_APPROVAL = "approval"
ITEM_TYPE_TAX_REVIEW = "tax_review"
_KNOWN_ITEM_TYPES = (ITEM_TYPE_APPROVAL, ITEM_TYPE_TAX_REVIEW)


@dataclass(frozen=True)
class WorkInboxItem:
    """One actionable item in the cross-client work inbox.

    Fields mirror the JSON contract one-for-one. Optional fields default to
    ``None`` so an approval (which has amount/recommended_*) and a tax-review
    item (which has severity) can share the one shape; the absent fields stay
    ``None`` rather than disappearing, so the response shape is uniform.

    ``age_hours`` is the load-bearing sort key (stalest first). It is computed
    by the API from ``created_at`` relative to "now" and passed in; the pure
    function never reads a clock, which keeps it deterministic.
    """

    workspace_id: str
    workspace_display_name: str
    entity_id: str | None
    item_type: str  # 'approval' | 'tax_review'
    subject_type: str | None
    title: str
    created_at: str | None  # ISO-8601
    age_hours: float
    deep_link: str
    # The item's own id (approval_queue.id / tax_review_queue.id), scoped to its
    # workspace_id. Required for the UI to act on it (inline approve/reject calls
    # the per-workspace decide endpoint) + to deep-link to the exact item.
    # Defaulted so older test fixtures that don't set it still construct; the API
    # always provides a real value.
    id: str = ""
    amount: str | None = None
    currency_code: str | None = None
    recommended_decision: str | None = None
    recommended_category: str | None = None
    confidence: str | None = None
    severity: str | None = None


def _coerce_items(
    items: Iterable[WorkInboxItem | Mapping[str, Any]],
) -> list[WorkInboxItem]:
    """Accept either WorkInboxItem instances or row-mappings (e.g. API dicts).

    The API builds plain dicts per row and hands them straight through; tests
    pass typed :class:`WorkInboxItem` objects. A mapping missing a required
    field is skipped defensively rather than crashing the whole inbox on one
    malformed record (a single broken workspace must not 500 the surface).
    """
    out: list[WorkInboxItem] = []
    for it in items:
        if isinstance(it, WorkInboxItem):
            out.append(it)
            continue
        try:
            out.append(
                WorkInboxItem(
                    workspace_id=str(it["workspace_id"]),
                    workspace_display_name=str(it["workspace_display_name"]),
                    entity_id=(
                        str(it["entity_id"])
                        if it.get("entity_id") is not None
                        else None
                    ),
                    item_type=str(it["item_type"]),
                    subject_type=(
                        str(it["subject_type"])
                        if it.get("subject_type") is not None
                        else None
                    ),
                    title=str(it.get("title") or ""),
                    created_at=(
                        str(it["created_at"])
                        if it.get("created_at") is not None
                        else None
                    ),
                    age_hours=float(it.get("age_hours") or 0.0),
                    deep_link=str(it.get("deep_link") or ""),
                    id=str(it.get("id") or ""),
                    amount=(
                        str(it["amount"]) if it.get("amount") is not None else None
                    ),
                    currency_code=it.get("currency_code"),
                    recommended_decision=it.get("recommended_decision"),
                    recommended_category=it.get("recommended_category"),
                    confidence=it.get("confidence"),
                    severity=it.get("severity"),
                )
            )
        except (KeyError, TypeError, ValueError):
            # Malformed row -> skip it. The inbox degrades gracefully.
            continue
    return out


def _sort_key(item: WorkInboxItem) -> tuple:
    """Stalest first: age_hours DESC, then stable tiebreakers.

    Returns a key for ascending ``sort`` that puts the OLDEST item first:
      * ``-age_hours``                 — larger age (older) sorts earlier;
      * ``workspace_display_name``     — deterministic grouping on ties;
      * ``created_at`` (ISO string)    — older ISO timestamp sorts earlier on
                                         a per-workspace tie (lexicographic on
                                         ISO-8601 == chronological).
    """
    return (
        -item.age_hours,
        item.workspace_display_name,
        item.created_at or "",
    )


def build_work_inbox(
    items: Iterable[WorkInboxItem | Mapping[str, Any]],
    *,
    max_total: int = DEFAULT_MAX_TOTAL,
    max_per_workspace: int = DEFAULT_MAX_PER_WORKSPACE,
) -> dict[str, Any]:
    """Merge cross-workspace actionable items into the stalest-first inbox.

    Pipeline (see module docstring for the full contract):
      1. Coerce + globally sort stalest-first (age_hours DESC, stable ties).
      2. Per-workspace cap: keep at most ``max_per_workspace`` per workspace,
         dropping that workspace's NEWEST overflow (the sort already put the
         oldest first). Count every drop.
      3. Total cap: from the per-workspace-capped, still-stalest-first list,
         keep at most ``max_total``. Count every further drop.
      4. Build ``totals`` over the RETURNED population + the visible
         ``truncated`` / ``dropped`` accounting.

    Args:
        items: iterable of :class:`WorkInboxItem` or row-mappings.
        max_total: hard cap on returned items (<=0 disables — returns none).
        max_per_workspace: hard cap per workspace before the total cap.

    Returns:
        The envelope dict described in the module docstring. ``dropped`` is the
        EXACT number of actionable items not present in ``items`` (never an
        estimate); ``truncated`` is ``dropped > 0``.
    """
    coerced = _coerce_items(items)
    # Stalest first, globally — so both caps keep the oldest items.
    coerced.sort(key=_sort_key)

    dropped = 0

    # ---- 2. Per-workspace cap (keep oldest, drop newest overflow) ----
    if max_per_workspace is not None and max_per_workspace >= 0:
        seen_per_ws: Counter[str] = Counter()
        per_ws_capped: list[WorkInboxItem] = []
        for item in coerced:
            if seen_per_ws[item.workspace_id] < max_per_workspace:
                per_ws_capped.append(item)
                seen_per_ws[item.workspace_id] += 1
            else:
                dropped += 1
    else:
        per_ws_capped = coerced

    # ---- 3. Total cap (list is still stalest-first) ----
    if max_total is not None and max_total >= 0 and len(per_ws_capped) > max_total:
        dropped += len(per_ws_capped) - max_total
        kept = per_ws_capped[:max_total]
    else:
        kept = per_ws_capped

    # ---- 4. Totals over the RETURNED population ----
    by_item_type: dict[str, int] = {t: 0 for t in _KNOWN_ITEM_TYPES}
    per_ws_count: dict[str, int] = defaultdict(int)
    per_ws_name: dict[str, str] = {}
    for item in kept:
        by_item_type[item.item_type] = by_item_type.get(item.item_type, 0) + 1
        per_ws_count[item.workspace_id] += 1
        # First (stalest) item's name wins; all rows for a workspace carry the
        # same display name anyway.
        per_ws_name.setdefault(item.workspace_id, item.workspace_display_name)

    by_workspace = [
        {
            "workspace_id": wid,
            "display_name": per_ws_name.get(wid, ""),
            "count": count,
        }
        for wid, count in per_ws_count.items()
    ]
    # Busiest client first, then name for stable ties.
    by_workspace.sort(key=lambda w: (-w["count"], w["display_name"]))

    return {
        "items": [asdict(item) for item in kept],
        "totals": {
            "total": len(kept),
            "by_item_type": by_item_type,
            "by_workspace": by_workspace,
        },
        "truncated": dropped > 0,
        "dropped": dropped,
    }


__all__ = (
    "WorkInboxItem",
    "build_work_inbox",
    "DEFAULT_MAX_TOTAL",
    "DEFAULT_MAX_PER_WORKSPACE",
    "ITEM_TYPE_APPROVAL",
    "ITEM_TYPE_TAX_REVIEW",
)
