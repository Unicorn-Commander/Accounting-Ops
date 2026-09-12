"""Quotes → money seam (Customer-Ops ``quote.accepted`` → AR drafts + schedules + partner bills).

Contract: ``~/Projects/uc-quotes/QUOTES-CONTRACT-v1.md`` §3 "Accounting-Ops" +
"Partner split", as amended by ``AMENDMENT-01`` (A5 deposit maths, A7 federated
status, A12 partner invoice is a payable, A16 line kinds, A17 currency
exponent), and the PR #13 review (H1–H6, M7–M13, L15–L19). Everything this
module creates is a DRAFT document. Nothing here posts to the journal —
``journal_entry_id`` stays NULL on every invoice and bill — so the hash chain,
the materiality gate and the closed-period lock are never bypassed: a human
issues, and issuing is what posts.

What one accepted quote turns into
==================================
  * the AR customer (a ``counterparty``) upserted from the Contact-Ops identity
    (thin ``contact_id`` reference — Contact-Ops is never copied in);
  * the DEPOSIT invoice draft: Σ over accepted ``one_time`` + ``milestone``
    lines of ``amount_cents × payment_split[0] / 100`` (each line carries its
    own split; default ``[50, 50]``; half-up to the cent);
  * one MILESTONE draft per remaining payment part, memo
    ``"Due on acceptance of <line label>"``;
  * one ``invoice_schedules`` row per ``recurring`` line (cadence
    ``month|quarter|year``, anchored on the acceptance date) plus the FIRST
    period's draft; the runner (``generate_due_schedule_invoices``) creates the
    later periods — customer draft AND, when the line passes to the partner,
    the per-period partner bill;
  * when ``delivery_partner`` + ``split_rule`` are present: a DRAFT partner
    ``bill`` (accounts payable, A12) from the delivery partner to the vendor of
    record — hosting lines to the partner in full when ``hosting_to_partner``
    (regardless of ``kind``), ``percent_net`` of the fixed scope after
    ``direct_costs_total_cents`` (or the flat ``fee_cents``), and — Amendment
    06 / A32 — ``percent_net`` of every NON-hosting recurring line per period,
    net of that period's recurring direct costs
    (``recurring_direct_costs_cents[interval]``), apportioned exactly across
    the lines of that interval; period 1 rides the acceptance bill, later
    periods are billed by the runner;
  * a NEW accepted version of the same quote number voids the prior versions'
    still-unissued drafts (invoices, bills) and retires their schedules before
    creating its own paper; issued/paid paper is left alone and reported.

Idempotency
===========
Keyed by ``quote_ref + version`` (``"Q-2026-0001 v2"``). Every invoice carries
``external_source='customer-ops'`` and a deterministic ``external_ref``
(``<quote_ref>:deposit``, ``<quote_ref>:part:<position>:<k>``,
``<quote_ref>:recurring:<position>:<period>``, bills ``<quote_ref>:partner`` and
``<quote_ref>:partner:recurring:<position>:<period>``) so a replay finds every
row it already created (the 0019 partial unique index is the arbiter) and
creates nothing. Schedules are unique on ``(workspace_id, quote_ref,
line_position)``.

Federated status (A7)
=====================
The result carries ``federated = {accounting_ops: done|failed, partner:
done|failed|skipped, reason}``. The partner step runs in its own savepoint:
a partner failure (an unknown ``partner_supplier_id``, a closed period in the
VENDOR entity's books, …) never loses the customer drafts, and "retry
federation" (the same call) completes only what is missing.

Materiality + closed periods
============================
Drafts post nothing, so the gate is satisfied by construction; the tier for
each draft's total is still computed and surfaced (``materiality_tier``,
``review_required``). A draft whose issue date falls in a CLOSED period of the
AR entity is refused (``PERIOD_CLOSED``); the same check runs for the vendor
entity before the partner bill (degrading to ``partner: failed``). The runner
skips (and reports) periods that fall in a closed window on either side.
"""
from __future__ import annotations

import calendar
import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Literal, Sequence
from uuid import UUID, uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ar.invoicing import (
    InvoiceError,
    _fetch_invoice_row,
    _invoice_view,
    create_invoice_from_draft,
    upsert_ar_customer,
    void_invoice,
)
from app.journal.materiality import materiality_tier
from app.journal.posting import assert_period_open

_log = logging.getLogger("acctops.ar.quotes_seam")

#: ``invoice.external_source`` / ``bill.external_source`` for everything the seam creates.
QUOTES_SEAM_SOURCE = "customer-ops"
#: The ONLY trusted M2M actor (Keycloak ``azp``) the seam endpoint accepts.
QUOTES_SEAM_ACTOR = "customer-ops"
#: Documented default Net-N when neither the payload nor the workspace says.
DEFAULT_NET_TERMS = 30

LineKind = Literal[
    "one_time", "milestone", "recurring", "hourly", "option", "included", "excluded"
]
Cadence = Literal["month", "quarter", "year"]

_MONTHS_PER_PERIOD: dict[str, int] = {"month": 1, "quarter": 3, "year": 12}
_PERIOD_LABEL: dict[str, str] = {"month": "Month", "quarter": "Quarter", "year": "Year"}
_DEFAULT_SPLIT: tuple[int, ...] = (50, 50)

# ISO-4217 minor-unit exponents that are NOT 2. v1 supports 2-decimal
# currencies only (A17) and rejects these with a clear error; anything not
# listed here is treated as exponent 2.
_NON_TWO_EXPONENT: dict[str, int] = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0,
    "KRW": 0, "PYG": 0, "RWF": 0, "UGX": 0, "UYI": 0, "VND": 0, "VUV": 0,
    "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
    "CLF": 4, "UYW": 4,
}


def quotes_enabled() -> bool:
    """The QUOTES_ENABLED fence (config.quotes_enabled). Surfaces check it; the
    service functions do not, so tests and operators can drive them directly."""
    from app.config import get_settings

    return bool(get_settings().quotes_enabled)


class QuoteSeamError(Exception):
    """A clean, caller-surfaceable seam error with a stable ``code``.

    ``http_status`` is the status the HTTP route maps it to (the MCP tool
    surfaces ``code: reason`` as a ValueError).
    """

    def __init__(self, code: str, reason: str, *, http_status: int = 422):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.http_status = http_status


# ---------------------------------------------------------------------------
# Payload (shared by the HTTP route and the MCP tool — one validation path)
# ---------------------------------------------------------------------------


class QuoteCustomer(BaseModel):
    """The relationship's Contact-Ops identity (thin reference; never copied in)."""

    model_config = ConfigDict(extra="ignore")

    display_name: str = Field(min_length=1, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    org: str | None = Field(default=None, max_length=256)
    contact_id: str | None = Field(default=None, max_length=128)
    billing_address: str | None = Field(default=None, max_length=1024)


class QuoteLineMilestone(BaseModel):
    model_config = ConfigDict(extra="ignore")

    payment_split: list[int] | None = None
    acceptance_criteria_md: str | None = None


class QuoteLineRecurring(BaseModel):
    model_config = ConfigDict(extra="ignore")

    interval: Cadence = "month"
    term_months: int | None = Field(default=None, ge=1, le=600)


class QuoteLine(BaseModel):
    """One accepted quote line. Amounts are integer cents, tax EXCLUDED (A9)."""

    model_config = ConfigDict(extra="ignore")

    kind: LineKind
    label: str = Field(min_length=1, max_length=512)
    amount_cents: int = Field(ge=0)
    position: int = Field(ge=0)
    # A5: each line may carry its own split; accepted at the line level or
    # under ``milestone`` (v1 §1 shape). Line level wins when both are given.
    payment_split: list[int] | None = None
    milestone: QuoteLineMilestone | None = None
    recurring: QuoteLineRecurring | None = None
    # Partner split needs to know which recurring line is "hosting". Explicit
    # flag preferred; default = a recurring line whose label mentions hosting.
    is_hosting: bool | None = None
    # Option groups: Customer-Ops only sends selected lines (A5); tolerated here
    # so an unselected line that does arrive is ignored, never invoiced.
    selected: bool | None = None
    option_group: str | None = None
    line_id: str | None = Field(default=None, max_length=128)

    def effective_split(self) -> tuple[int, ...]:
        if self.payment_split:
            return tuple(self.payment_split)
        if self.milestone and self.milestone.payment_split:
            return tuple(self.milestone.payment_split)
        return _DEFAULT_SPLIT

    def hosting(self) -> bool:
        if self.is_hosting is not None:
            return bool(self.is_hosting)
        return self.kind == "recurring" and "hosting" in self.label.lower()


class QuoteTotals(BaseModel):
    """Computed by Customer-Ops' single ``computeQuote`` path (A4). Cross-checked,
    never recomputed differently."""

    model_config = ConfigDict(extra="ignore")

    one_time_total_cents: int = Field(ge=0)
    milestone_total_cents: int = Field(ge=0)
    deposit_cents: int = Field(ge=0)
    recurring: dict[str, int] = Field(default_factory=dict)


class DeliveryPartner(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=256)
    contact_id: str | None = Field(default=None, max_length=128)
    # A12: explicit entity mapping. The AO counterparty id of the partner as a
    # supplier (kind='vendor'). When absent the seam finds-or-creates one from
    # name + contact_id and returns the id to store.
    partner_supplier_id: str | None = Field(default=None, max_length=64)


class SplitRule(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["percent_net", "fee", "none"] = "none"
    percent: Decimal | None = Field(default=None, ge=0, le=100)
    hosting_to_partner: bool = False
    # A3: only the TOTAL travels, never itemised costs. ``direct_costs_cents``
    # (the v1 name) is accepted as an alias.
    direct_costs_total_cents: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("direct_costs_total_cents", "direct_costs_cents"),
    )
    # kind=fee: the flat partner fee (v1 named the kind but no amount field).
    fee_cents: int | None = Field(default=None, ge=0)
    # A32: per-PERIOD totals of the quote_costs of kind ai_usage|third_party that
    # are flagged recurring, keyed by interval ({"month": 5000}); netted against
    # the non-hosting recurring lines of that interval every period. Still only
    # totals (A3) — nothing itemised travels. Alias: ``recurring_direct_costs``.
    recurring_direct_costs_cents: dict[str, int] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("recurring_direct_costs_cents", "recurring_direct_costs"),
    )
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("recurring_direct_costs_cents")
    @classmethod
    def _check_recurring_costs(cls, value: dict[str, int]) -> dict[str, int]:
        out: dict[str, int] = {}
        for k, v in (value or {}).items():
            if k not in _MONTHS_PER_PERIOD:
                raise ValueError(f"recurring_direct_costs_cents: interval must be month|quarter|year, got {k!r}")
            if int(v) < 0:
                raise ValueError("recurring_direct_costs_cents: amounts must be >= 0")
            out[k] = int(v)
        return out


