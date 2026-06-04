"""Smoke test: the cross-client work-inbox merge / sort / cap aggregator.

Exercises the PURE aggregator ``app.metrics.work_inbox.build_work_inbox``
directly — no DB, no live stack — the same "runs without a stack" style as
``tests/smoke/test_ai_agreement_metric.py``. The endpoint
(``app/api/work_inbox.py``) only fetches rows inside ``workspace_session`` and
hands them to this function, so proving the merge/sort/cap here proves the
load-bearing logic.

Covered:
  * Stalest-first sort (age_hours DESC) with deterministic tiebreakers.
  * Mixed item_types (approval + tax_review) interleave purely by age.
  * Per-workspace cap keeps the OLDEST items and counts every drop.
  * Total cap (after per-workspace) keeps the oldest and counts every drop.
  * truncated/dropped accounting is EXACT (never silent) and dropped is the
    precise number of items not present.
  * totals.by_item_type / by_workspace describe the RETURNED population and sum
    to ``total`` (never total+dropped).
  * Empty input -> empty, not-truncated envelope with the stable shape.
  * Raw dict rows (what the API actually passes) coerce correctly; a malformed
    row is skipped rather than crashing the inbox.
"""
from __future__ import annotations

from app.metrics.work_inbox import (
    ITEM_TYPE_APPROVAL,
    ITEM_TYPE_TAX_REVIEW,
    WorkInboxItem,
    build_work_inbox,
)


def _item(
    *,
    ws: str = "ws-a",
    ws_name: str = "Client A",
    age: float,
    item_type: str = ITEM_TYPE_APPROVAL,
    entity: str | None = "ent-1",
    created_at: str | None = None,
    title: str = "x",
) -> WorkInboxItem:
    return WorkInboxItem(
        workspace_id=ws,
        workspace_display_name=ws_name,
        entity_id=entity,
        item_type=item_type,
        subject_type="bank_transaction_import"
        if item_type == ITEM_TYPE_APPROVAL
        else "flagged_position",
        title=title,
        created_at=created_at,
        age_hours=age,
        deep_link=f"/approvals?workspace={ws}",
    )


def test_sorted_stalest_first_across_workspaces_and_types():
    """Items come back oldest-first regardless of workspace or item_type."""
    items = [
        _item(ws="ws-a", age=2.0, item_type=ITEM_TYPE_APPROVAL),
        _item(ws="ws-b", age=99.0, item_type=ITEM_TYPE_TAX_REVIEW, ws_name="Client B"),
        _item(ws="ws-a", age=50.0, item_type=ITEM_TYPE_TAX_REVIEW),
        _item(ws="ws-b", age=10.0, item_type=ITEM_TYPE_APPROVAL, ws_name="Client B"),
    ]
    out = build_work_inbox(items)

    ages = [i["age_hours"] for i in out["items"]]
    assert ages == [99.0, 50.0, 10.0, 2.0]  # strictly descending == stalest first
    # Mixed types interleaved purely by age (tax_review at 99 and 50 sit above
    # approvals at 10 and 2).
    assert out["items"][0]["item_type"] == ITEM_TYPE_TAX_REVIEW
    assert out["items"][-1]["item_type"] == ITEM_TYPE_APPROVAL
    assert out["truncated"] is False
    assert out["dropped"] == 0
    assert out["totals"]["total"] == 4


def test_tiebreak_is_deterministic_by_workspace_then_created_at():
    """Equal age -> ordered by workspace name, then created_at (ISO == chrono)."""
    items = [
        _item(ws="ws-z", ws_name="Zeta", age=5.0, created_at="2026-01-02T00:00:00+00:00"),
        _item(ws="ws-a", ws_name="Alpha", age=5.0, created_at="2026-01-02T00:00:00+00:00"),
        _item(ws="ws-a", ws_name="Alpha", age=5.0, created_at="2026-01-01T00:00:00+00:00"),
    ]
    out = build_work_inbox(items)
    names = [i["workspace_display_name"] for i in out["items"]]
    created = [i["created_at"] for i in out["items"]]
    # Alpha before Zeta; within Alpha the older created_at first.
    assert names == ["Alpha", "Alpha", "Zeta"]
    assert created[0] == "2026-01-01T00:00:00+00:00"
    assert created[1] == "2026-01-02T00:00:00+00:00"


def test_per_workspace_cap_keeps_oldest_and_counts_drops():
    """A workspace over the per-workspace cap loses its NEWEST items; dropped is exact."""
    # 5 items in ws-a (ages 10..50), 1 item in ws-b. Cap per workspace = 3.
    items = [_item(ws="ws-a", age=float(a)) for a in (10, 20, 30, 40, 50)]
    items.append(_item(ws="ws-b", ws_name="Client B", age=15.0))
    out = build_work_inbox(items, max_per_workspace=3, max_total=100)

    # ws-a kept its 3 OLDEST (ages 50,40,30); the 2 newest (20,10) dropped.
    ws_a_ages = sorted(
        (i["age_hours"] for i in out["items"] if i["workspace_id"] == "ws-a"),
        reverse=True,
    )
    assert ws_a_ages == [50.0, 40.0, 30.0]
    assert out["dropped"] == 2
    assert out["truncated"] is True
    # ws-b untouched.
    assert sum(1 for i in out["items"] if i["workspace_id"] == "ws-b") == 1
    assert out["totals"]["total"] == 4  # 3 from ws-a + 1 from ws-b


