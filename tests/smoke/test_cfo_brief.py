"""Smoke tests for app/api/cfo_brief.py — the concierge-home CFO brief.

Import-level + pure-logic checks (no DB). The endpoint coroutine itself
reuses get_open_items / workspace_cash_position (exercised by their own
tests + integration); here we lock down:

  1. Module imports cleanly + router shape + the single /cfo-brief route.
  2. The deterministic ranker (_rank_priorities):
       - interleaves all five sources by score
       - a CRITICAL sentinel finding outranks a time-sensitive item
       - resolved decisions / received documents are EXCLUDED
       - output is deterministic (stable sort) and uses non-accountant
         phrasing (no "trial balance" / "journal" / "debit"-jargon in
         headlines)
       - every priority carries a deep-link action_href + label
  3. The runway model (_runway_from_pl) mirrors get_runway:
       - burning → positive months
       - cash-flow positive → None
       - robust to string / bad inputs
  4. Plain-language cash status (_plain_cash_status):
       - configured vs. not
       - low runway flips tone to 'pending'; negative net flips to 'debit'
       - whole-dollar, jargon-free phrasing
  5. The summary line + the Ask-the-CFO deep-link.
"""
from __future__ import annotations

import importlib
import sys
from decimal import Decimal


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load():
    mod_name = "app.api.cfo_brief"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    return importlib.import_module(mod_name)


# ---------------------------------------------------------------------------
# 1. Import + router shape
# ---------------------------------------------------------------------------


def test_module_imports_cleanly():
    assert _load() is not None


def test_router_is_api_router():
    from fastapi import APIRouter

    assert isinstance(_load().router, APIRouter)


def test_router_exposes_cfo_brief_route():
    mod = _load()
    paths = [r.path for r in mod.router.routes]  # type: ignore[attr-defined]
    assert any(p.endswith("/cfo-brief") for p in paths), paths


def test_response_model_has_expected_fields():
    mod = _load()
    fields = set(mod.CfoBriefResponse.model_fields.keys())
    for f in (
        "priorities",
        "action_required_total",
        "cash",
        "summary_line",
        "ask_cfo_href",
        "sources_ok",
    ):
        assert f in fields, (f, fields)


# ---------------------------------------------------------------------------
# Fixtures (plain dicts mirroring the open-items payload + finding rows)
# ---------------------------------------------------------------------------


def _open_items_payload() -> dict:
    return {
        "time_sensitive": [
            {"id": "ts1", "summary": "SCDOR dissolution clock is running"}
        ],
        "submissions_in_flight": [
            {
                "id": "sub1",
                "status": "awaiting_followup",
                "submission_title": "SC1120 TY2023",
                "jurisdiction": "SCDOR",
            },
            # A non-followup submission must NOT become a priority.
            {"id": "sub2", "status": "in_transit", "submission_title": "ignore me"},
        ],
        "decisions": [
            {
                "key": "mfj",
                "status": "pending",
                "question": "Confirm filing status — Married Filing Jointly?",
                "recommendation": "Married Filing Jointly (MFJ).",
            },
            # Resolved decision must be excluded.
            {"key": "sec195", "status": "resolved", "question": "already decided"},
        ],
        "documents": [
            {
                "key": "scdor_notice",
                "status": "pending",
                "name": "SCDOR Notice (2025-06-16)",
                "needed_for": "reasonable-cause cure letter",
                "priority": "time_sensitive",
            },
            {
                "key": "paypal",
                "status": "pending",
                "name": "PayPal Credit statements",
                "needed_for": "interest deduction",
                "priority": "medium",
            },
            # Received doc must be excluded.
            {"key": "va", "status": "received", "name": "VA letter"},
        ],
        "counts": {"action_required": 4},
    }


def _findings() -> list[dict]:
    return [
        {
            "id": "f_crit",
            "severity": "critical",
            "title": "2023 return is missing officer compensation",
            "summary": "No officer comp booked for TY2023 — likely understated.",
        },
        {
            "id": "f_low",
            "severity": "low",
            "title": "A few transactions are uncategorized",
            "summary": "12 small transactions still need a category.",
        },
    ]


# ---------------------------------------------------------------------------
# 2. Ranker
# ---------------------------------------------------------------------------


def test_ranker_interleaves_and_orders_by_score():
    mod = _load()
    pr = mod._rank_priorities(_open_items_payload(), _findings())
    # Scores must be non-increasing.
    scores = [p.score for p in pr]
    assert scores == sorted(scores, reverse=True), scores


def test_ranker_critical_finding_outranks_time_sensitive():
    mod = _load()
    pr = mod._rank_priorities(_open_items_payload(), _findings())
    kinds_in_order = [p.kind for p in pr]
    # The critical officer-comp finding (score 95) must precede the
    # time-sensitive queue row (score 90).
    first_finding = kinds_in_order.index("finding")
    first_ts = kinds_in_order.index("time_sensitive")
    assert first_finding < first_ts, kinds_in_order


def test_ranker_excludes_resolved_and_received():
    mod = _load()
    pr = mod._rank_priorities(_open_items_payload(), _findings())
    ids = {p.id for p in pr}
    assert "dec:sec195" not in ids  # resolved decision excluded
    assert "doc:va" not in ids  # received doc excluded
    assert "sub:sub2" not in ids  # non-followup submission excluded