class QuoteAcceptedPayload(BaseModel):
    """The ``quote.accepted`` federation payload (internal notes and the itemised
    costs never travel — A3 / v1 §2.6)."""

    model_config = ConfigDict(extra="ignore")

    quote_ref: str = Field(min_length=1, max_length=120, description="Quote number, e.g. Q-2026-0001")
    version: int = Field(default=1, ge=1, le=9999, description="Accepted version_no")
    customer: QuoteCustomer
    currency: str = Field(default="USD", min_length=3, max_length=3)
    lines: list[QuoteLine] = Field(min_length=1)
    totals: QuoteTotals | None = None
    delivery_partner: DeliveryPartner | None = None
    split_rule: SplitRule | None = None
    # A12: the vendor of record's entity that OWES the partner bill. Defaults
    # to the path entity (the one issuing the customer invoices).
    vendor_entity_id: str | None = Field(default=None, max_length=64)
    # Issue date basis for the drafts (the acceptance date). Default: today.
    accepted_at: date | None = None
    # Net-N for every draft: payload -> workspace default -> 30 (M10).
    net_terms: int | None = Field(default=None, ge=0, le=365)


# ---------------------------------------------------------------------------
# Pure planning (no I/O) — unit-tested directly
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedPart:
    position: int
    label: str
    kind: str
    part_index: int      # 0 = the deposit part
    parts_total: int
    amount_cents: int
    percent: int


@dataclass(frozen=True)
class PlannedDraft:
    position: int
    label: str
    part_index: int
    parts_total: int
    amount_cents: int
    percent: int
    memo: str
    external_suffix: str


@dataclass(frozen=True)
class PlannedSchedule:
    position: int
    label: str
    cadence: str
    amount_cents: int
    term_months: int | None
    periods_total: int | None
    is_hosting: bool
    partner_amount_cents: int = 0        # the partner's share of ONE period (H3 / A32)
    partner_basis: str = ""              # human-readable basis of that share
    direct_cost_share_cents: int = 0     # this line's share of the period's recurring direct costs


@dataclass(frozen=True)
class PartnerLine:
    description: str
    amount_cents: int


@dataclass
class PartnerPlan:
    kind: str
    lines: list[PartnerLine]
    total_cents: int
    basis: str


@dataclass
class BillingPlan:
    quote_ref: str
    quote_number: str
    version: int
    currency: str
    deposit_parts: list[PlannedPart]
    deposit_cents: int
    milestone_drafts: list[PlannedDraft]
    schedules: list[PlannedSchedule]
    partner: PartnerPlan | None
    partner_skipped_reason: str | None
    skipped_lines: list[dict[str, Any]]
    computed_totals: dict[str, Any] = field(default_factory=dict)


def full_quote_ref(quote_ref: str, version: int) -> str:
    """``"Q-2026-0001" + 2 -> "Q-2026-0001 v2"`` (not doubled when already suffixed)."""
    base = quote_ref.strip()
    suffix = f" v{int(version)}"
    if base.endswith(suffix):
        return base
    return f"{base}{suffix}"


def parse_quote_ref(full: str) -> tuple[str, int | None]:
    """``"Q-2026-0001 v2" -> ("Q-2026-0001", 2)``; no suffix -> (full, None)."""
    base, sep, tail = full.rpartition(" v")
    if sep and tail.isdigit():
        return base, int(tail)
    return full, None


def validate_currency(code: str) -> str:
    """v1 supports 2-decimal currencies only (A17). Returns the upper-cased code."""
    cur = (code or "").strip().upper()
    if len(cur) != 3 or not cur.isalpha():
        raise QuoteSeamError("CURRENCY_INVALID", f"currency must be an ISO-4217 code; got {code!r}")
    exp = _NON_TWO_EXPONENT.get(cur, 2)
    if exp != 2:
        raise QuoteSeamError(
            "CURRENCY_UNSUPPORTED",
            f"{cur} has {exp} minor-unit digits; v1 supports 2-decimal currencies only",
        )
    return cur


