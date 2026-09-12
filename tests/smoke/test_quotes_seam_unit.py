"""Unit tests for the Quotes → money seam (app.ar.quotes_seam) — no live Postgres.

Three tiers:

  1. Pure planning maths: per-line payment splits (A5), exact-sum rounding,
     deposit / remaining-part drafts, recurring schedules (month|quarter|year)
     anchored with month-end clamping (review M9), totals cross-check (A4),
     currency exponent (A17), line kinds (A16), percent formatting (review H1),
     and the partner split (percent_net | fee | none, hosting pass-through
     independent of kind — review H2, direct costs, missing hosting flag — L16).
  2. Stubbed-session apply: the whole acceptance creates the right DRAFTS with
     NO journal entry, is idempotent by quote_ref + version (a replay returns
     the same ids and creates nothing), refuses a closed period, isolates a
     partner failure (federated.partner = failed, customer drafts kept), checks
     the vendor entity's closed period (review H4), validates the supplier is a
     vendor (review M7), resolves net terms payload -> workspace -> 30 (M10),
     and voids a prior version's unissued paper (M11 as directed).
  3. Stubbed-session schedule generation: the runner creates the next period's
     draft from the anchor, advances, retires the schedule at the end of its
     term, catches up several periods, skips closed periods, and bills the
     partner for every generated period of a pass-through line (review H3).

Fixtures are synthetic; the customer is "the apparel client".
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.ar import quotes_seam as qs
from app.ar.quotes_seam import (
    QuoteAcceptedPayload,
    QuoteSeamError,
    SplitRule,
    advance_period,
    apply_quote_acceptance,
    compute_partner_share,
    fmt_pct,
    full_quote_ref,
    generate_due_schedule_invoices,
    parse_quote_ref,
    period_date,
    periods_for_term,
    plan_quote_billing,
    split_parts,
    validate_currency,
)

WID = "11111111-1111-4111-8111-111111111111"
EID = "22222222-2222-4222-8222-222222222222"
VENDOR_EID = "33333333-3333-4333-8333-333333333333"


# ===========================================================================
# Fixture payload — the Order Pad shape: one-time + three milestones + hosting
# + AI allowance (recurring) + an hourly rate card + included/excluded lines.
# ===========================================================================


def _lines() -> list[dict[str, Any]]:
    return [
        {"kind": "one_time", "label": "Order Pad build — Starter package", "amount_cents": 140_000, "position": 1,
         "option_group": "package", "selected": True},
        {"kind": "milestone", "label": "Milestone 1 — Catalogue + ordering", "amount_cents": 200_000, "position": 2,
         "milestone": {"payment_split": [50, 50]}},
        {"kind": "milestone", "label": "Milestone 2 — Approvals + invoicing", "amount_cents": 150_000, "position": 3},
        {"kind": "milestone", "label": "Milestone 3 — Go-live + training", "amount_cents": 100_000, "position": 4,
         "payment_split": [30, 30, 40]},
        {"kind": "recurring", "label": "Managed hosting", "amount_cents": 35_000, "position": 5,
         "recurring": {"interval": "month", "term_months": 12}},
        {"kind": "recurring", "label": "AI allowance", "amount_cents": 5_000, "position": 6,
         "recurring": {"interval": "month", "term_months": 12}},
        {"kind": "recurring", "label": "Annual support retainer", "amount_cents": 120_000, "position": 7,
         "recurring": {"interval": "year", "term_months": 24}},
        {"kind": "hourly", "label": "Change work", "amount_cents": 15_000, "position": 8},
        {"kind": "included", "label": "Onboarding call", "amount_cents": 0, "position": 9},
        {"kind": "excluded", "label": "Custom hardware", "amount_cents": 0, "position": 10},
    ]


def _totals() -> dict[str, Any]:
    # deposit = 70,000 (one-time 50%) + 100,000 (M1 50%) + 75,000 (M2 50%) + 30,000 (M3 30%)
    return {
        "one_time_total_cents": 140_000,
        "milestone_total_cents": 450_000,
        "deposit_cents": 275_000,
        "recurring": {"month": 40_000, "year": 120_000},
    }


def _payload(**overrides: Any) -> QuoteAcceptedPayload:
    data: dict[str, Any] = {
        "quote_ref": "Q-2026-0001",
        "version": 2,
        "customer": {
            "display_name": "Ops Lead",
            "email": "ops@apparel-client.example",
            "org": "the apparel client",
            "contact_id": "co-2467436b",
        },
        "currency": "USD",
        "lines": _lines(),
        "totals": _totals(),
        "delivery_partner": {"name": "Synthetic Delivery Partner LLC", "contact_id": "co-partner-1"},
        "split_rule": {"kind": "percent_net", "percent": 50, "hosting_to_partner": True,
                       "direct_costs_total_cents": 90_000},
        "accepted_at": "2026-09-12",
    }
    data.update(overrides)
    return QuoteAcceptedPayload.model_validate(data)


# ===========================================================================
# 1. Pure maths
# ===========================================================================


def test_full_quote_ref_and_parse():
    assert full_quote_ref("Q-2026-0001", 2) == "Q-2026-0001 v2"
    assert full_quote_ref("Q-2026-0001 v2", 2) == "Q-2026-0001 v2"
    assert full_quote_ref(" Q-2026-0001 ", 1) == "Q-2026-0001 v1"
    assert parse_quote_ref("Q-2026-0001 v12") == ("Q-2026-0001", 12)
    assert parse_quote_ref("Q-2026-0001") == ("Q-2026-0001", None)


@pytest.mark.parametrize(
    "value,expected",
    [(50, "50"), (10, "10"), (Decimal("33.5"), "33.5"), (100, "100"), (Decimal("33.333"), "33.33"),
     (Decimal("0.5"), "0.5"), (Decimal("50.00"), "50"), (0, "0")],
)
def test_fmt_pct_never_scientific(value, expected):
    out = fmt_pct(value)
    assert out == expected
    assert "E" not in out and "e" not in out


@pytest.mark.parametrize(
    "amount,split,expected",
    [
        (100_000, [50, 50], [50_000, 50_000]),
        (100_001, [50, 50], [50_001, 50_000]),       # A5 part 0 = round-half-up; parts sum exactly
        (1_003, [50, 25, 25], [502, 251, 250]),      # part 0 half-up (501.5 -> 502); rest apportioned
        (100, [33, 33, 34], [33, 33, 34]),
        (101, [33, 33, 34], [33, 33, 35]),           # largest remainder: 33.49 vs 34.51 -> last part
        (100_001, [30, 30, 40], [30_000, 30_000, 40_001]),  # the review's worked example
        (33_333, [50, 50], [16_667, 16_666]),
        (1, [50, 50], [1, 0]),
        (7, [100], [7]),
        (10_000, [30, 30, 40], [3_000, 3_000, 4_000]),
    ],
)
def test_split_parts_exact_sum_and_a5_deposit_rounding(amount, split, expected):
    parts = split_parts(amount, split)
    assert parts == expected
    assert sum(parts) == amount
    assert all(p >= 0 for p in parts)


@pytest.mark.parametrize("split", [[], [60, 50], [50, -50, 100], [101], [10] * 13])
def test_split_parts_rejects_bad_splits(split):
    with pytest.raises(QuoteSeamError) as exc:
        split_parts(100, split)
    assert exc.value.code == "INVALID_PAYMENT_SPLIT"


def test_period_date_anchors_to_original_day_with_month_end_clamping():
    anchor = date(2026, 1, 31)
    assert [period_date(anchor, "month", n) for n in (1, 2, 3, 4, 5)] == [
        date(2026, 1, 31), date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30), date(2026, 5, 31),
    ]
    assert period_date(date(2026, 11, 30), "quarter", 2) == date(2027, 2, 28)
    assert period_date(date(2028, 2, 29), "year", 2) == date(2029, 2, 28)
    assert period_date(date(2028, 2, 29), "year", 5) == date(2032, 2, 29)
    assert advance_period(date(2026, 9, 12), "month") == date(2026, 10, 12)
    with pytest.raises(QuoteSeamError):
        period_date(date(2026, 1, 1), "week", 1)
    with pytest.raises(QuoteSeamError):
        period_date(date(2026, 1, 1), "month", 0)


def test_periods_for_term():
    assert periods_for_term(12, "month") == 12
    assert periods_for_term(12, "quarter") == 4
    assert periods_for_term(24, "year") == 2
    assert periods_for_term(13, "quarter") == 5   # ceil
    assert periods_for_term(None, "month") is None


def test_currency_two_decimal_only():
    assert validate_currency("usd") == "USD"
    assert validate_currency("EUR") == "EUR"
    for bad in ("JPY", "KWD", "CLF"):
        with pytest.raises(QuoteSeamError) as exc:
            validate_currency(bad)
        assert exc.value.code == "CURRENCY_UNSUPPORTED"
    with pytest.raises(QuoteSeamError) as exc:
        validate_currency("US")
    assert exc.value.code == "CURRENCY_INVALID"


def test_plan_deposit_milestones_schedules_and_skips():
    plan = plan_quote_billing(_payload())
    assert plan.quote_ref == "Q-2026-0001 v2"
    assert (plan.quote_number, plan.version) == ("Q-2026-0001", 2)
    assert plan.deposit_cents == 275_000
    assert [(p.position, p.amount_cents) for p in plan.deposit_parts] == [
        (1, 70_000), (2, 100_000), (3, 75_000), (4, 30_000),
    ]
    drafts = [(d.position, d.amount_cents, d.memo) for d in plan.milestone_drafts]
    assert drafts == [
        (1, 70_000, "Due on acceptance of Order Pad build — Starter package"),
        (2, 100_000, "Due on acceptance of Milestone 1 — Catalogue + ordering"),
        (3, 75_000, "Due on acceptance of Milestone 2 — Approvals + invoicing"),
        (4, 30_000, "Due on acceptance of Milestone 3 — Go-live + training (part 2 of 3)"),
        (4, 40_000, "Due on acceptance of Milestone 3 — Go-live + training (part 3 of 3)"),
    ]
    assert plan.deposit_cents + sum(d.amount_cents for d in plan.milestone_drafts) == 590_000
    sched = [(s.position, s.cadence, s.amount_cents, s.periods_total, s.is_hosting, s.partner_amount_cents)
             for s in plan.schedules]
    assert sched == [
        (5, "month", 35_000, 12, True, 35_000),   # hosting passes through, every period
        (6, "month", 5_000, 12, False, 0),
        (7, "year", 120_000, 2, False, 0),
    ]
    assert sorted(s["position"] for s in plan.skipped_lines) == [8, 9, 10]
    assert plan.computed_totals == {
        "one_time_total_cents": 140_000,
        "milestone_total_cents": 450_000,
        "deposit_cents": 275_000,
        "recurring": {"month": 40_000, "year": 120_000},
    }
    assert plan.partner is not None
    assert plan.partner.total_cents == 285_000
    assert [pl.amount_cents for pl in plan.partner.lines] == [250_000, 35_000]
    # H1: the human-readable basis says 50%, never 5E+1%.
    assert "50% of net fixed scope" in plan.partner.lines[0].description
    assert "E+" not in plan.partner.basis


def test_plan_totals_cross_check_refuses_mismatch():
    totals = _totals()
    totals["deposit_cents"] = 275_001
    with pytest.raises(QuoteSeamError) as exc:
        plan_quote_billing(_payload(totals=totals))
    assert exc.value.code == "TOTALS_MISMATCH"
    assert "deposit_cents" in exc.value.reason
    totals = _totals()
    totals["recurring"]["quarter"] = 1
    with pytest.raises(QuoteSeamError, match="recurring.quarter"):
        plan_quote_billing(_payload(totals=totals))


def test_plan_without_totals_block_still_plans():
    assert plan_quote_billing(_payload(totals=None)).deposit_cents == 275_000


def test_plan_unselected_option_and_option_kind():
    lines = [
        {"kind": "option", "label": "Growth package", "amount_cents": 290_000, "position": 1, "selected": True},
        {"kind": "option", "label": "Starter package", "amount_cents": 140_000, "position": 2, "selected": False},
    ]
    plan = plan_quote_billing(_payload(lines=lines, totals=None, delivery_partner=None, split_rule=None))
    assert plan.deposit_cents == 145_000
    assert plan.computed_totals["one_time_total_cents"] == 290_000
    assert plan.skipped_lines[0]["reason"] == "unselected option line"
    assert plan.partner is None and plan.partner_skipped_reason == "no delivery partner on the quote"


def test_plan_nothing_to_invoice_and_duplicate_positions():
    with pytest.raises(QuoteSeamError) as exc:
        plan_quote_billing(_payload(lines=[{"kind": "hourly", "label": "x", "amount_cents": 100, "position": 1}],
                                    totals=None))
    assert exc.value.code == "NOTHING_TO_INVOICE"
    dup = _lines()[:2]
    dup[1]["position"] = 1
    with pytest.raises(QuoteSeamError) as exc:
        plan_quote_billing(_payload(lines=dup, totals=None))
    assert exc.value.code == "DUPLICATE_LINE_POSITION"


def test_plan_hosting_to_partner_without_a_hosting_line_is_refused():
    lines = _lines()
    lines[4]["label"] = "Managed platform"   # no longer says "hosting", no is_hosting flag
    with pytest.raises(QuoteSeamError) as exc:
        plan_quote_billing(_payload(lines=lines))
    assert exc.value.code == "HOSTING_LINE_MISSING"
    # ... but an explicit is_hosting flag on the renamed line is honoured.
    lines[4]["is_hosting"] = True
    plan = plan_quote_billing(_payload(lines=lines))
    assert plan.schedules[0].is_hosting is True
    # ... and without any recurring lines the switch is simply moot.
    no_recurring = [ln for ln in _lines() if ln["kind"] != "recurring"]
    plan = plan_quote_billing(_payload(lines=no_recurring, totals=None))
    assert plan.partner is not None and plan.partner.total_cents == 250_000


def test_partner_share_percent_net_fee_none_and_hosting():
    hosting = [("Managed hosting", 35_000)]
    p = compute_partner_share(
        split_rule=SplitRule(kind="percent_net", percent=Decimal("50"), hosting_to_partner=True,
                             direct_costs_total_cents=90_000),
        fixed_total_cents=590_000, hosting_lines=hosting, currency="USD",
    )
    assert p is not None and p.total_cents == 285_000
    p = compute_partner_share(
        split_rule=SplitRule(kind="percent_net", percent=Decimal("33.5"), direct_costs_total_cents=0),
        fixed_total_cents=100_001, hosting_lines=hosting, currency="USD",
    )
    assert p is not None and p.total_cents == 33_500  # 33500.335 -> 33500
    assert "33.5% of net fixed scope" in p.lines[0].description
    # direct costs exceed the fixed scope -> net 0 -> hosting only.
    p = compute_partner_share(
        split_rule=SplitRule(kind="percent_net", percent=Decimal("50"), hosting_to_partner=True,
                             direct_costs_total_cents=1_000_000),
        fixed_total_cents=590_000, hosting_lines=hosting, currency="USD",
    )
    assert p is not None and [pl.amount_cents for pl in p.lines] == [35_000]
    p = compute_partner_share(
        split_rule=SplitRule(kind="percent_net", percent=Decimal("50"), direct_costs_total_cents=1_000_000),
        fixed_total_cents=590_000, hosting_lines=hosting, currency="USD",
    )
    assert p is None
    # fee kind.
    p = compute_partner_share(
        split_rule=SplitRule(kind="fee", fee_cents=12_345), fixed_total_cents=590_000,
        hosting_lines=[], currency="USD",
    )
    assert p is not None and p.total_cents == 12_345
    with pytest.raises(QuoteSeamError, match="fee_cents"):
        compute_partner_share(split_rule=SplitRule(kind="fee"), fixed_total_cents=1, hosting_lines=[], currency="USD")
    with pytest.raises(QuoteSeamError, match="percent"):
        compute_partner_share(split_rule=SplitRule(kind="percent_net"), fixed_total_cents=1, hosting_lines=[], currency="USD")
    # H2: kind=none is NOT a return-before-hosting — hosting still passes through.
    p = compute_partner_share(
        split_rule=SplitRule(kind="none", hosting_to_partner=True), fixed_total_cents=590_000,
        hosting_lines=hosting, currency="USD",
    )
    assert p is not None and p.total_cents == 35_000 and p.kind == "none"
    assert "hosting to partner in full" in p.basis
    # kind=none and hosting stays with the vendor -> nothing to bill.
    assert compute_partner_share(split_rule=SplitRule(kind="none"), fixed_total_cents=1, hosting_lines=hosting,
                                 currency="USD") is None


def test_plan_kind_none_with_hosting_to_partner_bills_the_partner():
    plan = plan_quote_billing(_payload(split_rule={"kind": "none", "hosting_to_partner": True}))
    assert plan.partner is not None and plan.partner.total_cents == 35_000
    plan = plan_quote_billing(_payload(split_rule={"kind": "none"}))
    assert plan.partner is None
    assert plan.partner_skipped_reason == "split_rule.kind=none and hosting stays with the vendor"


def test_split_rule_accepts_v1_direct_costs_alias():
    rule = SplitRule.model_validate({"kind": "percent_net", "percent": 50, "direct_costs_cents": 5})
    assert rule.direct_costs_total_cents == 5


# ===========================================================================
# 2. Stubbed session
# ===========================================================================


class _Result:
    def __init__(self, rows: list[Any]):
        self._rows = rows

    def mappings(self):
        return self

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows

    def scalar_one(self):
        return self._rows[0] if self._rows else 0


class _Row(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError as e:  # pragma: no cover
            raise AttributeError(k) from e


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_COA = {
    "1100": ("acc-1100", "1100·Accounts Receivable"),
    "2300": ("acc-2300", "2300·Sales Tax Payable"),
    "4030": ("acc-4030", "4030·Revenue · Services"),
    "1010": ("acc-1010", "1010·Cash · Primary Checking"),
}


class _SeamStubSession:
    """In-memory fake AsyncSession for the seam + the AR primitives it reuses.

    Records every INSERT/UPDATE so tests can assert exactly what was created.
    ``closed_through`` maps entity_id -> date (simulates closed_period rows);
    ``fail_bill_insert`` simulates a partner-step failure; ``policy_net_terms``
    is the workspace default from engagement_policy.
    """

    def __init__(self, *, closed_through: dict[str, date] | None = None, fail_bill_insert: bool = False,
                 entity_ids: set[str] | None = None, policy_net_terms: int | None = None):
        self.entities = entity_ids or {EID, VENDOR_EID}
        self.closed_through = dict(closed_through or {})
        self.fail_bill_insert = fail_bill_insert
        self.policy_net_terms = policy_net_terms
        self.counterparties: dict[str, dict] = {}
        self.invoices: dict[str, dict] = {}
        self.invoice_lines: list[dict] = []
        self.schedules: dict[tuple, dict] = {}
        self.bills: dict[str, dict] = {}
        self.journal_entries: list[dict] = []
        self.updates: list[str] = []
        self._n_invoices = 0
        self._n_bills = 0

    def begin_nested(self):
        return _Savepoint()

    def add_counterparty(self, cid: str, name: str, kind: str, refs: dict | None = None) -> None:
        self.counterparties[cid] = {"id": cid, "name": name, "kind": kind, "refs": dict(refs or {})}

    @staticmethod
    def _ver(quote_ref: str) -> int:
        return parse_quote_ref(quote_ref)[1] or 0

    def _like(self, value: str, pattern: str) -> bool:
        # our patterns are "<escaped prefix> v%" — good enough for the stub
        pfx = pattern[:-1].replace("\\_", "_").replace("\\%", "%").replace("\\\\", "\\")
        return value.startswith(pfx)

    async def execute(self, query, params=None):  # noqa: C901 - dispatch table
        sql = " ".join(str(query).split())
        p = dict(params or {})

        # ---- entity / closed_period / engagement_policy ----
        if "FROM entity WHERE id = :eid" in sql:
            return _Result([(1,)] if p["eid"] in self.entities else [])
        if "FROM closed_period" in sql:
            ct = self.closed_through.get(p["eid"])
            if ct is not None and p["effective_date"] <= ct:
                return _Result([(ct,)])
            return _Result([])
        if "FROM engagement_policy" in sql:
            if self.policy_net_terms is None:
                return _Result([])
            return _Result([_Row(invoice_net_terms=self.policy_net_terms)])

        # ---- chart of accounts ----
        if "FROM chart_of_account" in sql and "account_number = :num" in sql:
            hit = _COA.get(p.get("num"))
            if not hit:
                return _Result([])
            if "id::text AS id" in sql:
                return _Result([_Row(id=hit[0], canonical_form=hit[1])])
            return _Result([_Row(canonical_form=hit[1])])
        if "INSERT INTO chart_of_account" in sql:
            return _Result([])

        # ---- counterparty ----
        if "FROM counterparty" in sql and "WHERE id = :id" in sql:
            cp = self.counterparties.get(p["id"])
            return _Result([_Row(id=cp["id"], display_name=cp["name"], kind=cp["kind"])] if cp else [])
        if "FROM counterparty" in sql and "external_refs->>'contact_id' = :cid" in sql:
            kind = "vendor" if "kind = 'vendor'" in sql else "customer"
            for cp in self.counterparties.values():
                if cp["kind"] == kind and cp["refs"].get("contact_id") == p["cid"]:
                    return _Result([_Row(id=cp["id"], display_name=cp["name"])])
            return _Result([])
        if "FROM counterparty" in sql and "lower(display_name) = lower(:name)" in sql:
            kind = "vendor" if "kind = 'vendor'" in sql else "customer"
            for cp in self.counterparties.values():
                if cp["kind"] == kind and cp["name"].lower() == p["name"].lower():
                    return _Result([_Row(id=cp["id"], display_name=cp["name"])])
            return _Result([])
        if "INSERT INTO counterparty" in sql:
            kind = "vendor" if "'vendor'" in sql else "customer"
            self.add_counterparty(p["id"], p["name"], kind, json.loads(p["refs"]))
            return _Result([])
        if "UPDATE counterparty" in sql:
            self.updates.append("counterparty")
            cp = self.counterparties.get(p["id"])
            if cp is not None:
                cp["refs"].update(json.loads(p["refs"]))
            return _Result([])

        # ---- invoice ----
        if "FROM invoice WHERE workspace_id = :wid AND external_source = :src AND external_ref = :ref" in sql:
            rows = [r for r in self.invoices.values()
                    if r["external_source"] == p["src"] and r["external_ref"] == p["ref"]]
            return _Result([_Row(**r) for r in rows])
        if "FROM invoice WHERE workspace_id = :wid AND external_source = :src AND quote_ref LIKE :pfx" in sql:
            rows = [r for r in self.invoices.values()
                    if r["external_source"] == p["src"] and r["quote_ref"] and self._like(r["quote_ref"], p["pfx"])]
            return _Result([_Row(id=r["id"], quote_ref=r["quote_ref"], status=r["status"],
                                 journal_entry_id=r["journal_entry_id"]) for r in rows])
        if "count(*) AS n FROM invoice" in sql:
            return _Result([_Row(n=self._n_invoices)])
        if "INSERT INTO invoice (" in sql:
            self._n_invoices += 1
            self.invoices[p["id"]] = {
                "id": p["id"], "workspace_id": p["wid"], "entity_id": p["eid"], "counterparty_id": p["cpid"],
                "invoice_number": p["num"], "status": p["status"], "currency": p["cur"],
                "issue_date": p["issue"], "due_date": p["due"], "net_terms": p["terms"],
                "subtotal": p["subtotal"], "discount_total": p["discount"], "tax_total": p["tax"],
                "total": p["total"], "balance_due": p["balance"], "notes": p["notes"], "po_number": p["po"],
                "external_source": p["src"], "external_ref": p["ref"], "journal_entry_id": None,
                "quote_ref": None, "created_by_uc_uid": p["uid"], "void_reason": None,
                "created_at": datetime.now(tz=timezone.utc),
            }
            return _Result([])
        if "UPDATE invoice SET quote_ref" in sql:
            self.invoices[p["id"]]["quote_ref"] = p["qr"]
            return _Result([])
        if "UPDATE invoice SET journal_entry_id" in sql:
            raise AssertionError("a DRAFT must never receive a journal_entry_id")
        if "UPDATE invoice" in sql and "status = 'void'" in sql:
            inv = self.invoices[p["id"]]
            inv["status"] = "void"
            inv["balance_due"] = Decimal("0")
            inv["void_reason"] = p["reason"]
            return _Result([])
        if "FROM invoice i" in sql and "i.id = :id" in sql:
            inv = self.invoices.get(p["id"])
            if inv is None:
                return _Result([])
            cp = self.counterparties.get(inv["counterparty_id"], {"name": "?"})
            return _Result([_Row(**inv, customer_name=cp["name"], entity_display_name="Vendor Co", entity_slug="vendor-co")])
        if "FROM invoice WHERE id = :id" in sql:
            inv = self.invoices.get(p["id"])
            return _Result([_Row(**inv)] if inv else [])
        if "INSERT INTO invoice_line" in sql:
            self.invoice_lines.append(dict(p))
            return _Result([])
        if "FROM invoice_line" in sql:
            return _Result([_Row(**ln) for ln in self.invoice_lines if ln["iid"] == p["id"]])
        if "sum(amount)" in sql and "invoice_payment" in sql:
            return _Result([_Row(paid=Decimal("0"))])
        if "FROM invoice_payment" in sql:
            return _Result([])
        if "FROM journal_entry" in sql and "reverses_entry_id IS NULL" in sql:
            return _Result([])  # a never-posted draft has no issue rows to reverse

        # ---- invoice_schedules ----
        if "INSERT INTO invoice_schedules" in sql:
            key = (p["wid"], p["qr"], p["pos"])
            if key not in self.schedules:
                self.schedules[key] = {
                    "id": p["id"], "workspace_id": p["wid"], "entity_id": p["eid"], "quote_ref": p["qr"],
                    "ar_customer_id": p["cust"], "line_position": p["pos"], "cadence": p["cad"],
                    "amount_cents": p["amt"], "currency": p["cur"], "description": p["desc"],
                    "anchor_date": p["anchor"], "net_terms": p["net"],
                    "term_months": p["term"], "periods_total": p["ptotal"], "periods_generated": 1,
                    "next_run_at": p["next"], "generated_invoice_ids": json.loads(p["ids"]),
                    "active": p["active"], "partner_supplier_id": p["psup"], "vendor_entity_id": p["veid"],
                    "partner_amount_cents": p["pamt"],
                    "partner_split": json.loads(p["psplit"]) if p["psplit"] else None,
                    "partner_periods_billed": p["pbilled"], "generated_bill_ids": [],
                    "created_at": datetime(2026, 9, 12, tzinfo=timezone.utc),
                }
            return _Result([])
        if "FROM invoice_schedules WHERE workspace_id = :wid AND quote_ref = :qr AND line_position = :pos" in sql:
            row = self.schedules.get((p["wid"], p["qr"], p["pos"]))
            return _Result([_Row(**row)] if row else [])
        if "FROM invoice_schedules WHERE workspace_id = :wid AND quote_ref LIKE :pfx" in sql:
            rows = [s for s in self.schedules.values()
                    if s["workspace_id"] == p["wid"] and s["active"] and self._like(s["quote_ref"], p["pfx"])]
            return _Result([_Row(id=s["id"], quote_ref=s["quote_ref"]) for s in rows])
        if "FROM invoice_schedules s" in sql and "JOIN counterparty c" in sql:
            assert "s.workspace_id = :wid" in sql, "runner must carry an explicit workspace predicate (M13)"
            rows = []
            far = datetime.max.replace(tzinfo=timezone.utc)
            for s in sorted(self.schedules.values(), key=lambda r: (r["next_run_at"] or far)):
                if s["workspace_id"] != p["wid"]:
                    continue
                if not s["active"] or s["next_run_at"] is None or s["next_run_at"] > p["cutoff"]:
                    continue
                if "s.entity_id = :eid" in sql and s["entity_id"] != p["eid"]:
                    continue
                cp = self.counterparties[s["ar_customer_id"]]
                rows.append(_Row(**s, customer_name=cp["name"], customer_refs=cp["refs"]))
            return _Result(rows)
        if "UPDATE invoice_schedules SET active = false, next_run_at = NULL" in sql:
            for s in self.schedules.values():
                if s["id"] == p["id"]:
                    s["active"] = False
                    s["next_run_at"] = None
            return _Result([])
        if "UPDATE invoice_schedules SET partner_periods_billed" in sql:
            for s in self.schedules.values():
                if s["id"] == p["id"]:
                    s["partner_periods_billed"] = p["pb"]
                    s["generated_bill_ids"] = json.loads(p["bids"])
            return _Result([])
        if "UPDATE invoice_schedules" in sql:
            for s in self.schedules.values():
                if s["id"] == p["id"]:
                    s["periods_generated"] = p["pgen"]
                    s["generated_invoice_ids"] = json.loads(p["ids"])
                    s["next_run_at"] = p["next"]
                    s["active"] = p["active"]
            return _Result([])

        # ---- bill ----
        if "FROM bill WHERE workspace_id = :wid AND external_source = :src AND external_ref = :ref" in sql:
            rows = [b for b in self.bills.values() if b["external_ref"] == p["ref"]]
            return _Result([_Row(**b) for b in rows])
        if "FROM bill WHERE workspace_id = :wid AND external_source = :src AND quote_ref LIKE :pfx" in sql:
            rows = [b for b in self.bills.values()
                    if b["status"] == "draft" and b["journal_entry_id"] is None and self._like(b["quote_ref"], p["pfx"])]
            return _Result([_Row(id=b["id"], quote_ref=b["quote_ref"]) for b in rows])
        if "count(*) AS n FROM bill" in sql:
            return _Result([_Row(n=self._n_bills)])
        if "INSERT INTO bill" in sql:
            if self.fail_bill_insert:
                raise RuntimeError("simulated bill insert failure")
            self._n_bills += 1
            self.bills[p["id"]] = {
                "id": p["id"], "bill_number": p["num"], "status": "draft", "total": p["total"],
                "lines": json.loads(p["lines"]), "notes": p["notes"], "external_ref": p["ref"],
                "quote_ref": p["qr"], "entity_id": p["eid"], "supplier_counterparty_id": p["sup"],
                "issue_date": p["issue"], "due_date": p["due"], "journal_entry_id": None, "void_reason": None,
            }
            return _Result([])
        if "UPDATE bill SET status = 'void'" in sql:
            b = self.bills[p["id"]]
            b["status"] = "void"
            b["void_reason"] = p["reason"]
            return _Result([])

        # ---- ledger: must never be touched ----
        if "INSERT INTO journal_entry" in sql or "INSERT INTO posting_log" in sql:
            self.journal_entries.append(dict(p))
            raise AssertionError("the quotes seam must never post to the journal")

        raise AssertionError(f"unexpected SQL in stub: {sql[:140]}")


async def _apply(s: _SeamStubSession, payload: QuoteAcceptedPayload, *, actor: str = "m2m:customer-ops",
                 entity_id: str = EID) -> dict[str, Any]:
    return await apply_quote_acceptance(s, workspace_id=WID, entity_id=entity_id, actor_uc_uid=actor, payload=payload)


@pytest.mark.asyncio
async def test_apply_creates_drafts_only_and_never_posts():
    s = _SeamStubSession()
    res = await _apply(s, _payload())
    assert res["quote_ref"] == "Q-2026-0001 v2"
    assert res["created"] is True and res["replayed"] is False
    # deposit + 5 remaining parts + 3 first-period recurring drafts = 9 invoices.
    assert len(s.invoices) == 9
    assert res["deposit_invoice_id"] is not None
    assert len(res["milestone_invoice_ids"]) == 5
    assert len(res["schedule_ids"]) == 3
    assert res["partner_invoice_id"] is not None
    assert res["net_terms"] == 30
    for inv in s.invoices.values():
        assert inv["status"] == "draft"
        assert inv["journal_entry_id"] is None
        assert inv["quote_ref"] == "Q-2026-0001 v2"
        assert inv["external_source"] == "customer-ops"
        assert inv["created_by_uc_uid"] == "m2m:customer-ops"
        assert inv["due_date"] == date(2026, 10, 12)   # net 30 from the acceptance date
    assert s.journal_entries == []
    dep = s.invoices[res["deposit_invoice_id"]]
    assert Decimal(str(dep["total"])) == Decimal("2750.00")
    assert dep["external_ref"] == "Q-2026-0001 v2:deposit"
    memos = [i["memo"] for i in res["invoices"] if i["kind"] == "milestone"]
    assert memos[0] == "Due on acceptance of Order Pad build — Starter package"
    assert memos[-1].endswith("(part 3 of 3)")
    assert res["invoices"][0]["materiality_tier"] == "H"
    assert res["review_required"] is True
    hosting = next(x for x in res["schedules"] if x["position"] == 5)
    assert hosting["periods_total"] == 12 and hosting["periods_generated"] == 1
    assert hosting["next_run_at"].startswith("2026-10-12")
    assert hosting["anchor_date"] == "2026-09-12" and hosting["net_terms"] == 30
    assert hosting["is_hosting"] is True
    # H3: the hosting schedule carries the partner side; the AI allowance does not.
    assert hosting["partner_supplier_id"] == res["partner_supplier_id"]
    assert hosting["partner_amount_cents"] == 35_000 and hosting["partner_periods_billed"] == 1
    ai = next(x for x in res["schedules"] if x["position"] == 6)
    assert ai["partner_supplier_id"] is None and ai["partner_amount_cents"] == 0
    sched_row = s.schedules[(WID, "Q-2026-0001 v2", 5)]
    assert sched_row["vendor_entity_id"] == EID
    assert sched_row["partner_split"]["percent"] == "50" and sched_row["partner_split"]["is_hosting"] is True
    cust = s.counterparties[res["ar_customer_id"]]
    assert cust["name"] == "the apparel client" and cust["kind"] == "customer"
    assert cust["refs"]["contact_id"] == "co-2467436b"
    assert cust["refs"]["contact_name"] == "Ops Lead"
    assert res["federated"] == {"accounting_ops": "done", "partner": "done", "reason": None}
    bill = s.bills[res["partner_invoice_id"]]
    assert bill["status"] == "draft" and bill["journal_entry_id"] is None
    assert Decimal(str(bill["total"])) == Decimal("2850.00")
    assert bill["entity_id"] == EID and bill["bill_number"] == "BILL-2026-0001"
    # H1: no scientific notation anywhere on the AP document.
    assert "5E+1" not in bill["notes"] and "50% of net fixed scope" in bill["notes"]
    assert all("E+" not in ln["description"] for ln in bill["lines"])
    supplier = s.counterparties[res["partner_supplier_id"]]
    assert supplier["kind"] == "vendor" and supplier["refs"]["delivery_partner"] is True
    assert "PAYABLE" in bill["notes"] and "never AR" in bill["notes"]
    assert res["partner_bill"]["total_cents"] == 285_000
    assert res["totals"]["deposit_cents"] == 275_000
    assert res["superseded"]["versions"] == [] and res["superseded"]["voided_invoice_ids"] == []


@pytest.mark.asyncio
async def test_apply_is_idempotent_by_quote_ref_and_version():
    s = _SeamStubSession()
    first = await _apply(s, _payload())
    n_inv, n_sched, n_bill, n_cp = len(s.invoices), len(s.schedules), len(s.bills), len(s.counterparties)
    second = await _apply(s, _payload())
    assert second["created"] is False and second["replayed"] is True
    for key in ("ar_customer_id", "deposit_invoice_id", "milestone_invoice_ids", "schedule_ids",
                "partner_invoice_id", "partner_supplier_id"):
        assert second[key] == first[key], key
    assert (len(s.invoices), len(s.schedules), len(s.bills), len(s.counterparties)) == (n_inv, n_sched, n_bill, n_cp)
    assert all(i["created"] is False for i in second["invoices"])
    assert all(i["status"] == "draft" for i in s.invoices.values())   # a replay voids nothing
    assert s.journal_entries == []


@pytest.mark.asyncio
async def test_new_version_voids_prior_unissued_paper_then_creates_its_own():
    s = _SeamStubSession()
    v1 = await _apply(s, _payload(version=1))
    v1_invoice_ids = {i["id"] for i in v1["invoices"]}
    # Simulate that ONE v1 draft was already issued by a human (has a journal entry).
    issued_id = v1["milestone_invoice_ids"][0]
    s.invoices[issued_id]["status"] = "sent"
    s.invoices[issued_id]["journal_entry_id"] = "je-issued"

    v2 = await _apply(s, _payload(version=2))
    assert v2["created"] is True and v2["quote_ref"] == "Q-2026-0001 v2"
    sup = v2["superseded"]
    assert sup["versions"] == [1]
    assert set(sup["voided_invoice_ids"]) == v1_invoice_ids - {issued_id}
    assert sup["left_open_invoice_ids"] == [issued_id]
    assert set(sup["retired_schedule_ids"]) == set(v1["schedule_ids"])
    assert sup["voided_bill_ids"] == [v1["partner_invoice_id"]]
    for iid in sup["voided_invoice_ids"]:
        assert s.invoices[iid]["status"] == "void"
        assert "Superseded by accepted quote Q-2026-0001 v2" in s.invoices[iid]["void_reason"]
    assert s.invoices[issued_id]["status"] == "sent"                 # issued paper is never touched
    for key, row in s.schedules.items():
        if key[1] == "Q-2026-0001 v1":
            assert row["active"] is False and row["next_run_at"] is None
        else:
            assert row["active"] is True
    assert s.bills[v1["partner_invoice_id"]]["status"] == "void"
    assert s.bills[v2["partner_invoice_id"]]["status"] == "draft"
    assert s.journal_entries == []                                    # voiding a draft posts nothing
    # v2's own paper is a full fresh set.
    assert v2["deposit_invoice_id"] != v1["deposit_invoice_id"]
    assert len([i for i in s.invoices.values() if i["quote_ref"] == "Q-2026-0001 v2"]) == 9
    # Replaying v2 voids nothing further; an out-of-order v1 replay never touches v2.
    again = await _apply(s, _payload(version=2))
    assert again["created"] is False and again["superseded"]["voided_invoice_ids"] == []
    late_v1 = await _apply(s, _payload(version=1))
    assert late_v1["superseded"]["versions"] == []
    assert all(i["status"] == "draft" for i in s.invoices.values() if i["quote_ref"] == "Q-2026-0001 v2")


@pytest.mark.asyncio
async def test_apply_refuses_closed_period_and_unknown_entity():
    s = _SeamStubSession(closed_through={EID: date(2026, 9, 30)})
    with pytest.raises(QuoteSeamError) as exc:
        await _apply(s, _payload())
    assert exc.value.code == "PERIOD_CLOSED" and exc.value.http_status == 409
    assert s.invoices == {} and s.journal_entries == []

    s = _SeamStubSession()
    with pytest.raises(QuoteSeamError) as exc:
        await _apply(s, _payload(), entity_id="44444444-4444-4444-8444-444444444444")
    assert exc.value.code == "ENTITY_NOT_FOUND" and exc.value.http_status == 404


@pytest.mark.asyncio
async def test_vendor_entity_closed_period_degrades_to_partner_failed():
    """H4: the AP bill is checked against the VENDOR entity's closed periods."""
    s = _SeamStubSession(closed_through={VENDOR_EID: date(2026, 9, 30)})
    res = await _apply(s, _payload(vendor_entity_id=VENDOR_EID))
    assert len(s.invoices) == 9                      # customer drafts (AR entity is open) all created
    assert res["partner_invoice_id"] is None and s.bills == {}
    assert res["federated"]["partner"] == "failed"
    assert res["federated"]["reason"].startswith("PERIOD_CLOSED")
    assert VENDOR_EID in res["federated"]["reason"]
    # The schedule rows still carry the partner side, so the runner can catch up later.
    assert s.schedules[(WID, "Q-2026-0001 v2", 5)]["vendor_entity_id"] == VENDOR_EID
    # Once the vendor period reopens, the same call completes the partner side only.
    s.closed_through = {}
    again = await _apply(s, _payload(vendor_entity_id=VENDOR_EID))
    assert again["federated"]["partner"] == "done" and len(s.invoices) == 9 and len(s.bills) == 1
    assert s.bills[again["partner_invoice_id"]]["entity_id"] == VENDOR_EID


