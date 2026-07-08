"""Smoke test: the AI-decision agreement rate (review-vs-redo KPI, v1).

Exercises the PURE aggregator ``app.metrics.ai_agreement.compute_ai_agreement``
directly — no DB, no live stack — the same "runs without a stack" style as
``tests/smoke/test_bulk_approval_cap.py``. The endpoint
(``app/api/metrics.py``) only fetches rows inside ``workspace_session`` and hands
them to this function, so proving the math here proves the metric.

Covered:
  * 2 agree of 3 decided NON-flag  -> rate 0.667 (the headline example).
  * ``flag_for_review`` is EXCLUDED from the denominator and counted separately
    as a deferral.
  * Non-terminal human statuses (pending / skipped) are ignored.
  * Empty input -> ``agreement_rate is None`` (never a misleading 0.0).
  * Per-decision breakdown (approve / reject buckets + flag deferral count).
  * The optional category slice and its top-N cap + ordering.
"""
from __future__ import annotations

import math

from app.metrics.ai_agreement import AgreementRow, compute_ai_agreement


def _row(decision: str, status: str, category: str | None = None) -> AgreementRow:
    return AgreementRow(
        recommended_decision=decision,
        human_status=status,
        recommended_category=category,
    )


def test_two_of_three_agree_is_point_667():
    """2 agreements out of 3 decided non-flag rows -> 0.667 (the pitch number)."""
    rows = [
        _row("approve", "approved"),   # agree
        _row("reject", "rejected"),    # agree
        _row("approve", "rejected"),   # AI said approve, human rejected -> disagree
    ]
    result = compute_ai_agreement(rows)

    assert result["total_decided"] == 3
    assert result["agreed"] == 2
    assert result["agreement_rate"] is not None
    assert math.isclose(result["agreement_rate"], 2 / 3, rel_tol=1e-9)
    assert round(result["agreement_rate"], 3) == 0.667
    # No flags in this set.
    assert result["flagged_for_review"] == 0


def test_flag_for_review_excluded_from_denominator_and_counted_separately():
    """flag_for_review rows must NOT count toward the rate; they are deferrals.

    Two clean agreements + one flag -> rate is 1.0 over the 2 scored rows, and
    the flag shows up only in ``flagged_for_review`` / the flag deferral bucket.
    """
    rows = [
        _row("approve", "approved"),        # agree
        _row("reject", "rejected"),         # agree
        _row("flag_for_review", "approved"),  # AI deferred -> excluded
        _row("flag_for_review", "rejected"),  # AI deferred -> excluded
    ]
    result = compute_ai_agreement(rows)

    # Denominator excludes both flags.
    assert result["total_decided"] == 2
    assert result["agreed"] == 2
    assert result["agreement_rate"] == 1.0
    # Both flags counted as deferrals, not as misses.
    assert result["flagged_for_review"] == 2
    assert result["by_recommended_decision"]["flag_for_review"] == {"deferred": 2}


def test_non_terminal_human_status_is_ignored():
    """pending / skipped / canceled are not rulings -> not scored, not deferred."""
    rows = [
        _row("approve", "approved"),   # scored, agree
        _row("approve", "pending"),    # human hasn't ruled -> ignored
        _row("reject", "skipped"),     # ignored
        _row("reject", "canceled"),    # ignored
    ]
    result = compute_ai_agreement(rows)

    assert result["total_decided"] == 1
    assert result["agreed"] == 1
    assert result["agreement_rate"] == 1.0
    # Non-terminal rows are NOT flags either.
    assert result["flagged_for_review"] == 0


def test_empty_population_yields_none_rate():
    """No scorable rows -> rate is None (a 0.0 would falsely read as 'always wrong')."""
    # Truly empty.
    empty = compute_ai_agreement([])
    assert empty["total_decided"] == 0
    assert empty["agreed"] == 0
    assert empty["agreement_rate"] is None
    assert empty["flagged_for_review"] == 0

    # Only flags + only non-terminal -> still an empty denominator -> None.
    only_excluded = compute_ai_agreement(
        [
            _row("flag_for_review", "approved"),
            _row("approve", "pending"),
        ]
    )
    assert only_excluded["total_decided"] == 0
    assert only_excluded["agreement_rate"] is None
    assert only_excluded["flagged_for_review"] == 1


def test_per_decision_breakdown_math():
    """approve / reject buckets each get their own total/agreed/rate."""
    rows = [
        _row("approve", "approved"),   # approve bucket: agree
        _row("approve", "approved"),   # approve bucket: agree
        _row("approve", "rejected"),   # approve bucket: disagree
        _row("reject", "rejected"),    # reject bucket: agree
        _row("reject", "approved"),    # reject bucket: disagree
    ]
    result = compute_ai_agreement(rows)

    assert result["total_decided"] == 5
    assert result["agreed"] == 3  # 2 approve-agrees + 1 reject-agree

    by_dec = result["by_recommended_decision"]
    assert by_dec["approve"]["total"] == 3
    assert by_dec["approve"]["agreed"] == 2
    assert math.isclose(by_dec["approve"]["agreement_rate"], 2 / 3, rel_tol=1e-9)

    assert by_dec["reject"]["total"] == 2
    assert by_dec["reject"]["agreed"] == 1
    assert by_dec["reject"]["agreement_rate"] == 0.5

    # flag bucket present even with zero flags, as a deferral count.
    assert by_dec["flag_for_review"] == {"deferred": 0}


def test_category_slice_and_top_n_cap():
    """The optional category slice aggregates per category and respects top-N."""
    rows = [
        # SaaS: 2 decided, 1 agree -> 0.5
        _row("approve", "approved", "SaaS · Productivity"),
        _row("approve", "rejected", "SaaS · Productivity"),
        # Infra: 1 decided, 1 agree -> 1.0
        _row("reject", "rejected", "Infrastructure · VPS"),
        # Banking: 1 decided, 0 agree -> 0.0
        _row("approve", "rejected", "Banking · Fees"),
        # A flag with a category must not appear in the slice.
        _row("flag_for_review", "approved", "Meals · Travel"),
    ]
    result = compute_ai_agreement(rows, category_top_n=2)

    slice_ = result["by_recommended_category"]
    # Top-N cap honoured.
    assert len(slice_) == 2
    # Ordered by total desc -> SaaS (2) first.
    assert slice_[0]["recommended_category"] == "SaaS · Productivity"
    assert slice_[0]["total"] == 2
    assert slice_[0]["agreed"] == 1
    assert slice_[0]["agreement_rate"] == 0.5
    # The flag's category never entered the slice.
    cats = {c["recommended_category"] for c in slice_}
    assert "Meals · Travel" not in cats


def test_accepts_raw_row_mappings_like_db_rows():
    """The API hands DB mappings straight through — dict rows must work too."""
    rows = [
        {
            "recommended_decision": "approve",
            "human_status": "approved",
            "recommended_category": "SaaS · Productivity",
        },
        {
            "recommended_decision": "reject",
            "human_status": "approved",  # disagree
            "recommended_category": None,
        },
    ]
    result = compute_ai_agreement(rows)
    assert result["total_decided"] == 2
    assert result["agreed"] == 1
    assert result["agreement_rate"] == 0.5