def _round_half_up_cents(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def fmt_pct(value: Decimal | int | float | str) -> str:
    """Render a percentage for humans: ``50 -> "50"``, ``33.5 -> "33.5"``,
    ``33.333 -> "33.33"`` — never scientific notation (review H1: ``Decimal(50)
    .normalize()`` prints ``5E+1``). Quantised to 2 decimals, trailing zeros
    stripped by hand."""
    q = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    s = f"{q:f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def validate_payment_split(split: Sequence[int], *, label: str) -> tuple[int, ...]:
    parts = tuple(int(p) for p in split)
    if not parts or len(parts) > 12:
        raise QuoteSeamError("INVALID_PAYMENT_SPLIT", f"{label}: payment_split needs 1..12 parts")
    if any(p < 0 for p in parts):
        raise QuoteSeamError("INVALID_PAYMENT_SPLIT", f"{label}: payment_split parts must be >= 0")
    if sum(parts) != 100:
        raise QuoteSeamError(
            "INVALID_PAYMENT_SPLIT", f"{label}: payment_split must sum to 100 (got {sum(parts)})"
        )
    return parts


def split_parts(amount_cents: int, payment_split: Sequence[int], *, label: str = "line") -> list[int]:
    """Split ``amount_cents`` by ``payment_split`` into integer cents that sum exactly.

    Part 0 is ``round_half_up(amount × split[0] / 100)`` — literally the A5
    deposit formula, so our deposit equals Customer-Ops' ``deposit_cents``.
    The remaining parts share the remainder by weight using largest-remainder
    apportionment, so ``sum(parts) == amount_cents`` always and no part is
    negative.
    """
    parts = validate_payment_split(payment_split, label=label)
    amount = int(amount_cents)
    if amount < 0:
        raise QuoteSeamError("INVALID_AMOUNT", f"{label}: amount_cents cannot be negative")
    if len(parts) == 1:
        return [amount]
    part0 = min(amount, _round_half_up_cents(Decimal(amount) * parts[0] / Decimal(100)))
    rest = amount - part0
    weights = parts[1:]
    wsum = sum(weights)
    if wsum == 0:
        out = [0] * len(weights)
        out[-1] += rest
        return [part0] + out
    raw = [Decimal(rest) * w / Decimal(wsum) for w in weights]
    floors = [int(r.to_integral_value(rounding=ROUND_FLOOR)) for r in raw]
    remainder = rest - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return [part0] + floors


def _add_months(anchor: date, months: int) -> date:
    y, m0 = divmod(anchor.month - 1 + months, 12)
    year, month = anchor.year + y, m0 + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def period_date(anchor: date, cadence: str, period_no: int) -> date:
    """Date of period ``period_no`` (1-based) for a schedule anchored on ``anchor``.

    Always computed FROM THE ANCHOR with month-end clamping (review M9), so an
    anchor of Jan 31 gives Feb 28, Mar 31, Apr 30 — never a permanent drift to
    the 28th from a previously clamped date.
    """
    months = _MONTHS_PER_PERIOD.get(cadence)
    if months is None:
        raise QuoteSeamError("INVALID_CADENCE", f"cadence must be month|quarter|year; got {cadence!r}")
    if period_no < 1:
        raise QuoteSeamError("INVALID_PERIOD", f"period_no must be >= 1; got {period_no}")
    return _add_months(anchor, months * (period_no - 1))


def advance_period(d: date, cadence: str) -> date:
    """One period after ``d`` (clamped). Prefer ``period_date`` for schedules."""
    return period_date(d, cadence, 2)


def periods_for_term(term_months: int | None, cadence: str) -> int | None:
    if term_months is None:
        return None
    per = _MONTHS_PER_PERIOD[cadence]
    return max(1, -(-int(term_months) // per))  # ceil


def apportion_cents(total: int, weights: Sequence[int]) -> list[int]:
    """Split ``total`` integer cents across ``weights`` by largest-remainder — exact sum, no negatives."""
    total = int(total)
    if not weights:
        return []
    wsum = sum(int(w) for w in weights)
    if wsum <= 0:
        out = [0] * len(weights)
        out[-1] = total
        return out
    raw = [Decimal(total) * int(w) / Decimal(wsum) for w in weights]
    floors = [int(r.to_integral_value(rounding=ROUND_FLOOR)) for r in raw]
    remainder = total - sum(floors)
    order = sorted(range(len(raw)), key=lambda i: (-(raw[i] - floors[i]), i))
    for i in order[:remainder]:
        floors[i] += 1
    return floors


def apportion_recurring_partner_share(
    schedules: Sequence[PlannedSchedule], rule: SplitRule, currency: str
) -> list[PlannedSchedule]:
    """Set each schedule's per-period partner share (v1 §3 + Amendment 06 / A32).

      hosting lines      -> in full when ``hosting_to_partner`` (else 0);
      non-hosting lines  -> ``percent_net`` of (the interval's non-hosting recurring
                            amount − that interval's recurring direct costs), computed
                            per interval and apportioned exactly across its lines by
                            amount; ``fee`` and ``none`` share nothing recurring.
    """
    out: dict[int, PlannedSchedule] = {}
    pct = Decimal(rule.percent) if (rule.kind == "percent_net" and rule.percent is not None) else None
    for s in schedules:
        if s.is_hosting:
            if rule.hosting_to_partner:
                out[s.position] = replace(s, partner_amount_cents=s.amount_cents,
                                          partner_basis="hosting passed through in full each period")
            else:
                out[s.position] = replace(s, partner_amount_cents=0, partner_basis="hosting stays with the vendor")
        elif pct is None:
            out[s.position] = replace(s, partner_amount_cents=0,
                                      partner_basis=f"no recurring share (split_rule.kind={rule.kind})")
    for cadence in _MONTHS_PER_PERIOD:
        pool = [s for s in schedules if s.cadence == cadence and not s.is_hosting and pct is not None]
        if not pool:
            continue
        amount_c = sum(s.amount_cents for s in pool)
        cost_c = min(int(rule.recurring_direct_costs_cents.get(cadence, 0) or 0), amount_c)
        net_c = amount_c - cost_c
        share_c = _round_half_up_cents(Decimal(net_c) * pct / Decimal(100))
        weights = [s.amount_cents for s in pool]
        shares = apportion_cents(share_c, weights)
        costs = apportion_cents(cost_c, weights)
        for s, sh, co in zip(pool, shares, costs):
            basis = (
                f"{fmt_pct(pct)}% of ({_fmt_money(s.amount_cents, currency)} - recurring direct costs "
                f"{_fmt_money(co, currency)}) per {cadence} = {_fmt_money(sh, currency)}"
            )
            out[s.position] = replace(s, partner_amount_cents=sh, partner_basis=basis, direct_cost_share_cents=co)
    return [out[s.position] for s in schedules]


def _fmt_money(cents: int, currency: str) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    body = f"{whole:,}.{frac:02d}"
    return f"{sign}${body}" if currency == "USD" else f"{sign}{body} {currency}"


def compute_partner_share(
    *,
    split_rule: SplitRule,
    fixed_total_cents: int,
    hosting_lines: Sequence[tuple[str, int]],
    currency: str,
    recurring_shares: Sequence[tuple[str, int, str]] = (),
) -> PartnerPlan | None:
    """The partner's ACCEPTANCE-TIME share per ``split_rule`` (v1 §3 + A32).

      hosting lines     -> to the partner IN FULL when ``hosting_to_partner``
                           (period 1 here; the runner bills later periods) — this
                           is independent of ``kind`` (review H2);
      the fixed scope   -> ``percent_net`` of (one-time + milestone lines) after
                           ``direct_costs_total_cents``; or the flat ``fee_cents``
                           for kind=fee; nothing for kind=none;
      recurring_shares  -> the period-1 ``percent_net`` share of each non-hosting
                           recurring line net of its recurring direct costs
                           (A32; computed by ``apportion_recurring_partner_share``).

    Returns None when the rule yields nothing to bill — the caller records
    ``partner: skipped`` with the reason.
    """
    lines: list[PartnerLine] = []
    basis_parts: list[str] = []
    base = max(0, int(fixed_total_cents) - int(split_rule.direct_costs_total_cents))
    if split_rule.kind == "percent_net":
        if split_rule.percent is None:
            raise QuoteSeamError("PARTNER_RULE_INVALID", "split_rule.kind=percent_net requires percent")
        pct = Decimal(split_rule.percent)
        share = _round_half_up_cents(Decimal(base) * pct / Decimal(100))
        basis = (
            f"{fmt_pct(pct)}% of net fixed scope: {_fmt_money(fixed_total_cents, currency)} "
            f"- direct costs {_fmt_money(split_rule.direct_costs_total_cents, currency)} "
            f"= {_fmt_money(base, currency)}"
        )
        basis_parts.append(basis)
        if share > 0:
            lines.append(PartnerLine(f"Delivery partner share — {basis}", share))
    elif split_rule.kind == "fee":
        if split_rule.fee_cents is None:
            raise QuoteSeamError("PARTNER_RULE_INVALID", "split_rule.kind=fee requires fee_cents")
        basis = f"flat partner fee {_fmt_money(split_rule.fee_cents, currency)}"
        basis_parts.append(basis)
        if split_rule.fee_cents > 0:
            lines.append(PartnerLine(f"Delivery partner fee — {basis}", int(split_rule.fee_cents)))
    elif split_rule.kind == "none":
        basis_parts.append("no share of the fixed scope (kind=none)")
    else:  # pragma: no cover - pydantic Literal already restricts this
        raise QuoteSeamError("PARTNER_RULE_INVALID", f"unknown split_rule.kind {split_rule.kind!r}")

    # Hosting pass-through is a switch of its own — it applies for every kind.
    if split_rule.hosting_to_partner:
        for label, cents in hosting_lines:
            if cents > 0:
                lines.append(PartnerLine(f"Hosting passed through in full — {label} (period 1)", int(cents)))
        basis_parts.append("hosting to partner in full")
    # A32: non-hosting recurring lines share per period; period 1 rides this bill.
    for label, cents, basis in recurring_shares:
        if cents > 0:
            lines.append(PartnerLine(f"Delivery partner share (period 1) — {label}: {basis}", int(cents)))
    if any(cents > 0 for _, cents, _ in recurring_shares):
        basis_parts.append("recurring lines shared per period net of recurring direct costs")
    total = sum(pl.amount_cents for pl in lines)
    if total <= 0:
        return None
    return PartnerPlan(kind=split_rule.kind, lines=lines, total_cents=total, basis="; ".join(basis_parts))


def plan_quote_billing(payload: QuoteAcceptedPayload) -> BillingPlan:
    """Turn an accepted quote into the set of drafts/schedules to create. Pure."""
    currency = validate_currency(payload.currency)
    fq = full_quote_ref(payload.quote_ref, payload.version)
    number, _ = parse_quote_ref(fq)

    deposit_parts: list[PlannedPart] = []
    milestone_drafts: list[PlannedDraft] = []
    schedules: list[PlannedSchedule] = []
    skipped: list[dict[str, Any]] = []
    one_time_total = 0
    milestone_total = 0
    recurring_totals: dict[str, int] = {}
    seen_positions: set[int] = set()

    for line in sorted(payload.lines, key=lambda ln: ln.position):
        if line.position in seen_positions:
            raise QuoteSeamError("DUPLICATE_LINE_POSITION", f"line position {line.position} appears twice")
        seen_positions.add(line.position)
        kind = line.kind
        if line.selected is False:
            skipped.append({"position": line.position, "label": line.label, "reason": "unselected option line"})
            continue
        if kind in ("included", "excluded"):
            skipped.append({"position": line.position, "label": line.label, "reason": f"{kind} lines never invoice (A16)"})
            continue
        if kind == "hourly":
            skipped.append({"position": line.position, "label": line.label, "reason": "hourly rate card: billed as work is logged, not on acceptance"})
            continue
        if kind == "option":
            kind = "one_time"  # a selected package is a one-time deliverable
        if kind in ("one_time", "milestone"):
            if line.amount_cents == 0:
                skipped.append({"position": line.position, "label": line.label, "reason": "zero amount"})
                continue
            if kind == "one_time":
                one_time_total += line.amount_cents
            else:
                milestone_total += line.amount_cents
            split = line.effective_split()
            parts = split_parts(line.amount_cents, split, label=line.label)
            n = len(parts)
            if parts[0] > 0:
                deposit_parts.append(PlannedPart(
                    position=line.position, label=line.label, kind=kind,
                    part_index=0, parts_total=n, amount_cents=parts[0], percent=split[0],
                ))
            for k in range(1, n):
                if parts[k] <= 0:
                    continue
                memo = f"Due on acceptance of {line.label}"
                if n > 2:
                    memo += f" (part {k + 1} of {n})"
                milestone_drafts.append(PlannedDraft(
                    position=line.position, label=line.label, part_index=k, parts_total=n,
                    amount_cents=parts[k], percent=split[k], memo=memo,
                    external_suffix=f"part:{line.position}:{k}",
                ))
            continue
        if kind == "recurring":
            if line.amount_cents == 0:
                skipped.append({"position": line.position, "label": line.label, "reason": "zero amount"})
                continue
            rec = line.recurring or QuoteLineRecurring()
            cadence = rec.interval
            recurring_totals[cadence] = recurring_totals.get(cadence, 0) + line.amount_cents
            schedules.append(PlannedSchedule(
                position=line.position, label=line.label, cadence=cadence,
                amount_cents=line.amount_cents, term_months=rec.term_months,
                periods_total=periods_for_term(rec.term_months, cadence),
                is_hosting=line.hosting(),
            ))
            continue
        raise QuoteSeamError("UNSUPPORTED_LINE_KIND", f"line {line.position}: kind {line.kind!r}")  # pragma: no cover

    deposit_cents = sum(p.amount_cents for p in deposit_parts)
    computed = {
        "one_time_total_cents": one_time_total,
        "milestone_total_cents": milestone_total,
        "deposit_cents": deposit_cents,
        "recurring": {k: recurring_totals[k] for k in ("month", "quarter", "year") if k in recurring_totals},
    }

    # A4: Customer-Ops' totals are the truth; we cross-check, never recompute differently.
    if payload.totals is not None:
        diffs: list[str] = []
        for key in ("one_time_total_cents", "milestone_total_cents", "deposit_cents"):
            theirs, ours = getattr(payload.totals, key), computed[key]
            if int(theirs) != ours:
                diffs.append(f"{key}: customer-ops={theirs} accounting-ops={ours}")
        for cad in ("month", "quarter", "year"):
            theirs = int(payload.totals.recurring.get(cad, 0) or 0)
            ours = computed["recurring"].get(cad, 0)
            if theirs != ours:
                diffs.append(f"recurring.{cad}: customer-ops={theirs} accounting-ops={ours}")
        if diffs:
            raise QuoteSeamError("TOTALS_MISMATCH", "; ".join(diffs))

    if not deposit_parts and not milestone_drafts and not schedules:
        raise QuoteSeamError("NOTHING_TO_INVOICE", "no billable lines (one_time / milestone / recurring with amount > 0)")

    partner: PartnerPlan | None = None
    partner_skipped: str | None = None
    rule = payload.split_rule
    if payload.delivery_partner is None and rule is None:
        partner_skipped = "no delivery partner on the quote"
    elif payload.delivery_partner is None:
        partner_skipped = "split_rule present but no delivery_partner"
    elif rule is None:
        partner_skipped = "delivery_partner present but no split_rule"
    else:
        # L16: hosting_to_partner with recurring lines but none identifiable as
        # hosting is almost certainly a missing is_hosting flag — refuse loudly
        # rather than silently passing nothing through.
        if rule.hosting_to_partner and schedules and not any(s.is_hosting for s in schedules):
            raise QuoteSeamError(
                "HOSTING_LINE_MISSING",
                "split_rule.hosting_to_partner is set but no recurring line is flagged is_hosting "
                "(or labelled 'hosting')",
            )
        schedules = apportion_recurring_partner_share(schedules, rule, currency)
        hosting_lines = [(s.label, s.amount_cents) for s in schedules if s.is_hosting]
        recurring_shares = [
            (s.label, s.partner_amount_cents, s.partner_basis)
            for s in schedules if not s.is_hosting and s.partner_amount_cents > 0
        ]
        partner = compute_partner_share(
            split_rule=rule,
            fixed_total_cents=one_time_total + milestone_total,
            hosting_lines=hosting_lines,
            currency=currency,
            recurring_shares=recurring_shares,
        )
        if partner is None:
            partner_skipped = (
                "split_rule.kind=none and hosting stays with the vendor" if rule.kind == "none"
                else "computed partner share is zero"
            )

    return BillingPlan(
        quote_ref=fq,
        quote_number=number,
        version=int(payload.version),
        currency=currency,
        deposit_parts=deposit_parts,
        deposit_cents=deposit_cents,
        milestone_drafts=milestone_drafts,
        schedules=schedules,
        partner=partner,
        partner_skipped_reason=partner_skipped,
        skipped_lines=skipped,
        computed_totals=computed,
    )


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _cents_to_amount(cents: int) -> Decimal:
    return (Decimal(int(cents)) / Decimal(100)).quantize(Decimal("0.01"))


def _amount_to_cents(value: Any) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return list(json.loads(value))
        except ValueError:
            return []
    return list(value)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return dict(json.loads(value))
        except ValueError:
            return {}
    return dict(value)


def _utc_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _assert_entity(session: AsyncSession, *, workspace_id: str, entity_id: str) -> None:
    try:
        UUID(str(entity_id))
    except ValueError as exc:
        raise QuoteSeamError("ENTITY_NOT_FOUND", f"entity_id {entity_id!r} is not a UUID", http_status=404) from exc
    row = (
        await session.execute(
            text("SELECT 1 FROM entity WHERE id = :eid AND workspace_id = :wid LIMIT 1"),
            {"eid": str(entity_id), "wid": str(workspace_id)},
        )
    ).first()
    if row is None:
        raise QuoteSeamError("ENTITY_NOT_FOUND", f"entity {entity_id} not found in this workspace", http_status=404)


async def _assert_period_open_or_raise(
    session: AsyncSession, *, entity_id: str, effective_date: date, what: str
) -> None:
    closed_end = await assert_period_open(session, entity_id=entity_id, effective_date=effective_date)
    if closed_end is not None:
        raise QuoteSeamError(
            "PERIOD_CLOSED",
            f"{what}: issue date {effective_date.isoformat()} falls in a closed period of entity "
            f"{entity_id} (closed through {closed_end}); refusing to create paper dated into closed books",
            http_status=409,
        )


async def resolve_net_terms(session: AsyncSession, *, workspace_id: str, requested: int | None) -> int:
    """Net-N resolution (review M10): payload -> workspace default -> 30."""
    if requested is not None:
        return int(requested)
    row = (
        await session.execute(
            text("SELECT invoice_net_terms FROM engagement_policy WHERE workspace_id = :wid LIMIT 1"),
            {"wid": str(workspace_id)},
        )
    ).mappings().first()
    if row is not None and row.get("invoice_net_terms") is not None:
        return max(0, int(row["invoice_net_terms"]))
    return DEFAULT_NET_TERMS


async def _merge_counterparty_refs(session: AsyncSession, *, counterparty_id: str, refs: dict[str, Any]) -> None:
    refs = {k: v for k, v in refs.items() if v is not None}
    if not refs:
        return
    await session.execute(
        text(
            "UPDATE counterparty SET external_refs = external_refs || CAST(:refs AS jsonb) "
            "WHERE id = :id"
        ),
        {"refs": json.dumps(refs), "id": counterparty_id},
    )


def _summarize_invoice(view: dict[str, Any], *, kind: str, memo: str | None, created: bool) -> dict[str, Any]:
    total_cents = _amount_to_cents(view.get("total") or 0)
    return {
        "id": str(view["id"]),
        "kind": kind,
        "invoice_number": view.get("invoice_number"),
        "status": view.get("status"),
        "external_ref": view.get("external_ref"),
        "quote_ref": view.get("quote_ref"),
        "issue_date": str(view.get("issue_date")) if view.get("issue_date") is not None else None,
        "due_date": str(view.get("due_date")) if view.get("due_date") is not None else None,
        "total_cents": total_cents,
        "materiality_tier": materiality_tier(total_cents),
        "journal_entry_id": view.get("journal_entry_id"),
        "memo": memo,
        "created": created,
    }


async def _ensure_draft(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    actor_uc_uid: str,
    external_ref: str,
    customer: dict[str, Any],
    line_items: list[dict[str, Any]],
    currency: str,
    issue_date: date,
    net_terms: int,
    notes: str,
    quote_ref: str,
) -> tuple[dict[str, Any], bool]:
    """Find-or-create ONE draft invoice keyed by ``external_ref``. Never posts.

    ``created`` is decided by a pre-insert probe AND the row's ``created_at``
    (review L15): a concurrent replay that lost the unique-index race gets the
    existing row back from the primitive and must report ``created=False``.
    """
    existing = await _fetch_invoice_row(
        session, workspace_id=workspace_id, external_source=QUOTES_SEAM_SOURCE, external_ref=external_ref,
    )
    if existing is not None:
        return await _invoice_view(session, invoice_id=str(existing["id"])), False
    t0 = datetime.now(tz=timezone.utc) - timedelta(seconds=2)
    view = await create_invoice_from_draft(
        session,
        workspace_id=workspace_id,
        entity_id=entity_id,
        actor_uc_uid=actor_uc_uid,
        external_source=QUOTES_SEAM_SOURCE,
        external_ref=external_ref,
        customer=customer,
        line_items=line_items,
        currency=currency,
        issue_date=issue_date,
        net_terms=net_terms,
        notes=notes,
        auto_send=False,
        post_journal=False,
        quote_ref=quote_ref,
    )
    created_at = _as_datetime(view.get("created_at"))
    created = True if created_at is None else created_at >= t0
    return view, created


async def _ensure_supplier(session: AsyncSession, *, workspace_id: str, partner: DeliveryPartner) -> tuple[str, bool]:
    """Resolve the delivery partner to a supplier counterparty (kind='vendor')."""
    if partner.partner_supplier_id:
        try:
            UUID(str(partner.partner_supplier_id))
        except ValueError as exc:
            raise QuoteSeamError(
                "PARTNER_SUPPLIER_NOT_FOUND", f"partner_supplier_id {partner.partner_supplier_id!r} is not a UUID",
            ) from exc
        row = (
            await session.execute(
                text("SELECT id::text AS id, display_name, kind FROM counterparty WHERE id = :id LIMIT 1"),
                {"id": str(partner.partner_supplier_id)},
            )
        ).mappings().first()
        if row is None:
            raise QuoteSeamError(
                "PARTNER_SUPPLIER_NOT_FOUND",
                f"partner_supplier_id {partner.partner_supplier_id} is not a counterparty in this workspace",
            )
        if str(row.get("kind")) != "vendor":  # review M7: never make a customer payable
            raise QuoteSeamError(
                "PARTNER_SUPPLIER_NOT_VENDOR",
                f"partner_supplier_id {partner.partner_supplier_id} is a {row.get('kind')!r} counterparty, "
                "not a supplier (kind='vendor')",
            )
        return row["id"], False

    existing = None
    if partner.contact_id:
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id FROM counterparty "
                    "WHERE kind = 'vendor' AND external_refs->>'contact_id' = :cid LIMIT 1"
                ),
                {"cid": str(partner.contact_id)},
            )
        ).mappings().first()
    if existing is None:
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id FROM counterparty "
                    "WHERE kind = 'vendor' AND lower(display_name) = lower(:name) LIMIT 1"
                ),
                {"name": partner.name.strip()},
            )
        ).mappings().first()
    refs: dict[str, Any] = {"is_supplier": True, "delivery_partner": True}
    if partner.contact_id:
        refs["contact_id"] = str(partner.contact_id)
    if existing is not None:
        await _merge_counterparty_refs(session, counterparty_id=existing["id"], refs=refs)
        return existing["id"], False
    new_id = str(uuid4())
    await session.execute(
        text(
            "INSERT INTO counterparty (id, workspace_id, display_name, kind, external_refs) "
            "VALUES (:id, :wid, :name, 'vendor', CAST(:refs AS jsonb))"
        ),
        {"id": new_id, "wid": workspace_id, "name": partner.name.strip(), "refs": json.dumps(refs)},
    )
    return new_id, True


