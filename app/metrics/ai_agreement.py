"""AI-decision agreement rate — the "review-vs-redo" accuracy KPI (v1).

WHY THIS EXISTS
---------------
The "10x CPA" pitch is: *a human reviews AI-drafted work instead of redoing
it.* Every human terminal decision is already captured in ``approval_queue``
(``status`` -> approved | rejected), and the AI's prior call is captured in
``approval_recommendation`` (``recommended_decision`` -> approve |
flag_for_review | reject). Until now those two were never compared in
aggregate. This module computes the agreement rate between them.

The function here is intentionally PURE: it takes a list of already-fetched
``(recommended_decision, human_status, recommended_category)`` rows and returns
the metric. The SQL that fetches those rows (inside ``workspace_session`` so RLS
+ search_path are pinned) lives in ``app/api/metrics.py``. Keeping the math
DB-free makes it deterministically unit-testable without a live stack.

METRIC v1 DEFINITION
--------------------
For each approval that (a) has a recommendation row AND (b) reached a terminal
human ``status`` of ``approved`` or ``rejected``:

  * **Agreement** = the human terminal status matches the AI's
    ``recommended_decision`` under this mapping:
        approve  <-> approved
        reject   <-> rejected

  * ``recommended_decision == 'flag_for_review'`` is treated as **"the AI
    deferred to a human"** — i.e. the AI explicitly declined to make the call.
    Scoring a deferral as right/wrong would be a category error (there is no
    AI decision to agree or disagree with), and counting every flag as a
    "miss" would punish the system for correctly asking for help — exactly the
    behaviour we want. So flags are **EXCLUDED from the agreement denominator**
    and reported separately as ``flagged_for_review`` (a deferral count). They
    are NOT in ``total_decided`` and NOT in ``agreed``.

  * ``agreement_rate = agreed / total_decided`` over the non-flag terminal
    population, or ``None`` when ``total_decided == 0`` (no data yet — a rate
    of 0.0 would falsely read as "the AI is always wrong").

Denominator scoping summary (one approval = one recommendation, since
``approval_recommendation.approval_id`` is UNIQUE):
  * human status NOT in {approved, rejected}  -> excluded (non-terminal:
    pending / skipped / canceled — the human hasn't ruled yet).
  * recommended_decision == flag_for_review   -> excluded, counted as a
    deferral.
  * everything else                            -> in the denominator;
    in the numerator iff the approve/reject mapping matches.

OUTPUT SHAPE
------------
``compute_ai_agreement`` returns::

    {
      "total_decided": int,          # non-flag terminal approvals scored
      "agreed": int,                 # of those, where human == AI
      "agreement_rate": float|None,  # agreed/total_decided, None if denom 0
      "flagged_for_review": int,     # AI-deferred terminal approvals (excluded)
      "by_recommended_decision": {   # per AI decision, on the SAME population
          "approve": {"total": int, "agreed": int, "agreement_rate": float|None},
          "reject":  {"total": int, "agreed": int, "agreement_rate": float|None},
          # flag_for_review reported as deferrals, never scored:
          "flag_for_review": {"deferred": int},
      },
      "by_recommended_category": [   # optional slice, top-N by 'total' desc
          {"recommended_category": str|None, "total": int, "agreed": int,
           "agreement_rate": float|None},
          ...
      ],
    }

``compute_ai_agreement`` does NOT know about time windows or the secondary
"learned corrections" count — those are assembled by the API caller (which runs
the windowed SQL twice and reads ``category_override.match_count`` separately).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

# Human terminal statuses that count as a ruling for this metric. 'pending',
# 'skipped' and 'canceled' are NOT terminal here — the human has not decided.
TERMINAL_HUMAN_STATUSES: frozenset[str] = frozenset({"approved", "rejected"})

# AI recommended_decision -> the human status it agrees with.
_DECISION_TO_MATCHING_STATUS: dict[str, str] = {
    "approve": "approved",
    "reject": "rejected",
}

# The AI decision that means "I am deferring to a human" — never scored.
FLAG_DECISION = "flag_for_review"


@dataclass(frozen=True)
class AgreementRow:
    """One scored unit: an approval that has an AI recommendation.

    Attributes:
        recommended_decision: AI call — 'approve' | 'flag_for_review' | 'reject'.
        human_status: terminal human ``approval_queue.status`` — typically
            'approved' | 'rejected' (other values are filtered out as
            non-terminal before scoring).
        recommended_category: optional COA category the AI proposed (used only
            for the category slice; may be ``None``).
    """

    recommended_decision: str
    human_status: str
    recommended_category: str | None = None


def _rate(agreed: int, total: int) -> float | None:
    """agreed/total, or None when total == 0 (no population to score)."""
    if total <= 0:
        return None
    return agreed / total


def _is_agreement(recommended_decision: str, human_status: str) -> bool:
    """True iff a (non-flag) AI decision matches the human's terminal status."""
    return _DECISION_TO_MATCHING_STATUS.get(recommended_decision) == human_status