def test_total_cap_after_per_workspace_keeps_oldest_and_counts_drops():
    """The total cap trims the globally-newest survivors; dropped accumulates."""
    # Two workspaces, 3 items each (ages 1..6), per-ws cap high, total cap = 4.
    items = [
        _item(ws="ws-a", age=6.0),
        _item(ws="ws-a", age=4.0),
        _item(ws="ws-a", age=2.0),
        _item(ws="ws-b", ws_name="Client B", age=5.0),
        _item(ws="ws-b", ws_name="Client B", age=3.0),
        _item(ws="ws-b", ws_name="Client B", age=1.0),
    ]
    out = build_work_inbox(items, max_per_workspace=100, max_total=4)

    ages = [i["age_hours"] for i in out["items"]]
    # Keep the 4 oldest globally: 6,5,4,3 — drop the 2 newest (2,1).
    assert ages == [6.0, 5.0, 4.0, 3.0]
    assert out["dropped"] == 2
    assert out["truncated"] is True
    assert out["totals"]["total"] == 4


def test_both_caps_compose_and_dropped_is_the_exact_total():
    """Per-workspace + total drops both count toward one exact ``dropped``."""
    # ws-a has 5 (ages 10..50); ws-b has 2 (ages 5,7). per-ws cap=3 -> drop 2
    # from ws-a. Survivors: ws-a 50,40,30 + ws-b 7,5 = 5 items. total cap=4 ->
    # drop the newest survivor (ws-b age 5). dropped total = 2 + 1 = 3.
    items = [_item(ws="ws-a", age=float(a)) for a in (10, 20, 30, 40, 50)]
    items += [
        _item(ws="ws-b", ws_name="Client B", age=5.0),
        _item(ws="ws-b", ws_name="Client B", age=7.0),
    ]
    out = build_work_inbox(items, max_per_workspace=3, max_total=4)

    assert out["totals"]["total"] == 4
    assert out["dropped"] == 3  # 2 (per-ws) + 1 (total)
    assert out["truncated"] is True
    ages = [i["age_hours"] for i in out["items"]]
    assert ages == [50.0, 40.0, 30.0, 7.0]  # oldest 4 survivors


def test_totals_describe_returned_population_only():
    """by_item_type + by_workspace sum to total (post-cap), not total+dropped."""
    items = [
        _item(ws="ws-a", age=9.0, item_type=ITEM_TYPE_APPROVAL),
        _item(ws="ws-a", age=8.0, item_type=ITEM_TYPE_TAX_REVIEW),
        _item(ws="ws-a", age=7.0, item_type=ITEM_TYPE_APPROVAL),
        # Newest globally -> dropped by the total cap (stalest-first keeps 9,8,7).
        _item(ws="ws-b", ws_name="Client B", age=6.0, item_type=ITEM_TYPE_TAX_REVIEW),
    ]
    out = build_work_inbox(items, max_total=3, max_per_workspace=100)

    assert out["totals"]["total"] == 3
    assert out["dropped"] == 1
    # Returned (oldest 3): ws-a approval@9 + tax_review@8 + approval@7.
    bt = out["totals"]["by_item_type"]
    assert bt[ITEM_TYPE_APPROVAL] == 2
    assert bt[ITEM_TYPE_TAX_REVIEW] == 1
    assert bt[ITEM_TYPE_APPROVAL] + bt[ITEM_TYPE_TAX_REVIEW] == out["totals"]["total"]

    # by_workspace: busiest first, counts sum to total. ws-b's only item was the
    # one dropped, so it must NOT appear in the post-cap totals.
    bw = out["totals"]["by_workspace"]
    assert bw[0]["workspace_id"] == "ws-a"
    assert bw[0]["count"] == 3
    assert bw[0]["display_name"] == "Client A"
    assert all(w["workspace_id"] != "ws-b" for w in bw)
    assert sum(w["count"] for w in bw) == out["totals"]["total"]


def test_empty_input_is_stable_and_not_truncated():
    """No items -> stable shape, both known item-type keys present at 0."""
    out = build_work_inbox([])
    assert out["items"] == []
    assert out["totals"]["total"] == 0
    assert out["totals"]["by_item_type"] == {
        ITEM_TYPE_APPROVAL: 0,
        ITEM_TYPE_TAX_REVIEW: 0,
    }
    assert out["totals"]["by_workspace"] == []
    assert out["truncated"] is False
    assert out["dropped"] == 0


def test_accepts_raw_dict_rows_and_skips_malformed():
    """The API hands plain dicts through; a row missing keys is skipped, not fatal."""
    rows = [
        {
            "workspace_id": "ws-a",
            "workspace_display_name": "Client A",
            "entity_id": "ent-1",
            "item_type": ITEM_TYPE_APPROVAL,
            "subject_type": "bank_transaction_import",
            "title": "Bank import: Stripe",
            "amount": "123.45",
            "currency_code": "USD",
            "recommended_decision": "approve",
            "recommended_category": "SaaS · AI",
            "confidence": "high",
            "severity": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "age_hours": 12.0,
            "deep_link": "/approvals?workspace=ws-a",
        },
        # Malformed: no workspace_id -> coercer skips it (degrade, don't crash).
        {"item_type": ITEM_TYPE_TAX_REVIEW, "title": "broken"},
    ]
    out = build_work_inbox(rows)
    assert out["totals"]["total"] == 1
    only = out["items"][0]
    assert only["amount"] == "123.45"
    assert only["recommended_decision"] == "approve"
    assert only["confidence"] == "high"
    assert out["dropped"] == 0