async def _next_bill_number(session: AsyncSession, *, entity_id: str, issue_date: date) -> str:
    prefix = f"BILL-{issue_date.year}-"
    row = (
        await session.execute(
            text("SELECT count(*) AS n FROM bill WHERE entity_id = :eid AND bill_number LIKE :pfx"),
            {"eid": entity_id, "pfx": f"{prefix}%"},
        )
    ).mappings().first()
    seq = int(row["n"]) + 1 if row else 1
    return f"{prefix}{seq:04d}"


async def _fetch_bill(session: AsyncSession, *, workspace_id: str, external_ref: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT id::text AS id, bill_number, status, total, lines, notes, issue_date, due_date "
                "FROM bill WHERE workspace_id = :wid AND external_source = :src AND external_ref = :ref LIMIT 1"
            ),
            {"wid": workspace_id, "src": QUOTES_SEAM_SOURCE, "ref": external_ref},
        )
    ).mappings().first()
    return dict(row) if row else None


async def _ensure_bill_draft(
    session: AsyncSession,
    *,
    workspace_id: str,
    vendor_entity_id: str,
    supplier_id: str,
    actor_uc_uid: str,
    external_ref: str,
    quote_ref: str,
    currency: str,
    issue_date: date,
    net_terms: int,
    lines: Sequence[PartnerLine],
    notes: str,
) -> tuple[dict[str, Any], bool]:
    """Find-or-create ONE draft AP bill keyed by ``external_ref``. Never posts.

    The vendor entity's closed-period lock applies (review H4). A bill_number
    race trips ``uq_bill_entity_number`` (review M8): the savepoint rolls
    back, the idempotency key is re-checked, otherwise the error propagates to
    the partner step's savepoint (``partner: failed``, retry mints the next
    number).
    """
    existing = await _fetch_bill(session, workspace_id=workspace_id, external_ref=external_ref)
    if existing is not None:
        return existing, False
    await _assert_period_open_or_raise(
        session, entity_id=vendor_entity_id, effective_date=issue_date, what="partner bill (vendor entity)",
    )
    total_cents = sum(pl.amount_cents for pl in lines)
    if total_cents <= 0:
        raise QuoteSeamError("PARTNER_RULE_INVALID", "partner bill total must be positive")
    total = _cents_to_amount(total_cents)
    bill_id = str(uuid4())
    line_rows = [
        {
            "line_number": i,
            "description": pl.description[:512],
            "quantity": "1",
            "unit_price": str(_cents_to_amount(pl.amount_cents)),
            "amount": str(_cents_to_amount(pl.amount_cents)),
        }
        for i, pl in enumerate(lines, start=1)
    ]
    bill_number = await _next_bill_number(session, entity_id=vendor_entity_id, issue_date=issue_date)
    params = {
        "id": bill_id, "wid": workspace_id, "eid": vendor_entity_id, "sup": supplier_id, "num": bill_number,
        "cur": currency, "issue": issue_date, "due": issue_date + timedelta(days=int(net_terms)),
        "subtotal": total, "tax": Decimal("0"), "total": total, "balance": total,
        "lines": json.dumps(line_rows), "notes": notes, "qr": quote_ref,
        "src": QUOTES_SEAM_SOURCE, "ref": external_ref, "uid": actor_uc_uid,
    }
    try:
        async with session.begin_nested():
            await session.execute(
                text(
                    """
                    INSERT INTO bill (
                        id, workspace_id, entity_id, supplier_counterparty_id, bill_number,
                        status, currency, issue_date, due_date,
                        subtotal, tax_total, total, balance_due,
                        lines, notes, quote_ref, external_source, external_ref, created_by_uc_uid
                    ) VALUES (
                        :id, :wid, :eid, :sup, :num,
                        'draft', :cur, :issue, :due,
                        :subtotal, :tax, :total, :balance,
                        CAST(:lines AS jsonb), :notes, :qr, :src, :ref, :uid
                    )
                    """
                ),
                params,
            )
    except IntegrityError:
        existing = await _fetch_bill(session, workspace_id=workspace_id, external_ref=external_ref)
        if existing is not None:
            return existing, False
        raise
    return {
        "id": bill_id, "bill_number": bill_number, "status": "draft", "total": total,
        "lines": line_rows, "notes": notes, "issue_date": issue_date,
        "due_date": params["due"],
    }, True