def _coerce_rows(
    rows: Iterable[AgreementRow | Mapping[str, object]],
) -> list[AgreementRow]:
    """Accept either AgreementRow instances or row-mappings (e.g. DB rows).

    DB rows come back as ``sqlalchemy`` mappings / dicts with keys
    ``recommended_decision``, ``human_status`` (aliased in SQL), and
    ``recommended_category``. Normalising here lets the API hand raw mappings
    straight through while tests pass typed ``AgreementRow`` objects.
    """
    out: list[AgreementRow] = []
    for r in rows:
        if isinstance(r, AgreementRow):
            out.append(r)
            continue
        rd = r.get("recommended_decision")
        hs = r.get("human_status")
        # Defensive: a row without both fields cannot be scored — skip it rather
        # than crash the whole metric on one malformed record.
        if rd is None or hs is None:
            continue
        out.append(
            AgreementRow(
                recommended_decision=str(rd),
                human_status=str(hs),
                recommended_category=(
                    str(r["recommended_category"])
                    if r.get("recommended_category") is not None
                    else None
                ),
            )
        )
    return out


def compute_ai_agreement(
    rows: Iterable[AgreementRow | Mapping[str, object]],
    *,
    category_top_n: int = 10,
) -> dict:
    """Compute the v1 AI-decision agreement rate from already-fetched rows.

    Each input row pairs an AI ``recommended_decision`` with the human's
    ``human_status`` for the SAME approval (the join is 1:1 because
    ``approval_recommendation.approval_id`` is UNIQUE). Rows whose human status
    is not terminal (not in :data:`TERMINAL_HUMAN_STATUSES`) are ignored — they
    have not been ruled on. Among terminal rows, ``flag_for_review`` is excluded
    from the rate and counted as a deferral (see module docstring for the full
    rationale).

    Args:
        rows: iterable of :class:`AgreementRow` or row-mappings carrying
            ``recommended_decision`` / ``human_status`` /
            ``recommended_category``.
        category_top_n: cap on the ``by_recommended_category`` slice
            (ordered by ``total`` desc, then category name for stable ties).

    Returns:
        The metric dict described in the module docstring. ``agreement_rate`` is
        ``None`` whenever its denominator is 0 (never a misleading 0.0).
    """
    scored = _coerce_rows(rows)

    total_decided = 0
    agreed = 0
    flagged_for_review = 0

    # Per-AI-decision tallies over the scored (non-flag) population.
    per_decision_total: dict[str, int] = defaultdict(int)
    per_decision_agreed: dict[str, int] = defaultdict(int)

    # Per-category tallies (None category bucketed under the literal None key).
    per_cat_total: dict[str | None, int] = defaultdict(int)
    per_cat_agreed: dict[str | None, int] = defaultdict(int)

    for row in scored:
        # Only score approvals the human has actually ruled on.
        if row.human_status not in TERMINAL_HUMAN_STATUSES:
            continue

        # The AI deferred — count as a deferral, never as agreement/disagreement.
        if row.recommended_decision == FLAG_DECISION:
            flagged_for_review += 1
            continue

        hit = _is_agreement(row.recommended_decision, row.human_status)
        total_decided += 1
        per_decision_total[row.recommended_decision] += 1
        per_cat_total[row.recommended_category] += 1
        if hit:
            agreed += 1
            per_decision_agreed[row.recommended_decision] += 1
            per_cat_agreed[row.recommended_category] += 1

    # Per-decision breakdown: always surface approve + reject (even at 0 so the
    # shape is stable), plus any other non-flag decision values we happened to
    # see, plus the flag deferral count.
    by_recommended_decision: dict[str, dict] = {}
    decisions_seen = (
        {"approve", "reject"}
        | set(per_decision_total)
    )
    for decision in sorted(decisions_seen):
        d_total = per_decision_total.get(decision, 0)
        d_agreed = per_decision_agreed.get(decision, 0)
        by_recommended_decision[decision] = {
            "total": d_total,
            "agreed": d_agreed,
            "agreement_rate": _rate(d_agreed, d_total),
        }
    by_recommended_decision[FLAG_DECISION] = {"deferred": flagged_for_review}

    # Category slice: top-N by total desc, then category name asc for stable
    # ordering (None sorts last so it never crowds out named categories).
    category_rows = [
        {
            "recommended_category": cat,
            "total": per_cat_total[cat],
            "agreed": per_cat_agreed.get(cat, 0),
            "agreement_rate": _rate(per_cat_agreed.get(cat, 0), per_cat_total[cat]),
        }
        for cat in per_cat_total
    ]
    category_rows.sort(
        key=lambda c: (
            -c["total"],
            c["recommended_category"] is None,  # named categories first
            c["recommended_category"] or "",
        )
    )
    by_recommended_category = category_rows[: max(category_top_n, 0)]

    return {
        "total_decided": total_decided,
        "agreed": agreed,
        "agreement_rate": _rate(agreed, total_decided),
        "flagged_for_review": flagged_for_review,
        "by_recommended_decision": by_recommended_decision,
        "by_recommended_category": by_recommended_category,
    }