@pytest.mark.asyncio
async def test_apply_partner_failure_is_isolated_and_retry_completes_it():
    s = _SeamStubSession(fail_bill_insert=True)
    res = await _apply(s, _payload())
    assert len(s.invoices) == 9 and len(s.schedules) == 3
    assert res["partner_invoice_id"] is None
    assert res["federated"]["accounting_ops"] == "done"
    assert res["federated"]["partner"] == "failed"
    assert "simulated bill insert failure" in res["federated"]["reason"]
    s.fail_bill_insert = False
    again = await _apply(s, _payload())
    assert len(s.invoices) == 9 and len(s.bills) == 1
    assert again["federated"]["partner"] == "done"
    assert again["deposit_invoice_id"] == res["deposit_invoice_id"]


@pytest.mark.asyncio
async def test_partner_supplier_id_must_exist_and_be_a_vendor():
    s = _SeamStubSession()
    payload = _payload(delivery_partner={"name": "Synthetic Delivery Partner LLC",
                                         "partner_supplier_id": "44444444-4444-4444-8444-444444444444"})
    res = await _apply(s, payload)
    assert res["federated"]["partner"] == "failed"
    assert res["federated"]["reason"].startswith("PARTNER_SUPPLIER_NOT_FOUND")
    assert s.bills == {}
    # M7: an AR customer's counterparty id must never become payable.
    s = _SeamStubSession()
    s.add_counterparty("55555555-5555-4555-8555-555555555555", "the apparel client", "customer")
    payload = _payload(delivery_partner={"name": "x", "partner_supplier_id": "55555555-5555-4555-8555-555555555555"})
    res = await _apply(s, payload)
    assert res["federated"]["partner"] == "failed"
    assert res["federated"]["reason"].startswith("PARTNER_SUPPLIER_NOT_VENDOR")
    assert s.bills == {}
    # ... while a real supplier id is used as given.
    s = _SeamStubSession()
    s.add_counterparty("66666666-6666-4666-8666-666666666666", "Synthetic Delivery Partner LLC", "vendor")
    res = await _apply(s, _payload(delivery_partner={"name": "x", "partner_supplier_id": "66666666-6666-4666-8666-666666666666"}))
    assert res["federated"]["partner"] == "done"
    assert res["partner_supplier_id"] == "66666666-6666-4666-8666-666666666666"