def _summarize_bill(row: dict[str, Any], *, lines: Sequence[PartnerLine], basis: str,
                    supplier_id: str, vendor_entity_id: str, created: bool) -> dict[str, Any]:
    total_cents = _amount_to_cents(row.get("total") or 0)
    return {
        "id": str(row["id"]),
        "bill_number": row.get("bill_number"),
        "status": row.get("status"),
        "supplier_counterparty_id": supplier_id,
        "vendor_entity_id": vendor_entity_id,
        "issue_date": str(row.get("issue_date")) if row.get("issue_date") is not None else None,
        "total_cents": total_cents,
        "materiality_tier": materiality_tier(total_cents),
        "lines": [asdict(pl) for pl in lines],
        "basis": basis,
        "created": created,
    }


# ---------------------------------------------------------------------------
# Superseding an earlier accepted version (review M11 as directed)
# ---------------------------------------------------------------------------


async def _supersede_prior_versions(
    session: AsyncSession,
    *,
    workspace_id: str,
    quote_number: str,
    new_version: int,
    new_ref: str,
    actor_uc_uid: str,
) -> dict[str, Any]:
    """Void the still-unissued paper of every LOWER version of this quote number.

    Draft invoices with no journal entry are voided (``void_invoice`` finds no
    issue rows for a never-posted draft, so it only flips the status — nothing
    reverses, nothing posts). Active schedules are retired. Draft bills are
    voided. Issued / paid paper is never touched — it is reported in
    ``left_open_invoice_ids`` for a human. Versions >= the incoming one are
    left alone (an out-of-order replay of v1 after v2 must not touch v2).
    """
    pfx = _escape_like(quote_number) + " v%"
    reason = f"Superseded by accepted quote {new_ref}"
    out: dict[str, Any] = {
        "versions": [], "voided_invoice_ids": [], "left_open_invoice_ids": [],
        "retired_schedule_ids": [], "voided_bill_ids": [],
    }
    versions: set[int] = set()

    inv_rows = (
        await session.execute(
            text(
                "SELECT id::text AS id, quote_ref, status, journal_entry_id::text AS journal_entry_id "
                "FROM invoice WHERE workspace_id = :wid AND external_source = :src "
                "AND quote_ref LIKE :pfx ESCAPE '\\' ORDER BY invoice_number"
            ),
            {"wid": workspace_id, "src": QUOTES_SEAM_SOURCE, "pfx": pfx},
        )
    ).mappings().all()
    for r in inv_rows:
        number, ver = parse_quote_ref(str(r["quote_ref"] or ""))
        if number != quote_number or ver is None or ver >= new_version:
            continue
        versions.add(ver)
        if r["status"] == "draft" and r["journal_entry_id"] is None:
            await void_invoice(
                session, workspace_id=workspace_id, invoice_id=r["id"], actor_uc_uid=actor_uc_uid, reason=reason,
            )
            out["voided_invoice_ids"].append(r["id"])
        elif r["status"] != "void":
            out["left_open_invoice_ids"].append(r["id"])

    sched_rows = (
        await session.execute(
            text(
                "SELECT id::text AS id, quote_ref FROM invoice_schedules "
                "WHERE workspace_id = :wid AND quote_ref LIKE :pfx ESCAPE '\\' AND active"
            ),
            {"wid": workspace_id, "pfx": pfx},
        )
    ).mappings().all()
    for r in sched_rows:
        number, ver = parse_quote_ref(str(r["quote_ref"] or ""))
        if number != quote_number or ver is None or ver >= new_version:
            continue
        versions.add(ver)
        await session.execute(
            text("UPDATE invoice_schedules SET active = false, next_run_at = NULL WHERE id = :id"),
            {"id": r["id"]},
        )
        out["retired_schedule_ids"].append(r["id"])

    bill_rows = (
        await session.execute(
            text(
                "SELECT id::text AS id, quote_ref FROM bill "
                "WHERE workspace_id = :wid AND external_source = :src AND quote_ref LIKE :pfx ESCAPE '\\' "
                "AND status = 'draft' AND journal_entry_id IS NULL"
            ),
            {"wid": workspace_id, "src": QUOTES_SEAM_SOURCE, "pfx": pfx},
        )
    ).mappings().all()
    for r in bill_rows:
        number, ver = parse_quote_ref(str(r["quote_ref"] or ""))
        if number != quote_number or ver is None or ver >= new_version:
            continue
        versions.add(ver)
        await session.execute(
            text(
                "UPDATE bill SET status = 'void', voided_at = now(), void_reason = :reason WHERE id = :id"
            ),
            {"reason": reason, "id": r["id"]},
        )
        out["voided_bill_ids"].append(r["id"])

    out["versions"] = sorted(versions)
    return out


