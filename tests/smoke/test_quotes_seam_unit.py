"""Unit tests for the Quotes → money seam (app.ar.quotes_seam) — no live Postgres.

Three tiers:

  1. Pure planning maths: per-line payment splits (A5), exact-sum rounding,
     deposit / remaining-part drafts, recurring schedules (month|quarter|year),
     totals cross-check (A4), currency exponent (A17), line kinds (A16), and
     the partner split (percent_net | fee | none, hosting pass-through,
     direct costs).
  2. Stubbed-session apply: the whole acceptance creates the right DRAFTS with
     NO journal entry, is idempotent by quote_ref + version (a replay returns
     the same ids and creates nothing), refuses a closed period, and isolates a
     partner failure (federated.partner = failed, customer drafts kept).
  3. Stubbed-session schedule generation: the runner creates the next period's
     draft, advances next_run_at, retires the schedule at the end of its term,
     catches up several periods, and skips closed periods.

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
    full_quote_ref,
    generate_due_schedule_invoices,
    periods_for_term,
    plan_quote_billing,
    split_parts,
    validate_currency,
)

WID = "11111111-1111-4111-8111-111111111111"
EID = "22222222-2222-4222-8222-222222222222"


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


def test_full_quote_ref_is_number_plus_version_not_doubled():
    assert full_quote_ref("Q-2026-0001", 2) == "Q-2026-0001 v2"
    assert full_quote_ref("Q-2026-0001 v2", 2) == "Q-2026-0001 v2"
    assert full_quote_ref(" Q-2026-0001 ", 1) == "Q-2026-0001 v1"


@pytest.mark.parametrize(
    "amount,split,expected",
    [
        (100_000, [50, 50], [50_000, 50_000]),
        (100_001, [50, 50], [50_001, 50_000]),       # A5 part 0 = round-half-up; parts sum exactly
        (1_003, [50, 25, 25], [502, 251, 250]),      # part 0 half-up (501.5 -> 502); rest apportioned
        (100, [33, 33, 34], [33, 33, 34]),
        (101, [33, 33, 34], [33, 33, 35]),      # largest remainder: 33.49 vs 34.51 -> last part
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


def test_advance_period_month_quarter_year_clamps_day():
    assert advance_period(date(2026, 1, 31), "month") == date(2026, 2, 28)
    assert advance_period(date(2026, 11, 30), "quarter") == date(2027, 2, 28)
    assert advance_period(date(2028, 2, 29), "year") == date(2029, 2, 28)
    assert advance_period(date(2026, 9, 12), "month") == date(2026, 10, 12)
    with pytest.raises(QuoteSeamError):
        advance_period(date(2026, 1, 1), "week")


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
    # Deposit: Σ amount × split[0] per accepted one_time + milestone line.
    assert plan.deposit_cents == 275_000
    assert [(p.position, p.amount_cents) for p in plan.deposit_parts] == [
        (1, 70_000), (2, 100_000), (3, 75_000), (4, 30_000),
    ]
    # Remaining parts: one draft per part, "Due on acceptance of <label>".
    drafts = [(d.position, d.amount_cents, d.memo) for d in plan.milestone_drafts]
    assert drafts == [
        (1, 70_000, "Due on acceptance of Order Pad build — Starter package"),
        (2, 100_000, "Due on acceptance of Milestone 1 — Catalogue + ordering"),
        (3, 75_000, "Due on acceptance of Milestone 2 — Approvals + invoicing"),
        (4, 30_000, "Due on acceptance of Milestone 3 — Go-live + training (part 2 of 3)"),
        (4, 40_000, "Due on acceptance of Milestone 3 — Go-live + training (part 3 of 3)"),
    ]
    # Every cent of the fixed scope is either deposit or a remaining part.
    assert plan.deposit_cents + sum(d.amount_cents for d in plan.milestone_drafts) == 590_000
    # Schedules: month x2 (12 periods), year x1 (2 periods); hosting flagged by label.
    sched = [(s.position, s.cadence, s.amount_cents, s.periods_total, s.is_hosting) for s in plan.schedules]
    assert sched == [
        (5, "month", 35_000, 12, True),
        (6, "month", 5_000, 12, False),
        (7, "year", 120_000, 2, False),
    ]
    # hourly / included / excluded never invoice.
    assert sorted(s["position"] for s in plan.skipped_lines) == [8, 9, 10]
    assert plan.computed_totals == {
        "one_time_total_cents": 140_000,
        "milestone_total_cents": 450_000,
        "deposit_cents": 275_000,
        "recurring": {"month": 40_000, "year": 120_000},
    }
    # Partner: 50% of (590,000 - 90,000) = 250,000 + hosting 35,000 in full.
    assert plan.partner is not None
    assert plan.partner.total_cents == 285_000
    assert [pl.amount_cents for pl in plan.partner.lines] == [250_000, 35_000]


def test_plan_totals_cross_check_refuses_mismatch():
    totals = _totals()
    totals["deposit_cents"] = 275_001
    with pytest.raises(QuoteSeamError) as exc:
        plan_quote_billing(_payload(totals=totals))
    assert exc.value.code == "TOTALS_MISMATCH"
    assert "deposit_cents" in exc.value.reason
    # recurring.quarter mismatch (absent on our side == 0)
    totals = _totals()
    totals["recurring"]["quarter"] = 1
    with pytest.raises(QuoteSeamError, match="recurring.quarter"):
        plan_quote_billing(_payload(totals=totals))


def test_plan_without_totals_block_still_plans():
    plan = plan_quote_billing(_payload(totals=None))
    assert plan.deposit_cents == 275_000


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


def test_partner_share_percent_net_fee_none_and_hosting():
    hosting = [("Managed hosting", 35_000)]
    # percent_net after direct costs.
    p = compute_partner_share(
        split_rule=SplitRule(kind="percent_net", percent=Decimal("50"), hosting_to_partner=True,
                             direct_costs_total_cents=90_000),
        fixed_total_cents=590_000, hosting_lines=hosting, currency="USD",
    )
    assert p is not None and p.total_cents == 285_000
    # hosting NOT to partner -> only the net share.
    p = compute_partner_share(
        split_rule=SplitRule(kind="percent_net", percent=Decimal("33.5"), direct_costs_total_cents=0),
        fixed_total_cents=100_001, hosting_lines=hosting, currency="USD",
    )
    assert p is not None and p.total_cents == 33_500  # 33500.335 -> 33500
    # direct costs exceed the fixed scope -> net 0 -> hosting only.
    p = compute_partner_share(
        split_rule=SplitRule(kind="percent_net", percent=Decimal("50"), hosting_to_partner=True,
                             direct_costs_total_cents=1_000_000),
        fixed_total_cents=590_000, hosting_lines=hosting, currency="USD",
    )
    assert p is not None and [pl.amount_cents for pl in p.lines] == [35_000]
    # ... and nothing at all when hosting stays with the vendor -> None (skipped).
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
    # none kind.
    assert compute_partner_share(split_rule=SplitRule(kind="none"), fixed_total_cents=1, hosting_lines=hosting,
                                 currency="USD") is None


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
    """In-memory fake AsyncSession for the seam + the AR primitive it reuses.

    Records every INSERT/UPDATE so tests can assert exactly what was created.
    ``closed_through`` simulates a closed_period row; ``fail_bill_insert``
    simulates a partner-step failure.
    """

    def __init__(self, *, closed_through: date | None = None, fail_bill_insert: bool = False,
                 entity_ids: set[str] | None = None):
        self.entities = entity_ids or {EID}
        self.closed_through = closed_through
        self.fail_bill_insert = fail_bill_insert
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

    # -- helpers ----------------------------------------------------------
    def _invoice_rows(self, rows):
        return _Result([_Row(**r) for r in rows])

    async def execute(self, query, params=None):  # noqa: C901 - dispatch table
        sql = " ".join(str(query).split())
        p = dict(params or {})

        # ---- entity / closed_period ----
        if "FROM entity WHERE id = :eid" in sql:
            return _Result([(1,)] if p["eid"] in self.entities else [])
        if "FROM closed_period" in sql:
            if self.closed_through is not None and p["effective_date"] <= self.closed_through:
                return _Result([(self.closed_through,)])
            return _Result([])

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
            return _Result([_Row(id=cp["id"], display_name=cp["name"])] if cp else [])
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
            self.counterparties[p["id"]] = {
                "id": p["id"], "name": p["name"], "kind": kind, "refs": json.loads(p["refs"]),
            }
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
            return self._invoice_rows(rows)
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
                "quote_ref": None, "created_by_uc_uid": p["uid"],
            }
            return _Result([])
        if "UPDATE invoice SET quote_ref" in sql:
            self.invoices[p["id"]]["quote_ref"] = p["qr"]
            return _Result([])
        if "UPDATE invoice SET journal_entry_id" in sql:
            raise AssertionError("a DRAFT must never receive a journal_entry_id")
        if "FROM invoice i" in sql and "i.id = :id" in sql:
            inv = self.invoices.get(p["id"])
            if inv is None:
                return _Result([])
            cp = self.counterparties.get(inv["counterparty_id"], {"name": "?"})
            return _Result([_Row(**inv, customer_name=cp["name"], entity_display_name="Vendor Co", entity_slug="vendor-co")])
        if "INSERT INTO invoice_line" in sql:
            self.invoice_lines.append(dict(p))
            return _Result([])
        if "FROM invoice_line" in sql:
            return _Result([_Row(**ln) for ln in self.invoice_lines if ln["iid"] == p["id"]])
        if "FROM invoice_payment" in sql:
            return _Result([])

        # ---- invoice_schedules ----
        if "INSERT INTO invoice_schedules" in sql:
            key = (p["wid"], p["qr"], p["pos"])
            if key not in self.schedules:
                self.schedules[key] = {
                    "id": p["id"], "workspace_id": p["wid"], "entity_id": p["eid"], "quote_ref": p["qr"],
                    "ar_customer_id": p["cust"], "line_position": p["pos"], "cadence": p["cad"],
                    "amount_cents": p["amt"], "currency": p["cur"], "description": p["desc"],
                    "term_months": p["term"], "periods_total": p["ptotal"], "periods_generated": 1,
                    "next_run_at": p["next"], "generated_invoice_ids": json.loads(p["ids"]),
                    "active": p["active"], "created_at": datetime(2026, 9, 12, tzinfo=timezone.utc),
                }
            return _Result([])
        if "FROM invoice_schedules WHERE workspace_id = :wid AND quote_ref = :qr AND line_position = :pos" in sql:
            row = self.schedules.get((p["wid"], p["qr"], p["pos"]))
            return _Result([_Row(**row)] if row else [])
        if "FROM invoice_schedules s" in sql and "JOIN counterparty c" in sql:
            rows = []
            for s in sorted(self.schedules.values(), key=lambda r: (r["next_run_at"] or datetime.max.replace(tzinfo=timezone.utc))):
                if not s["active"] or s["next_run_at"] is None or s["next_run_at"] > p["cutoff"]:
                    continue
                if "s.entity_id = :eid" in sql and s["entity_id"] != p["eid"]:
                    continue
                cp = self.counterparties[s["ar_customer_id"]]
                rows.append(_Row(**s, customer_name=cp["name"], customer_refs=cp["refs"]))
            return _Result(rows)
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
        if "count(*) AS n FROM bill" in sql:
            return _Result([_Row(n=self._n_bills)])
        if "INSERT INTO bill" in sql:
            if self.fail_bill_insert:
                raise RuntimeError("simulated bill insert failure")
            self._n_bills += 1
            self.bills[p["id"]] = {
                "id": p["id"], "bill_number": p["num"], "status": "draft", "total": p["total"],
                "lines": json.loads(p["lines"]), "notes": p["notes"], "external_ref": p["ref"],
                "entity_id": p["eid"], "supplier_counterparty_id": p["sup"], "journal_entry_id": None,
            }
            return _Result([])

        # ---- ledger: must never be touched ----
        if "INSERT INTO journal_entry" in sql or "INSERT INTO posting_log" in sql:
            self.journal_entries.append(dict(p))
            raise AssertionError("the quotes seam must never post to the journal")

        raise AssertionError(f"unexpected SQL in stub: {sql[:120]}")


@pytest.mark.asyncio
async def test_apply_creates_drafts_only_and_never_posts():
    s = _SeamStubSession()
    res = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=_payload(),
    )
    assert res["quote_ref"] == "Q-2026-0001 v2"
    assert res["created"] is True and res["replayed"] is False
    # deposit + 5 remaining parts + 3 first-period recurring drafts = 9 invoices.
    assert len(s.invoices) == 9
    assert res["deposit_invoice_id"] is not None
    assert len(res["milestone_invoice_ids"]) == 5
    assert len(res["schedule_ids"]) == 3
    assert res["partner_invoice_id"] is not None
    # Every invoice is a DRAFT with NO journal entry, stamped with the quote_ref.
    for inv in s.invoices.values():
        assert inv["status"] == "draft"
        assert inv["journal_entry_id"] is None
        assert inv["quote_ref"] == "Q-2026-0001 v2"
        assert inv["external_source"] == "customer-ops"
        assert inv["created_by_uc_uid"] == "m2m:customer-ops"
    assert s.journal_entries == []
    # Deposit total = Σ part-0 amounts; the deposit memo is on the invoice.
    dep = s.invoices[res["deposit_invoice_id"]]
    assert Decimal(str(dep["total"])) == Decimal("2750.00")
    assert dep["external_ref"] == "Q-2026-0001 v2:deposit"
    # Milestone draft memos.
    memos = [i["memo"] for i in res["invoices"] if i["kind"] == "milestone"]
    assert memos[0] == "Due on acceptance of Order Pad build — Starter package"
    assert memos[-1].endswith("(part 3 of 3)")
    # Materiality is surfaced (deposit is tier H) and review is flagged.
    assert res["invoices"][0]["materiality_tier"] == "H"
    assert res["review_required"] is True
    # Schedules: first period generated now, next period one cadence later.
    hosting = next(x for x in res["schedules"] if x["position"] == 5)
    assert hosting["periods_total"] == 12 and hosting["periods_generated"] == 1
    assert hosting["next_run_at"].startswith("2026-10-12")
    assert hosting["is_hosting"] is True
    # Customer upserted as the org ("the apparel client") with the contact stashed as a thin ref.
    cust = s.counterparties[res["ar_customer_id"]]
    assert cust["name"] == "the apparel client" and cust["kind"] == "customer"
    assert cust["refs"]["contact_id"] == "co-2467436b"
    assert cust["refs"]["contact_name"] == "Ops Lead"
    # Partner: an AP bill DRAFT from the supplier to the vendor entity, never posted.
    assert res["federated"] == {"accounting_ops": "done", "partner": "done", "reason": None}
    bill = s.bills[res["partner_invoice_id"]]
    assert bill["status"] == "draft" and bill["journal_entry_id"] is None
    assert Decimal(str(bill["total"])) == Decimal("2850.00")
    assert bill["entity_id"] == EID
    supplier = s.counterparties[res["partner_supplier_id"]]
    assert supplier["kind"] == "vendor" and supplier["refs"]["delivery_partner"] is True
    assert "PAYABLE" in bill["notes"] and "never AR" in bill["notes"]
    assert res["partner_bill"]["total_cents"] == 285_000
    assert res["totals"]["deposit_cents"] == 275_000


@pytest.mark.asyncio
async def test_apply_is_idempotent_by_quote_ref_and_version():
    s = _SeamStubSession()
    first = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=_payload(),
    )
    n_inv, n_sched, n_bill, n_cp = len(s.invoices), len(s.schedules), len(s.bills), len(s.counterparties)
    second = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=_payload(),
    )
    # Same ids, nothing created.
    assert second["created"] is False and second["replayed"] is True
    for key in ("ar_customer_id", "deposit_invoice_id", "milestone_invoice_ids", "schedule_ids",
                "partner_invoice_id", "partner_supplier_id"):
        assert second[key] == first[key], key
    assert (len(s.invoices), len(s.schedules), len(s.bills), len(s.counterparties)) == (n_inv, n_sched, n_bill, n_cp)
    assert all(i["created"] is False for i in second["invoices"])
    assert s.journal_entries == []
    # A NEW version is a new set of paper (different idempotency key).
    third = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=_payload(version=3),
    )
    assert third["created"] is True
    assert third["deposit_invoice_id"] != first["deposit_invoice_id"]
    assert third["quote_ref"] == "Q-2026-0001 v3"


@pytest.mark.asyncio
async def test_apply_refuses_closed_period_and_unknown_entity():
    s = _SeamStubSession(closed_through=date(2026, 9, 30))
    with pytest.raises(QuoteSeamError) as exc:
        await apply_quote_acceptance(
            s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=_payload(),
        )
    assert exc.value.code == "PERIOD_CLOSED" and exc.value.http_status == 409
    assert s.invoices == {} and s.journal_entries == []

    s = _SeamStubSession()
    with pytest.raises(QuoteSeamError) as exc:
        await apply_quote_acceptance(
            s, workspace_id=WID, entity_id="33333333-3333-4333-8333-333333333333",
            actor_uc_uid="m2m:customer-ops", payload=_payload(),
        )
    assert exc.value.code == "ENTITY_NOT_FOUND" and exc.value.http_status == 404


@pytest.mark.asyncio
async def test_apply_partner_failure_is_isolated_and_retry_completes_it():
    s = _SeamStubSession(fail_bill_insert=True)
    res = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=_payload(),
    )
    # Customer paper is all there; the partner cursor reports the failure.
    assert len(s.invoices) == 9 and len(s.schedules) == 3
    assert res["partner_invoice_id"] is None
    assert res["federated"]["accounting_ops"] == "done"
    assert res["federated"]["partner"] == "failed"
    assert "simulated bill insert failure" in res["federated"]["reason"]
    # Retry (same call) once the cause is fixed: only the bill is created.
    s.fail_bill_insert = False
    again = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=_payload(),
    )
    assert len(s.invoices) == 9 and len(s.bills) == 1
    assert again["federated"]["partner"] == "done"
    assert again["partner_invoice_id"] is not None
    assert again["deposit_invoice_id"] == res["deposit_invoice_id"]


@pytest.mark.asyncio
async def test_apply_partner_supplier_id_must_exist():
    s = _SeamStubSession()
    payload = _payload(delivery_partner={"name": "Synthetic Delivery Partner LLC",
                                         "partner_supplier_id": "44444444-4444-4444-8444-444444444444"})
    res = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops", payload=payload,
    )
    assert res["federated"]["partner"] == "failed"
    assert res["federated"]["reason"].startswith("PARTNER_SUPPLIER_NOT_FOUND")
    assert s.bills == {}


@pytest.mark.asyncio
async def test_apply_without_partner_skips_cleanly():
    s = _SeamStubSession()
    res = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="tester",
        payload=_payload(delivery_partner=None, split_rule=None),
    )
    assert res["partner_invoice_id"] is None
    assert res["federated"] == {"accounting_ops": "done", "partner": "skipped",
                                "reason": "no delivery partner on the quote"}
    res = await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="tester",
        payload=_payload(version=9, split_rule={"kind": "none"}),
    )
    assert res["federated"]["partner"] == "skipped"
    assert res["federated"]["reason"] == "split_rule.kind=none"


# ===========================================================================
# 3. Schedule generation
# ===========================================================================


async def _seed_schedules(s: _SeamStubSession) -> dict[str, Any]:
    return await apply_quote_acceptance(
        s, workspace_id=WID, entity_id=EID, actor_uc_uid="m2m:customer-ops",
        payload=_payload(delivery_partner=None, split_rule=None),
    )


@pytest.mark.asyncio
async def test_schedule_runner_generates_next_period_draft_and_advances():
    s = _SeamStubSession()
    await _seed_schedules(s)
    before = len(s.invoices)
    # Nothing due before the first anniversary.
    res = await generate_due_schedule_invoices(
        s, workspace_id=WID, actor_uc_uid="scheduler", as_of=date(2026, 10, 11),
    )
    assert res["generated"] == [] and len(s.invoices) == before
    # On 2026-10-12 both monthly schedules are due; the annual one is not.
    res = await generate_due_schedule_invoices(
        s, workspace_id=WID, actor_uc_uid="scheduler", as_of=date(2026, 10, 12),
    )
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
    # Re-running for the same day is idempotent.
    res = await generate_due_schedule_invoices(
        s, workspace_id=WID, actor_uc_uid="scheduler", as_of=date(2026, 10, 12),
    )
    assert res["generated"] == [] and len(s.invoices) == before + 2
    assert s.journal_entries == []


@pytest.mark.asyncio
async def test_schedule_runner_catches_up_and_retires_at_term_end():
    s = _SeamStubSession()
    await _seed_schedules(s)
    # Jump two years ahead: monthly (12 periods) and annual (2 periods) all run out.
    res = await generate_due_schedule_invoices(
        s, workspace_id=WID, actor_uc_uid="scheduler", as_of=date(2028, 12, 31),
    )
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
    # Nothing more to do afterwards.
    res = await generate_due_schedule_invoices(
        s, workspace_id=WID, actor_uc_uid="scheduler", as_of=date(2030, 1, 1),
    )
    assert res["schedules_due"] == 0


@pytest.mark.asyncio
async def test_schedule_runner_skips_closed_period_without_advancing():
    s = _SeamStubSession()
    await _seed_schedules(s)
    s.closed_through = date(2026, 10, 31)
    res = await generate_due_schedule_invoices(
        s, workspace_id=WID, actor_uc_uid="scheduler", as_of=date(2026, 10, 12),
    )
    assert res["generated"] == []
    assert len(res["skipped"]) == 2 and "period closed" in res["skipped"][0]["reason"]
    hosting = s.schedules[(WID, "Q-2026-0001 v2", 5)]
    assert hosting["periods_generated"] == 1 and hosting["next_run_at"].date() == date(2026, 10, 12)
    # Once the period reopens the same run generates it.
    s.closed_through = None
    res = await generate_due_schedule_invoices(
        s, workspace_id=WID, actor_uc_uid="scheduler", as_of=date(2026, 10, 12),
    )
    assert len(res["generated"]) == 2


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