@pytest.mark.asyncio
async def test_apply_without_partner_skips_cleanly():
    s = _SeamStubSession()
    res = await _apply(s, _payload(delivery_partner=None, split_rule=None), actor="tester")
    assert res["partner_invoice_id"] is None
    assert res["federated"] == {"accounting_ops": "done", "partner": "skipped",
                                "reason": "no delivery partner on the quote"}
    assert all(x["partner_supplier_id"] is None for x in res["schedules"])
    res = await _apply(s, _payload(version=9, split_rule={"kind": "none"}), actor="tester")
    assert res["federated"]["partner"] == "skipped"
    assert res["federated"]["reason"] == "split_rule.kind=none and hosting stays with the vendor"
    # H2: kind=none + hosting_to_partner still produces the bill.
    res = await _apply(s, _payload(version=10, split_rule={"kind": "none", "hosting_to_partner": True}), actor="tester")
    assert res["federated"]["partner"] == "done"
    assert res["partner_bill"]["total_cents"] == 35_000


@pytest.mark.asyncio
async def test_net_terms_resolution_payload_then_workspace_then_30():
    s = _SeamStubSession(policy_net_terms=45)
    res = await _apply(s, _payload(net_terms=15))
    assert res["net_terms"] == 15
    assert all(i["due_date"] == date(2026, 9, 27) for i in s.invoices.values())
    assert s.schedules[(WID, "Q-2026-0001 v2", 5)]["net_terms"] == 15
    s = _SeamStubSession(policy_net_terms=45)
    res = await _apply(s, _payload())
    assert res["net_terms"] == 45
    assert all(i["due_date"] == date(2026, 10, 27) for i in s.invoices.values())
    s = _SeamStubSession()
    res = await _apply(s, _payload())
    assert res["net_terms"] == 30