# ---------------------------------------------------------------------------
# apply_quote_acceptance — the seam
# ---------------------------------------------------------------------------


async def apply_quote_acceptance(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str,
    actor_uc_uid: str,
    payload: QuoteAcceptedPayload,
    today: date | None = None,
) -> dict[str, Any]:
    """Create every DRAFT the accepted quote implies. Idempotent by quote_ref + version.

    Runs inside the caller's ``workspace_session`` (one transaction): the
    customer drafts + schedules commit together; the partner bill runs in a
    savepoint and reports ``federated.partner`` independently (A7).
    """
    plan = plan_quote_billing(payload)
    fq = plan.quote_ref
    today = today or datetime.now(tz=timezone.utc).date()
    issue_date = payload.accepted_at or today
    currency = plan.currency

    workspace_id = str(workspace_id)
    entity_id = str(entity_id)
    await _assert_entity(session, workspace_id=workspace_id, entity_id=entity_id)
    vendor_entity_id = str(payload.vendor_entity_id or entity_id)
    if vendor_entity_id != entity_id:
        await _assert_entity(session, workspace_id=workspace_id, entity_id=vendor_entity_id)
    net_terms = await resolve_net_terms(session, workspace_id=workspace_id, requested=payload.net_terms)

    await _assert_period_open_or_raise(
        session, entity_id=entity_id, effective_date=issue_date, what="customer drafts",
    )

    # ---- A new version voids the prior versions' unissued paper first ----
    superseded = await _supersede_prior_versions(
        session, workspace_id=workspace_id, quote_number=plan.quote_number,
        new_version=plan.version, new_ref=fq, actor_uc_uid=actor_uc_uid,
    )

    # ---- AR customer (thin Contact-Ops reference) ----
    cust_in = payload.customer
    bill_to = (cust_in.org or cust_in.display_name).strip()
    cust = await upsert_ar_customer(
        session,
        workspace_id=workspace_id,
        name=bill_to,
        contact_id=cust_in.contact_id,
        email=cust_in.email,
        billing_address=cust_in.billing_address,
    )
    ar_customer_id = cust["customer_id"]
    await _merge_counterparty_refs(
        session,
        counterparty_id=ar_customer_id,
        refs={"contact_name": cust_in.display_name if cust_in.org else None, "org": cust_in.org},
    )
    customer_dict = {
        "name": bill_to,
        "contact_id": cust_in.contact_id,
        "email": cust_in.email,
        "billing_address": cust_in.billing_address,
    }
    tax_note = "Amounts exclude tax; tax is applied on the invoice."

    invoices: list[dict[str, Any]] = []
    created_any = False

    # ---- Deposit draft ----
    deposit_invoice_id: str | None = None
    if plan.deposit_cents > 0:
        line_items = [
            {
                "description": f"Deposit ({p.percent}%) — {p.label}"[:512],
                "quantity": 1,
                "unit_price": str(_cents_to_amount(p.amount_cents)),
                "amount": str(_cents_to_amount(p.amount_cents)),
                "source_ref": f"{fq}:line:{p.position}",
            }
            for p in plan.deposit_parts
        ]
        view, created = await _ensure_draft(
            session,
            workspace_id=workspace_id, entity_id=entity_id, actor_uc_uid=actor_uc_uid,
            external_ref=f"{fq}:deposit", customer=customer_dict, line_items=line_items,
            currency=currency, issue_date=issue_date, net_terms=net_terms,
            notes=f"Deposit due on acceptance of quote {fq}. {tax_note}", quote_ref=fq,
        )
        created_any |= created
        deposit_invoice_id = str(view["id"])
        invoices.append(_summarize_invoice(view, kind="deposit", memo="Due on acceptance of the quote", created=created))

    # ---- Milestone drafts (remaining parts) ----
    milestone_invoice_ids: list[str] = []
    for d in plan.milestone_drafts:
        line_items = [{
            "description": f"{d.label} — {d.percent}% ({d.memo})"[:512],
            "quantity": 1,
            "unit_price": str(_cents_to_amount(d.amount_cents)),
            "amount": str(_cents_to_amount(d.amount_cents)),
            "source_ref": f"{fq}:line:{d.position}",
        }]
        view, created = await _ensure_draft(
            session,
            workspace_id=workspace_id, entity_id=entity_id, actor_uc_uid=actor_uc_uid,
            external_ref=f"{fq}:{d.external_suffix}", customer=customer_dict, line_items=line_items,
            currency=currency, issue_date=issue_date, net_terms=net_terms,
            notes=f"{d.memo}. Quote {fq}. {tax_note}", quote_ref=fq,
        )
        created_any |= created
        milestone_invoice_ids.append(str(view["id"]))
        invoices.append(_summarize_invoice(view, kind="milestone", memo=d.memo, created=created))

    # ---- Partner supplier (resolved before the schedules so the rows can carry it) ----
    partner_status = "skipped"
    partner_reason = plan.partner_skipped_reason
    partner_bill: dict[str, Any] | None = None
    partner_supplier_id: str | None = None
    partner_name = payload.delivery_partner.name if payload.delivery_partner else None
    if plan.partner is not None and payload.delivery_partner is not None:
        try:
            async with session.begin_nested():
                partner_supplier_id, _ = await _ensure_supplier(
                    session, workspace_id=workspace_id, partner=payload.delivery_partner,
                )
        except Exception as exc:  # noqa: BLE001 - A7: report, never lose the customer drafts
            _log.exception("quotes seam: partner supplier resolution failed for %s", fq)
            partner_status = "failed"
            partner_reason = (
                f"{exc.code}: {exc.reason}" if isinstance(exc, QuoteSeamError)
                else f"{exc.__class__.__name__}: {str(exc)[:400]}"
            )

    # ---- Recurring schedules + first-period drafts ----
    schedule_ids: list[str] = []
    schedules_out: list[dict[str, Any]] = []
    rule = payload.split_rule
    for s in plan.schedules:
        period_label = _PERIOD_LABEL[s.cadence]
        of_n = f" of {s.periods_total}" if s.periods_total else ""
        memo = f"{period_label} 1{of_n} — {s.label}"
        line_items = [{
            "description": f"{s.label} — {period_label.lower()} 1{of_n}"[:512],
            "quantity": 1,
            "unit_price": str(_cents_to_amount(s.amount_cents)),
            "amount": str(_cents_to_amount(s.amount_cents)),
            "source_ref": f"{fq}:line:{s.position}",
        }]
        view, created = await _ensure_draft(
            session,
            workspace_id=workspace_id, entity_id=entity_id, actor_uc_uid=actor_uc_uid,
            external_ref=f"{fq}:recurring:{s.position}:1", customer=customer_dict, line_items=line_items,
            currency=currency, issue_date=issue_date, net_terms=net_terms,
            notes=f"{memo}. Quote {fq}. Recurring {s.cadence}ly. {tax_note}", quote_ref=fq,
        )
        created_any |= created
        first_id = str(view["id"])
        invoices.append(_summarize_invoice(view, kind="recurring", memo=memo, created=created))

        done_after_first = s.periods_total is not None and s.periods_total <= 1
        next_run = None if done_after_first else _utc_start(period_date(issue_date, s.cadence, 2))
        has_partner = bool(partner_supplier_id) and s.partner_amount_cents > 0
        partner_split = None
        if has_partner and rule is not None:
            partner_split = {
                "kind": rule.kind,
                "percent": fmt_pct(rule.percent) if rule.percent is not None else None,
                "hosting_to_partner": rule.hosting_to_partner,
                "is_hosting": s.is_hosting,
                "partner_name": partner_name,
                "basis": s.partner_basis,
                "direct_cost_share_cents": s.direct_cost_share_cents,
            }
        sched_id = str(uuid4())
        await session.execute(
            text(
                """
                INSERT INTO invoice_schedules (
                    id, workspace_id, entity_id, quote_ref, ar_customer_id, line_position,
                    cadence, amount_cents, currency, description, anchor_date, net_terms,
                    term_months, periods_total, periods_generated, next_run_at,
                    generated_invoice_ids, active,
                    partner_supplier_id, vendor_entity_id, partner_amount_cents, partner_split,
                    partner_periods_billed, generated_bill_ids, created_by_uc_uid
                ) VALUES (
                    :id, :wid, :eid, :qr, :cust, :pos,
                    :cad, :amt, :cur, :desc, :anchor, :net,
                    :term, :ptotal, 1, :next,
                    CAST(:ids AS jsonb), :active,
                    :psup, :veid, :pamt, CAST(:psplit AS jsonb),
                    :pbilled, CAST('[]' AS jsonb), :uid
                )
                ON CONFLICT (workspace_id, quote_ref, line_position) DO NOTHING
                """
            ),
            {
                "id": sched_id, "wid": workspace_id, "eid": entity_id, "qr": fq,
                "cust": ar_customer_id, "pos": s.position, "cad": s.cadence,
                "amt": s.amount_cents, "cur": currency, "desc": s.label[:512],
                "anchor": issue_date, "net": net_terms,
                "term": s.term_months, "ptotal": s.periods_total, "next": next_run,
                "ids": json.dumps([first_id]), "active": not done_after_first,
                "psup": partner_supplier_id if has_partner else None,
                "veid": vendor_entity_id if has_partner else None,
                "pamt": s.partner_amount_cents if has_partner else None,
                "psplit": json.dumps(partner_split) if partner_split else None,
                # Period 1's pass-through is part of the acceptance bill below.
                "pbilled": 1 if has_partner else 0,
                "uid": actor_uc_uid,
            },
        )
        row = (
            await session.execute(
                text(
                    "SELECT id::text AS id, cadence, amount_cents, anchor_date, net_terms, periods_total, "
                    "periods_generated, next_run_at, generated_invoice_ids, active, "
                    "partner_supplier_id::text AS partner_supplier_id, partner_amount_cents, "
                    "partner_periods_billed, generated_bill_ids "
                    "FROM invoice_schedules "
                    "WHERE workspace_id = :wid AND quote_ref = :qr AND line_position = :pos LIMIT 1"
                ),
                {"wid": workspace_id, "qr": fq, "pos": s.position},
            )
        ).mappings().first()
        if row is None:  # pragma: no cover - the INSERT above guarantees a row
            raise QuoteSeamError("SCHEDULE_NOT_FOUND", f"schedule for line {s.position} vanished")
        sched_created = str(row["id"]) == sched_id
        created_any |= sched_created
        schedule_ids.append(str(row["id"]))
        nr = _as_datetime(row["next_run_at"])
        schedules_out.append({
            "id": str(row["id"]),
            "position": s.position,
            "label": s.label,
            "cadence": s.cadence,
            "amount_cents": int(row["amount_cents"]),
            "anchor_date": str(row["anchor_date"]),
            "net_terms": int(row["net_terms"]),
            "periods_total": row["periods_total"],
            "periods_generated": int(row["periods_generated"] or 0),
            "next_run_at": nr.isoformat() if nr else None,
            "active": bool(row["active"]),
            "is_hosting": s.is_hosting,
            "partner_supplier_id": row["partner_supplier_id"],
            "partner_amount_cents": int(row["partner_amount_cents"] or 0),
            "partner_periods_billed": int(row["partner_periods_billed"] or 0),
            "first_invoice_id": first_id,
            "created": sched_created,
        })

    # ---- Partner bill (AP draft) — isolated so the customer drafts never depend on it ----
    if plan.partner is not None and payload.delivery_partner is not None and partner_supplier_id:
        try:
            async with session.begin_nested():
                notes = (
                    f"PARTNER SPLIT DRAFT — bill from {partner_name} (delivery partner) to the vendor of "
                    f"record for quote {fq}. Basis: {plan.partner.basis}. Accounts PAYABLE for this entity, "
                    "never AR. Never auto-issued: review, then approve/post through the AP path. "
                    "Later recurring periods are billed by the schedule runner."
                )
                row, created = await _ensure_bill_draft(
                    session,
                    workspace_id=workspace_id, vendor_entity_id=vendor_entity_id,
                    supplier_id=partner_supplier_id, actor_uc_uid=actor_uc_uid,
                    external_ref=f"{fq}:partner", quote_ref=fq, currency=currency,
                    issue_date=issue_date, net_terms=net_terms, lines=plan.partner.lines, notes=notes,
                )
            created_any |= created
            partner_bill = _summarize_bill(
                row, lines=plan.partner.lines, basis=plan.partner.basis,
                supplier_id=partner_supplier_id, vendor_entity_id=vendor_entity_id, created=created,
            )
            partner_status, partner_reason = "done", None
        except Exception as exc:  # noqa: BLE001 - A7: report, never lose the customer drafts
            _log.exception("quotes seam: partner bill failed for %s", fq)
            partner_status = "failed"
            partner_reason = (
                f"{exc.code}: {exc.reason}" if isinstance(exc, QuoteSeamError)
                else f"{exc.__class__.__name__}: {str(exc)[:400]}"
            )

    review_required = any(i["materiality_tier"] == "H" for i in invoices) or (
        partner_bill is not None and partner_bill["materiality_tier"] == "H"
    )
    return {
        "quote_ref": fq,
        "quote_number": plan.quote_number,
        "version": plan.version,
        "created": created_any,
        "replayed": not created_any,
        "ar_customer_id": ar_customer_id,
        "deposit_invoice_id": deposit_invoice_id,
        "milestone_invoice_ids": milestone_invoice_ids,
        "schedule_ids": schedule_ids,
        "partner_invoice_id": partner_bill["id"] if partner_bill else None,
        "partner_supplier_id": partner_supplier_id,
        "vendor_entity_id": vendor_entity_id,
        "entity_id": entity_id,
        "currency": currency,
        "issue_date": issue_date.isoformat(),
        "net_terms": net_terms,
        "invoices": invoices,
        "schedules": schedules_out,
        "partner_bill": partner_bill,
        "superseded": superseded,
        "review_required": review_required,
        "skipped_lines": plan.skipped_lines,
        "totals": plan.computed_totals,
        "federated": {
            "accounting_ops": "done",
            "partner": partner_status,
            "reason": partner_reason,
        },
    }