def test_ranker_time_sensitive_doc_outranks_ordinary_doc():
    mod = _load()
    pr = mod._rank_priorities(_open_items_payload(), _findings())
    by_id = {p.id: p.score for p in pr}
    assert by_id["doc:scdor_notice"] > by_id["doc:paypal"]


def test_every_priority_has_a_deeplink_and_label():
    mod = _load()
    pr = mod._rank_priorities(_open_items_payload(), _findings())
    assert pr  # non-empty
    for p in pr:
        assert p.action_href.startswith("/"), p
        assert p.action_label.strip(), p


def test_headlines_are_non_accountant_plain_language():
    """No jargon leaks into the headline copy a non-accountant reads."""
    mod = _load()
    pr = mod._rank_priorities(_open_items_payload(), _findings())
    banned = ("trial balance", "journal entry", "debit", "credit memo", "ledger")
    for p in pr:
        low = p.headline.lower()
        for b in banned:
            assert b not in low, (b, p.headline)


def test_ranker_is_deterministic():
    mod = _load()
    a = mod._rank_priorities(_open_items_payload(), _findings())
    b = mod._rank_priorities(_open_items_payload(), _findings())
    assert [p.id for p in a] == [p.id for p in b]


def test_ranker_handles_empty_and_none_inputs():
    mod = _load()
    assert mod._rank_priorities(None, []) == []
    assert mod._rank_priorities({}, []) == []
    # Missing sub-keys must not raise.
    assert mod._rank_priorities({"counts": {}}, []) == []


# ---------------------------------------------------------------------------
# 3. Runway model
# ---------------------------------------------------------------------------


def test_runway_burning_returns_positive_months():
    mod = _load()
    # cash 180k, losing 30k/mo over a 3-month window (net income -90k).
    rw = mod._runway_from_pl("180000", "-90000", 3)
    assert rw is not None
    assert rw == Decimal("6.0")


def test_runway_cash_flow_positive_is_none():
    mod = _load()
    assert mod._runway_from_pl("1000", "9000", 3) is None  # making money


def test_runway_robust_to_bad_inputs():
    mod = _load()
    assert mod._runway_from_pl(None, "-90000", 3) is None
    assert mod._runway_from_pl("180000", None, 3) is None
    assert mod._runway_from_pl("180000", "-90000", "3") == Decimal("6.0")  # str ok
    assert mod._runway_from_pl("180000", "-90000", "oops") is None
    assert mod._runway_from_pl("180000", "-90000", 0) is None


def test_runway_zero_cash_when_burning_is_zero_months():
    mod = _load()
    assert mod._runway_from_pl("0", "-90000", 3) == Decimal("0")


# ---------------------------------------------------------------------------
# 4. Plain-language cash status
# ---------------------------------------------------------------------------


def test_cash_not_configured():
    mod = _load()
    c = mod._plain_cash_status(None, None)
    assert c.configured is False
    assert "connect" in (c.detail or "").lower()


def test_cash_configured_plain_words():
    mod = _load()
    c = mod._plain_cash_status(
        {
            "configured": True,
            "assets_total": "182304.50",
            "liabilities_total": "17293.65",
            "net_total": "165010.85",
        },
        Decimal("8.0"),
    )
    assert c.configured is True
    assert "in the bank" in c.headline
    assert "$182,304" in c.headline  # whole dollars, comma-grouped
    assert c.detail and "credit cards" in c.detail
    assert c.tone == "credit"  # positive net, healthy runway


def test_cash_low_runway_flips_tone_to_pending():
    mod = _load()
    c = mod._plain_cash_status(
        {"configured": True, "assets_total": "10000", "liabilities_total": "0", "net_total": "10000"},
        Decimal("2.0"),
    )
    assert c.tone == "pending"
    assert c.detail and "runway" in c.detail


def test_cash_negative_net_flips_tone_to_debit():
    mod = _load()
    c = mod._plain_cash_status(
        {"configured": True, "assets_total": "5000", "liabilities_total": "20000", "net_total": "-15000"},
        None,
    )
    assert c.tone == "debit"


def test_fmt_usd_whole_dollars():
    mod = _load()
    assert mod._fmt_usd("182304.50") == "$182,304"
    assert mod._fmt_usd("-17293.65") == "-$17,294"
    assert mod._fmt_usd(None) == "$—"
    assert mod._fmt_usd("not-a-number") == "$—"


# ---------------------------------------------------------------------------
# 5. Summary line + Ask-the-CFO deep-link
# ---------------------------------------------------------------------------


def test_summary_line_counts_and_cash():
    mod = _load()
    pr = mod._rank_priorities(_open_items_payload(), _findings())
    cash = mod._plain_cash_status(
        {"configured": True, "assets_total": "182304.50", "liabilities_total": "0", "net_total": "182304.50"},
        Decimal("8.0"),
    )
    line = mod._summary_line(pr, cash)
    assert "your OK" in line  # decision phrasing
    assert "$182,304" in line
    # Reads as a sentence (ends with a period).
    assert line.strip().endswith(".")


def test_summary_line_all_clear():
    mod = _load()
    cash = mod._plain_cash_status(None, None)
    line = mod._summary_line([], cash)
    assert "caught up" in line.lower()


def test_ask_cfo_deeplink_targets_cfo_persona():
    """The brief must hand off to the CFO persona for follow-up."""
    import inspect

    mod = _load()
    src = inspect.getsource(mod.get_cfo_brief)
    assert "/chat?persona=cfo-agent" in src