# ===========================================================================
# 3. Schedule generation
# ===========================================================================


async def _seed_schedules(s: _SeamStubSession, **overrides: Any) -> dict[str, Any]:
    return await _apply(s, _payload(**overrides))


async def _run(s: _SeamStubSession, as_of: date) -> dict[str, Any]:
    return await generate_due_schedule_invoices(s, workspace_id=WID, actor_uc_uid="scheduler", as_of=as_of)


@pytest.mark.asyncio
async def test_schedule_runner_generates_next_period_draft_and_advances():
    s = _SeamStubSession()
    await _seed_schedules(s, delivery_partner=None, split_rule=None)
    before = len(s.invoices)
    res = await _run(s, date(2026, 10, 11))
    assert res["generated"] == [] and len(s.invoices) == before
    res = await _run(s, date(2026, 10, 12))
    assert len(res["generated"]) == 2
    assert {g["period_no"] for g in res["generated"]} == {2}
    assert len(s.invoices) == before + 2
    hosting = s.schedules[(WID, "Q-2026-0001 v2", 5)]
    assert hosting["periods_generated"] == 2
    assert len(hosting["generated_invoice_ids"]) == 2
    assert hosting["next_run_at"].date() == date(2026, 11, 12)
    assert hosting["active"] is True
    new_inv = s.invoices[res["generated"][0]["invoice_id"]]
    assert new_inv["status"] == "draft" and new_inv["journal_entry_id"] is None
    assert new_inv["external_ref"].endswith(":recurring:5:2") or new_inv["external_ref"].endswith(":recurring:6:2")
    assert new_inv["issue_date"] == date(2026, 10, 12)
    assert new_inv["due_date"] == date(2026, 11, 11)   # M10: net 30 persisted on the schedule
    res = await _run(s, date(2026, 10, 12))
    assert res["generated"] == [] and len(s.invoices) == before + 2
    assert res["partner_bills"] == []                  # no partner on this quote
    assert s.journal_entries == []