# ---------------------------------------------------------------------------
# Schedule runner — one DRAFT per due period (+ the partner bill per period)
# ---------------------------------------------------------------------------


async def generate_due_schedule_invoices(
    session: AsyncSession,
    *,
    workspace_id: str,
    actor_uc_uid: str,
    as_of: date | None = None,
    entity_id: str | None = None,
    limit: int = 200,
    max_periods_per_schedule: int = 36,
) -> dict[str, Any]:
    """Generate the next period's DRAFT for every active schedule due by ``as_of``.

    Two independent cursors per schedule:

      * customer side — ``periods_generated``: one draft per period from the
        anchor date (idempotent on ``<quote_ref>:recurring:<position>:<n>``);
        a schedule that fell behind catches up (bounded); periods inside a
        closed period of the AR entity are skipped and reported and the
        schedule is left untouched;
      * partner side — ``partner_periods_billed``: for a line that passes to
        the partner, one DRAFT bill per generated period (idempotent on
        ``<quote_ref>:partner:recurring:<position>:<n>``), in its own savepoint,
        with the VENDOR entity's closed-period lock; a failure is reported and
        the cursor stays, so the next run catches up (review H3/H4).

    Never posts.
    """
    workspace_id = str(workspace_id)
    as_of = as_of or datetime.now(tz=timezone.utc).date()
    cutoff = datetime.combine(as_of, time.max, tzinfo=timezone.utc)
    where = ["s.workspace_id = :wid", "s.active", "s.next_run_at IS NOT NULL", "s.next_run_at <= :cutoff"]
    params: dict[str, Any] = {"wid": workspace_id, "cutoff": cutoff, "limit": max(1, min(int(limit), 1000))}
    if entity_id:
        where.append("s.entity_id = :eid")
        params["eid"] = str(entity_id)
    rows = (
        await session.execute(
            text(
                f"""
                SELECT s.id::text AS id, s.entity_id::text AS entity_id, s.quote_ref,
                       s.ar_customer_id::text AS ar_customer_id, s.line_position, s.cadence,
                       s.amount_cents, s.currency, s.description, s.anchor_date, s.net_terms,
                       s.periods_total, s.periods_generated, s.next_run_at, s.generated_invoice_ids,
                       s.partner_supplier_id::text AS partner_supplier_id,
                       s.vendor_entity_id::text AS vendor_entity_id,
                       s.partner_amount_cents, s.partner_split, s.partner_periods_billed,
                       s.generated_bill_ids,
                       c.display_name AS customer_name, c.external_refs AS customer_refs
                FROM invoice_schedules s
                JOIN counterparty c ON c.id = s.ar_customer_id
                WHERE {" AND ".join(where)}
                ORDER BY s.next_run_at ASC, s.created_at ASC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    partner_bills: list[dict[str, Any]] = []
    partner_skipped: list[dict[str, Any]] = []
    for r in rows:
        refs = _as_dict(r["customer_refs"])
        customer = {
            "name": r["customer_name"],
            "contact_id": refs.get("contact_id"),
            "email": refs.get("email"),
            "billing_address": refs.get("billing_address"),
        }
        periods_total = r["periods_total"]
        periods_generated = int(r["periods_generated"] or 0)
        ids = [str(x) for x in _as_list(r["generated_invoice_ids"])]
        anchor = _as_date(r["anchor_date"])
        cadence = str(r["cadence"])
        net_terms = int(r["net_terms"] or DEFAULT_NET_TERMS)
        period_label = _PERIOD_LABEL[cadence]
        of_n = f" of {periods_total}" if periods_total else ""
        touched = False

        # ---- customer side ----
        for _ in range(max_periods_per_schedule):
            if periods_total is not None and periods_generated >= int(periods_total):
                break
            period_no = periods_generated + 1
            p_date = period_date(anchor, cadence, period_no)
            if p_date > as_of:
                break
            closed_end = await assert_period_open(session, entity_id=r["entity_id"], effective_date=p_date)
            if closed_end is not None:
                skipped.append({
                    "schedule_id": r["id"], "quote_ref": r["quote_ref"], "period_no": period_no,
                    "period_date": p_date.isoformat(), "reason": f"period closed through {closed_end}",
                })
                break
            memo = f"{period_label} {period_no}{of_n} — {r['description']}"
            try:
                view = await create_invoice_from_draft(
                    session,
                    workspace_id=workspace_id,
                    entity_id=r["entity_id"],
                    actor_uc_uid=actor_uc_uid,
                    external_source=QUOTES_SEAM_SOURCE,
                    external_ref=f"{r['quote_ref']}:recurring:{int(r['line_position'])}:{period_no}",
                    customer=customer,
                    line_items=[{
                        "description": f"{r['description']} — {period_label.lower()} {period_no}{of_n}"[:512],
                        "quantity": 1,
                        "unit_price": str(_cents_to_amount(int(r["amount_cents"]))),
                        "amount": str(_cents_to_amount(int(r["amount_cents"]))),
                        "source_ref": f"{r['quote_ref']}:line:{int(r['line_position'])}",
                    }],
                    currency=str(r["currency"]),
                    issue_date=p_date,
                    net_terms=net_terms,
                    notes=f"{memo}. Quote {r['quote_ref']}. Amounts exclude tax; tax is applied on the invoice.",
                    auto_send=False,
                    post_journal=False,
                    quote_ref=str(r["quote_ref"]),
                )
            except InvoiceError as exc:
                skipped.append({
                    "schedule_id": r["id"], "quote_ref": r["quote_ref"], "period_no": period_no,
                    "period_date": p_date.isoformat(), "reason": f"InvoiceError: {exc}",
                })
                break
            inv_id = str(view["id"])
            if inv_id not in ids:
                ids.append(inv_id)
            periods_generated = period_no
            generated.append({
                "schedule_id": r["id"], "quote_ref": r["quote_ref"], "invoice_id": inv_id,
                "invoice_number": view.get("invoice_number"), "period_no": period_no,
                "period_date": p_date.isoformat(), "amount_cents": int(r["amount_cents"]),
                "materiality_tier": materiality_tier(int(r["amount_cents"])),
            })
            touched = True
        if touched:
            done = periods_total is not None and periods_generated >= int(periods_total)
            await session.execute(
                text(
                    """
                    UPDATE invoice_schedules
                    SET periods_generated = :pgen,
                        generated_invoice_ids = CAST(:ids AS jsonb),
                        next_run_at = :next,
                        active = :active
                    WHERE id = :id
                    """
                ),
                {
                    "pgen": periods_generated,
                    "ids": json.dumps(ids),
                    "next": None if done else _utc_start(period_date(anchor, cadence, periods_generated + 1)),
                    "active": not done,
                    "id": r["id"],
                },
            )

        # ---- partner side (H3): one bill per generated period the partner has not been billed for ----
        partner_amount = int(r["partner_amount_cents"] or 0)
        supplier_id = r["partner_supplier_id"]
        vendor_eid = r["vendor_entity_id"] or r["entity_id"]
        if not supplier_id or partner_amount <= 0:
            continue
        billed = int(r["partner_periods_billed"] or 0)
        bill_ids = [str(x) for x in _as_list(r["generated_bill_ids"])]
        split = _as_dict(r["partner_split"])
        partner_name = split.get("partner_name") or "delivery partner"
        touched_partner = False
        for n in range(billed + 1, periods_generated + 1):
            p_date = period_date(anchor, cadence, n)
            ref = f"{r['quote_ref']}:partner:recurring:{int(r['line_position'])}:{n}"
            when = f"({period_label.lower()} {n}{of_n}, {p_date.isoformat()})"
            if split.get("is_hosting"):
                line_desc = f"Hosting passed through in full — {r['description']} {when}"
            else:
                line_desc = f"Delivery partner share — {r['description']} {when}: {split.get('basis') or ''}".rstrip(": ")
            try:
                async with session.begin_nested():
                    row, created = await _ensure_bill_draft(
                        session,
                        workspace_id=workspace_id, vendor_entity_id=vendor_eid, supplier_id=supplier_id,
                        actor_uc_uid=actor_uc_uid, external_ref=ref, quote_ref=str(r["quote_ref"]),
                        currency=str(r["currency"]), issue_date=p_date, net_terms=net_terms,
                        lines=[PartnerLine(line_desc[:512], partner_amount)],
                        notes=(
                            f"PARTNER SPLIT DRAFT — bill from {partner_name} (delivery partner) to the vendor "
                            f"of record for quote {r['quote_ref']}, {period_label.lower()} {n}{of_n}. "
                            "Accounts PAYABLE for this entity, never AR. Never auto-issued."
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - report; the cursor stays so the next run retries
                _log.exception("quotes seam runner: partner bill failed for %s period %s", r["quote_ref"], n)
                partner_skipped.append({
                    "schedule_id": r["id"], "quote_ref": r["quote_ref"], "period_no": n,
                    "period_date": p_date.isoformat(),
                    "reason": (f"{exc.code}: {exc.reason}" if isinstance(exc, QuoteSeamError)
                               else f"{exc.__class__.__name__}: {str(exc)[:300]}"),
                })
                break
            bid = str(row["id"])
            if bid not in bill_ids:
                bill_ids.append(bid)
            billed = n
            touched_partner = True
            partner_bills.append({
                "schedule_id": r["id"], "quote_ref": r["quote_ref"], "bill_id": bid,
                "bill_number": row.get("bill_number"), "period_no": n, "period_date": p_date.isoformat(),
                "amount_cents": partner_amount, "created": created,
            })
        if touched_partner:
            await session.execute(
                text(
                    "UPDATE invoice_schedules SET partner_periods_billed = :pb, "
                    "generated_bill_ids = CAST(:bids AS jsonb) WHERE id = :id"
                ),
                {"pb": billed, "bids": json.dumps(bill_ids), "id": r["id"]},
            )
    return {
        "as_of": as_of.isoformat(),
        "schedules_due": len(rows),
        "generated": generated,
        "skipped": skipped,
        "partner_bills": partner_bills,
        "partner_skipped": partner_skipped,
    }


async def list_invoice_schedules(
    session: AsyncSession,
    *,
    workspace_id: str,
    entity_id: str | None = None,
    entity_scope: Sequence[UUID] | None = None,
    active_only: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    where = ["s.workspace_id = :wid"]
    params: dict[str, Any] = {"wid": str(workspace_id), "limit": max(1, min(int(limit), 1000))}
    if entity_id:
        where.append("s.entity_id = :eid")
        params["eid"] = str(entity_id)
    if entity_scope is not None:
        where.append("s.entity_id = ANY(:scope)")
        params["scope"] = [str(e) for e in entity_scope]
    if active_only:
        where.append("s.active")
    rows = (
        await session.execute(
            text(
                f"""
                SELECT s.id::text AS id, s.entity_id::text AS entity_id, s.quote_ref,
                       s.ar_customer_id::text AS ar_customer_id, c.display_name AS customer_name,
                       s.line_position, s.cadence, s.amount_cents, s.currency, s.description,
                       s.anchor_date, s.net_terms, s.term_months, s.periods_total, s.periods_generated,
                       s.next_run_at, s.generated_invoice_ids, s.active,
                       s.partner_supplier_id::text AS partner_supplier_id,
                       s.vendor_entity_id::text AS vendor_entity_id,
                       s.partner_amount_cents, s.partner_split, s.partner_periods_billed,
                       s.generated_bill_ids, s.created_at
                FROM invoice_schedules s
                LEFT JOIN counterparty c ON c.id = s.ar_customer_id
                WHERE {" AND ".join(where)}
                ORDER BY s.created_at DESC, s.line_position ASC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()
    out = []
    for r in rows:
        d = dict(r)
        d["generated_invoice_ids"] = [str(x) for x in _as_list(d.get("generated_invoice_ids"))]
        d["generated_bill_ids"] = [str(x) for x in _as_list(d.get("generated_bill_ids"))]
        d["partner_split"] = _as_dict(d.get("partner_split")) or None
        for k in ("next_run_at", "created_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        if d.get("anchor_date") is not None:
            d["anchor_date"] = str(d["anchor_date"])
        d["amount_cents"] = int(d["amount_cents"])
        d["partner_amount_cents"] = int(d["partner_amount_cents"] or 0)
        out.append(d)
    return out


__all__ = (
    "DEFAULT_NET_TERMS",
    "QUOTES_SEAM_ACTOR",
    "QUOTES_SEAM_SOURCE",
    "BillingPlan",
    "DeliveryPartner",
    "PartnerPlan",
    "QuoteAcceptedPayload",
    "QuoteCustomer",
    "QuoteLine",
    "QuoteSeamError",
    "QuoteTotals",
    "SplitRule",
    "advance_period",
    "apply_quote_acceptance",
    "apportion_cents",
    "apportion_recurring_partner_share",
    "compute_partner_share",
    "fmt_pct",
    "full_quote_ref",
    "generate_due_schedule_invoices",
    "list_invoice_schedules",
    "parse_quote_ref",
    "period_date",
    "periods_for_term",
    "plan_quote_billing",
    "quotes_enabled",
    "resolve_net_terms",
    "split_parts",
    "validate_currency",
)