@pytest.mark.asyncio
async def test_schedule_runner_honours_net_terms_and_anchor_day():
    s = _SeamStubSession()
    await _seed_schedules(s, delivery_partner=None, split_rule=None, accepted_at="2026-01-31", net_terms=15)
    res = await _run(s, date(2026, 4, 30))
    hosting_rows = [g for g in res["generated"] if s.invoices[g["invoice_id"]]["external_ref"].endswith(("5:2", "5:3", "5:4"))]
    dates = [s.invoices[g["invoice_id"]]["issue_date"] for g in hosting_rows]
    # M9: Jan 31 anchor -> Feb 28, Mar 31, Apr 30 (never a drift to the 28th).
    assert dates == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
    assert [s.invoices[g["invoice_id"]]["due_date"] for g in hosting_rows] == [
        date(2026, 3, 15), date(2026, 4, 15), date(2026, 5, 15),
    ]
    assert s.schedules[(WID, "Q-2026-0001 v2", 5)]["next_run_at"].date() == date(2026, 5, 31)


@pytest.mark.asyncio
async def test_schedule_runner_catches_up_and_retires_at_term_end():
    s = _SeamStubSession()
    await _seed_schedules(s, delivery_partner=None, split_rule=None)
    res = await _run(s, date(2028, 12, 31))
    by_sched: dict[str, list] = {}
    for g in res["generated"]:
        by_sched.setdefault(g["schedule_id"], []).append(g["period_no"])
    hosting = s.schedules[(WID, "Q-2026-0001 v2", 5)]
    annual = s.schedules[(WID, "Q-2026-0001 v2", 7)]
    assert by_sched[hosting["id"]] == list(range(2, 13))
    assert by_sched[annual["id"]] == [2]
    assert hosting["periods_generated"] == 12 and hosting["active"] is False and hosting["next_run_at"] is None
    assert annual["periods_generated"] == 2 and annual["active"] is False
    assert len(hosting["generated_invoice_ids"]) == 12
    res = await _run(s, date(2030, 1, 1))
    assert res["schedules_due"] == 0


@pytest.mark.asyncio
async def test_schedule_runner_skips_closed_period_without_advancing():
    s = _SeamStubSession()
    await _seed_schedules(s, delivery_partner=None, split_rule=None)
    s.closed_through = {EID: date(2026, 10, 31)}
    res = await _run(s, date(2026, 10, 12))
    assert res["generated"] == []
    assert len(res["skipped"]) == 2 and "period closed" in res["skipped"][0]["reason"]
    hosting = s.schedules[(WID, "Q-2026-0001 v2", 5)]
    assert hosting["periods_generated"] == 1 and hosting["next_run_at"].date() == date(2026, 10, 12)
    s.closed_through = {}
    res = await _run(s, date(2026, 10, 12))
    assert len(res["generated"]) == 2


@pytest.mark.asyncio
async def test_schedule_runner_bills_the_partner_for_every_generated_period():
    """H3: a pass-through line gets a partner bill draft per period, on the vendor entity."""
    s = _SeamStubSession()
    acc = await _seed_schedules(s, vendor_entity_id=VENDOR_EID)
    assert len(s.bills) == 1                      # the acceptance bill (fixed scope + period 1 hosting)
    res = await _run(s, date(2026, 12, 12))       # periods 2, 3, 4 due
    assert {g["period_no"] for g in res["generated"] if g["quote_ref"] == "Q-2026-0001 v2"} == {2, 3, 4}
    hosting_bills = [b for b in res["partner_bills"]]
    assert [b["period_no"] for b in hosting_bills] == [2, 3, 4]
    assert all(b["amount_cents"] == 35_000 for b in hosting_bills)
    assert len(s.bills) == 4
    for b in hosting_bills:
        row = s.bills[b["bill_id"]]
        assert row["status"] == "draft" and row["journal_entry_id"] is None
        assert row["entity_id"] == VENDOR_EID and row["supplier_counterparty_id"] == acc["partner_supplier_id"]
        assert row["external_ref"] == f"Q-2026-0001 v2:partner:recurring:5:{b['period_no']}"
        assert "PAYABLE" in row["notes"]
    hosting = s.schedules[(WID, "Q-2026-0001 v2", 5)]
    assert hosting["partner_periods_billed"] == 4 and len(hosting["generated_bill_ids"]) == 3
    ai = s.schedules[(WID, "Q-2026-0001 v2", 6)]
    assert ai["partner_periods_billed"] == 0 and ai["generated_bill_ids"] == []
    # Idempotent: a re-run bills nothing twice.
    res = await _run(s, date(2026, 12, 12))
    assert res["partner_bills"] == [] and len(s.bills) == 4
    assert s.journal_entries == []


@pytest.mark.asyncio
async def test_schedule_runner_partner_cursor_survives_a_closed_vendor_period():
    """H4 on the runner: a closed VENDOR period skips the partner bill (customer draft still
    generated) and the partner cursor stays put, so the next run catches up."""
    s = _SeamStubSession()
    await _seed_schedules(s, vendor_entity_id=VENDOR_EID)
    s.closed_through = {VENDOR_EID: date(2026, 10, 31)}
    res = await _run(s, date(2026, 10, 12))
    assert {g["period_no"] for g in res["generated"]} == {2}        # AR side generated
    assert res["partner_bills"] == []
    assert len(res["partner_skipped"]) == 1 and res["partner_skipped"][0]["reason"].startswith("PERIOD_CLOSED")
    hosting = s.schedules[(WID, "Q-2026-0001 v2", 5)]
    assert hosting["periods_generated"] == 2 and hosting["partner_periods_billed"] == 1
    s.closed_through = {}
    res = await _run(s, date(2026, 11, 12))
    assert [b["period_no"] for b in res["partner_bills"]] == [2, 3]  # caught up
    assert hosting["partner_periods_billed"] == 3


@pytest.mark.asyncio
async def test_seam_tools_fail_closed_without_auth():
    """The MCP tools DENY (PermissionError) with no bound caller (multi-tenant safety)."""
    import uuid

    from app.tools import create_invoices_from_quote, list_invoice_schedules, run_invoice_schedules

    wid, eid = uuid.uuid4(), uuid.uuid4()
    with pytest.raises(PermissionError):
        await create_invoices_from_quote(wid, eid, "Q-1", {"display_name": "x"}, _lines())
    with pytest.raises(PermissionError):
        await run_invoice_schedules(wid)
    with pytest.raises(PermissionError):
        await list_invoice_schedules(wid)


def test_existing_ar_primitive_unchanged_by_default():
    """The additive post_journal/quote_ref kwargs default to today's behaviour."""
    import inspect

    from app.ar.invoicing import create_invoice_from_draft

    sig = inspect.signature(create_invoice_from_draft)
    assert sig.parameters["post_journal"].default is True
    assert sig.parameters["quote_ref"].default is None
    assert qs.QUOTES_SEAM_SOURCE == "customer-ops"
    assert qs.DEFAULT_NET_TERMS == 30


def test_quotes_enabled_fence_reads_settings(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.delenv("QUOTES_ENABLED", raising=False)
    assert qs.quotes_enabled() is False
    get_settings.cache_clear()
    monkeypatch.setenv("QUOTES_ENABLED", "1")
    assert qs.quotes_enabled() is True
    get_settings.cache_clear()
