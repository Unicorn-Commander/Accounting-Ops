"""Quotes → money seam (Customer-Ops ``quote.accepted`` → AR drafts + schedules + partner bill).

Contract: ``~/Projects/uc-quotes/QUOTES-CONTRACT-v1.md`` §3 "Accounting-Ops" +
"Partner split", as amended by ``AMENDMENT-01`` (A5 deposit maths, A7 federated
status, A12 partner invoice is a payable, A16 line kinds, A17 currency
exponent). Everything this module creates is a DRAFT document. Nothing here
posts to the journal — ``journal_entry_id`` stays NULL on every invoice and
bill — so the hash chain, the materiality gate and the closed-period lock are
never bypassed: a human issues, and issuing is what posts.

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
    ``month|quarter|year``) plus the FIRST period's draft; the runner
    (``generate_due_schedule_invoices``) creates the later periods;
  * when ``delivery_partner`` + ``split_rule`` are present: a DRAFT partner
    ``bill`` (accounts payable, A12) from the delivery partner to the vendor of
    record — hosting lines to the partner in full when ``hosting_to_partner``,
    ``percent_net`` of the fixed scope after ``direct_costs_total_cents``.

Idempotency
===========
Keyed by ``quote_ref + version`` (``"Q-2026-0001 v2"``). Every invoice carries
``external_source='customer-ops'`` and a deterministic ``external_ref``
(``<quote_ref>:deposit``, ``<quote_ref>:part:<position>:<k>``,
``<quote_ref>:recurring:<position>:<period>``, the bill ``<quote_ref>:partner``)
so a replay finds every row it already created (the 0019 partial unique
index is the arbiter) and creates nothing. Schedules are unique on
``(workspace_id, quote_ref, line_position)``.

Federated status (A7)
=====================
The result carries ``federated = {accounting_ops: done|failed, partner:
done|failed|skipped, reason}``. The partner step runs in its own savepoint:
a partner failure (e.g. an unknown ``partner_supplier_id``) never loses the
customer drafts, and "retry federation" (the same call) completes only what
is missing.

Materiality + closed periods
============================
Drafts post nothing, so the gate is satisfied by construction; the tier for
each draft's total is still computed and surfaced (``materiality_tier``,
``review_required``) so a $10k deposit is visibly a high-value item. A draft
whose issue date falls in a CLOSED period is refused (``PERIOD_CLOSED``) — a
document dated into closed books would be issued into them later. The
schedule runner skips (and reports) periods that fall in a closed window.
"""
from __future__ import annotations

import calendar
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal
from typing import Any, Literal, Sequence
from uuid import UUID, uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ar.invoicing import (
    InvoiceError,
    _fetch_invoice_row,
    _invoice_view,
    create_invoice_from_draft,
    upsert_ar_customer,
)
from app.journal.materiality import materiality_tier
from app.journal.posting import assert_period_open

_log = logging.getLogger("acctops.ar.quotes_seam")

#: ``invoice.external_source`` / ``bill.external_source`` for everything the seam creates.
QUOTES_SEAM_SOURCE = "customer-ops"
#: The ONLY trusted M2M actor (Keycloak ``azp``) the seam endpoint accepts.
QUOTES_SEAM_ACTOR = "customer-ops"

LineKind = Literal[
    "one_time", "milestone", "recurring", "hourly", "option", "included", "excluded"
]
Cadence = Literal["month", "quarter", "year"]

_MONTHS_PER_PERIOD: dict[str, int] = {"month": 1, "quarter": 3, "year": 12}
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
    # supplier. When absent the seam finds-or-creates a supplier counterparty
    # (kind='vendor') from name + contact_id and returns the id to store.
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
    notes: str | None = Field(default=None, max_length=2000)


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
    net_terms: int = Field(default=30, ge=0, le=365)


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


def advance_period(d: date, cadence: str) -> date:
    """The same day-of-month one period later (clamped to the month's length)."""
    months = _MONTHS_PER_PERIOD.get(cadence)
    if months is None:
        raise QuoteSeamError("INVALID_CADENCE", f"cadence must be month|quarter|year; got {cadence!r}")
    y, m0 = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def periods_for_term(term_months: int | None, cadence: str) -> int | None:
    if term_months is None:
        return None
    per = _MONTHS_PER_PERIOD[cadence]
    return max(1, -(-int(term_months) // per))  # ceil


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
) -> PartnerPlan | None:
    """The partner's share per ``split_rule`` (v1 §3 "Partner split").

      hosting lines  -> to the partner IN FULL when ``hosting_to_partner``
                        (the first period, matching the first-period draft);
      the rest       -> ``percent_net`` of the fixed scope (one-time + milestone
                        lines) after ``direct_costs_total_cents``; or the flat
                        ``fee_cents`` for kind=fee; nothing for kind=none.

    Returns None when the rule yields nothing to bill (kind=none, or a zero
    share) — the caller records ``partner: skipped`` with the reason.
    """
    if split_rule.kind == "none":
        return None
    lines: list[PartnerLine] = []
    base = max(0, int(fixed_total_cents) - int(split_rule.direct_costs_total_cents))
    if split_rule.kind == "percent_net":
        if split_rule.percent is None:
            raise QuoteSeamError("PARTNER_RULE_INVALID", "split_rule.kind=percent_net requires percent")
        pct = Decimal(split_rule.percent)
        share = _round_half_up_cents(Decimal(base) * pct / Decimal(100))
        basis = (
            f"{pct.normalize()}% of net fixed scope: {_fmt_money(fixed_total_cents, currency)} "
            f"- direct costs {_fmt_money(split_rule.direct_costs_total_cents, currency)} "
            f"= {_fmt_money(base, currency)}"
        )
        if share > 0:
            lines.append(PartnerLine(f"Delivery partner share — {basis}", share))
    elif split_rule.kind == "fee":
        if split_rule.fee_cents is None:
            raise QuoteSeamError("PARTNER_RULE_INVALID", "split_rule.kind=fee requires fee_cents")
        basis = f"flat partner fee {_fmt_money(split_rule.fee_cents, currency)}"
        if split_rule.fee_cents > 0:
            lines.append(PartnerLine(f"Delivery partner fee — {basis}", int(split_rule.fee_cents)))
    else:  # pragma: no cover - pydantic Literal already restricts this
        raise QuoteSeamError("PARTNER_RULE_INVALID", f"unknown split_rule.kind {split_rule.kind!r}")

    if split_rule.hosting_to_partner:
        for label, cents in hosting_lines:
            if cents > 0:
                lines.append(PartnerLine(f"Hosting passed through in full — {label} (period 1)", int(cents)))
        basis += "; hosting to partner in full"
    total = sum(pl.amount_cents for pl in lines)
    if total <= 0:
        return None
    return PartnerPlan(kind=split_rule.kind, lines=lines, total_cents=total, basis=basis)


def plan_quote_billing(payload: QuoteAcceptedPayload) -> BillingPlan:
    """Turn an accepted quote into the set of drafts/schedules to create. Pure."""
    currency = validate_currency(payload.currency)
    fq = full_quote_ref(payload.quote_ref, payload.version)

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
    if payload.delivery_partner is None and payload.split_rule is None:
        partner_skipped = "no delivery partner on the quote"
    elif payload.delivery_partner is None:
        partner_skipped = "split_rule present but no delivery_partner"
    elif payload.split_rule is None:
        partner_skipped = "delivery_partner present but no split_rule"
    else:
        hosting_lines = [(s.label, s.amount_cents) for s in schedules if s.is_hosting]
        partner = compute_partner_share(
            split_rule=payload.split_rule,
            fixed_total_cents=one_time_total + milestone_total,
            hosting_lines=hosting_lines,
            currency=currency,
        )
        if partner is None:
            partner_skipped = (
                "split_rule.kind=none" if payload.split_rule.kind == "none"
                else "computed partner share is zero"
            )

    return BillingPlan(
        quote_ref=fq,
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


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        try:
            return list(json.loads(value))
        except ValueError:
            return []
    return list(value)


def _utc_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


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
    """Find-or-create ONE draft invoice keyed by ``external_ref``. Never posts."""
    existing = await _fetch_invoice_row(
        session, workspace_id=workspace_id, external_source=QUOTES_SEAM_SOURCE, external_ref=external_ref,
    )
    if existing is not None:
        return await _invoice_view(session, invoice_id=str(existing["id"])), False
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
    # A concurrent replay may have won the unique-index race; the primitive then
    # returns the existing row, which is what we want either way.
    return view, str(view.get("external_ref")) == external_ref and view.get("journal_entry_id") is None


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
                text("SELECT id::text AS id, display_name FROM counterparty WHERE id = :id LIMIT 1"),
                {"id": str(partner.partner_supplier_id)},
            )
        ).mappings().first()
        if row is None:
            raise QuoteSeamError(
                "PARTNER_SUPPLIER_NOT_FOUND",
                f"partner_supplier_id {partner.partner_supplier_id} is not a counterparty in this workspace",
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
                "SELECT id::text AS id, bill_number, status, total, lines, notes "
                "FROM bill WHERE workspace_id = :wid AND external_source = :src AND external_ref = :ref LIMIT 1"
            ),
            {"wid": workspace_id, "src": QUOTES_SEAM_SOURCE, "ref": external_ref},
        )
    ).mappings().first()
    return dict(row) if row else None


async def _ensure_partner_bill(
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
    partner: PartnerPlan,
    partner_name: str,
) -> tuple[dict[str, Any], bool]:
    """Find-or-create the DRAFT AP bill. Never posts (journal_entry_id NULL)."""
    existing = await _fetch_bill(session, workspace_id=workspace_id, external_ref=external_ref)
    if existing is not None:
        return existing, False
    bill_id = str(uuid4())
    total = _cents_to_amount(partner.total_cents)
    lines = [
        {
            "line_number": i,
            "description": pl.description[:512],
            "quantity": "1",
            "unit_price": str(_cents_to_amount(pl.amount_cents)),
            "amount": str(_cents_to_amount(pl.amount_cents)),
        }
        for i, pl in enumerate(partner.lines, start=1)
    ]
    notes = (
        f"PARTNER SPLIT DRAFT — bill from {partner_name} (delivery partner) to the vendor of record "
        f"for quote {quote_ref}. Basis: {partner.basis}. Accounts PAYABLE for this entity, never AR. "
        "Never auto-issued: review, then approve/post through the AP path."
    )
    bill_number = await _next_bill_number(session, entity_id=vendor_entity_id, issue_date=issue_date)
    params = {
        "id": bill_id,
        "wid": workspace_id,
        "eid": vendor_entity_id,
        "sup": supplier_id,
        "num": bill_number,
        "cur": currency,
        "issue": issue_date,
        "due": issue_date + timedelta(days=int(net_terms)),
        "subtotal": total,
        "tax": Decimal("0"),
        "total": total,
        "balance": total,
        "lines": json.dumps(lines),
        "notes": notes,
        "qr": quote_ref,
        "src": QUOTES_SEAM_SOURCE,
        "ref": external_ref,
        "uid": actor_uc_uid,
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
        "id": bill_id,
        "bill_number": bill_number,
        "status": "draft",
        "total": total,
        "lines": lines,
        "notes": notes,
    }, True


def _summarize_bill(row: dict[str, Any], *, partner: PartnerPlan, supplier_id: str, created: bool) -> dict[str, Any]:
    total_cents = _amount_to_cents(row.get("total") or 0)
    return {
        "id": str(row["id"]),
        "bill_number": row.get("bill_number"),
        "status": row.get("status"),
        "supplier_counterparty_id": supplier_id,
        "total_cents": total_cents,
        "materiality_tier": materiality_tier(total_cents),
        "lines": [asdict(pl) for pl in partner.lines],
        "basis": partner.basis,
        "created": created,
    }


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
    net_terms = int(payload.net_terms)
    currency = plan.currency

    workspace_id = str(workspace_id)
    entity_id = str(entity_id)
    await _assert_entity(session, workspace_id=workspace_id, entity_id=entity_id)
    vendor_entity_id = str(payload.vendor_entity_id or entity_id)
    if vendor_entity_id != entity_id:
        await _assert_entity(session, workspace_id=workspace_id, entity_id=vendor_entity_id)

    closed_end = await assert_period_open(session, entity_id=entity_id, effective_date=issue_date)
    if closed_end is not None:
        raise QuoteSeamError(
            "PERIOD_CLOSED",
            f"issue date {issue_date.isoformat()} falls in a closed period (closed through {closed_end}); "
            "refusing to create drafts dated into closed books",
            http_status=409,
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
            notes=f"Deposit due on acceptance of quote {fq}. Amounts exclude tax; tax is applied on the invoice.",
            quote_ref=fq,
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
            notes=f"{d.memo}. Quote {fq}. Amounts exclude tax; tax is applied on the invoice.",
            quote_ref=fq,
        )
        created_any |= created
        milestone_invoice_ids.append(str(view["id"]))
        invoices.append(_summarize_invoice(view, kind="milestone", memo=d.memo, created=created))

    # ---- Recurring schedules + first-period drafts ----
    schedule_ids: list[str] = []
    schedules_out: list[dict[str, Any]] = []
    for s in plan.schedules:
        period_label = {"month": "Month", "quarter": "Quarter", "year": "Year"}[s.cadence]
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
            notes=f"{memo}. Quote {fq}. Recurring {s.cadence}ly. Amounts exclude tax; tax is applied on the invoice.",
            quote_ref=fq,
        )
        created_any |= created
        first_id = str(view["id"])
        invoices.append(_summarize_invoice(view, kind="recurring", memo=memo, created=created))

        done_after_first = s.periods_total is not None and s.periods_total <= 1
        next_run = None if done_after_first else _utc_start(advance_period(issue_date, s.cadence))
        sched_id = str(uuid4())
        await session.execute(
            text(
                """
                INSERT INTO invoice_schedules (
                    id, workspace_id, entity_id, quote_ref, ar_customer_id, line_position,
                    cadence, amount_cents, currency, description, term_months, periods_total,
                    periods_generated, next_run_at, generated_invoice_ids, active, created_by_uc_uid
                ) VALUES (
                    :id, :wid, :eid, :qr, :cust, :pos,
                    :cad, :amt, :cur, :desc, :term, :ptotal,
                    1, :next, CAST(:ids AS jsonb), :active, :uid
                )
                ON CONFLICT (workspace_id, quote_ref, line_position) DO NOTHING
                """
            ),
            {
                "id": sched_id, "wid": workspace_id, "eid": entity_id, "qr": fq,
                "cust": ar_customer_id, "pos": s.position, "cad": s.cadence,
                "amt": s.amount_cents, "cur": currency, "desc": s.label[:512],
                "term": s.term_months, "ptotal": s.periods_total, "next": next_run,
                "ids": json.dumps([first_id]), "active": not done_after_first, "uid": actor_uc_uid,
            },
        )
        row = (
            await session.execute(
                text(
                    "SELECT id::text AS id, cadence, amount_cents, periods_total, periods_generated, "
                    "next_run_at, generated_invoice_ids, active FROM invoice_schedules "
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
        schedules_out.append({
            "id": str(row["id"]),
            "position": s.position,
            "label": s.label,
            "cadence": s.cadence,
            "amount_cents": int(row["amount_cents"]),
            "periods_total": row["periods_total"],
            "periods_generated": int(row["periods_generated"] or 0),
            "next_run_at": row["next_run_at"].isoformat() if isinstance(row["next_run_at"], datetime) else row["next_run_at"],
            "active": bool(row["active"]),
            "is_hosting": s.is_hosting,
            "first_invoice_id": first_id,
            "created": sched_created,
        })

    # ---- Partner bill (AP draft) — isolated so the customer drafts never depend on it ----
    partner_status = "skipped"
    partner_reason = plan.partner_skipped_reason
    partner_bill: dict[str, Any] | None = None
    partner_supplier_id: str | None = None
    if plan.partner is not None and payload.delivery_partner is not None:
        try:
            async with session.begin_nested():
                partner_supplier_id, _ = await _ensure_supplier(
                    session, workspace_id=workspace_id, partner=payload.delivery_partner,
                )
                row, created = await _ensure_partner_bill(
                    session,
                    workspace_id=workspace_id, vendor_entity_id=vendor_entity_id,
                    supplier_id=partner_supplier_id, actor_uc_uid=actor_uc_uid,
                    external_ref=f"{fq}:partner", quote_ref=fq, currency=currency,
                    issue_date=issue_date, net_terms=net_terms, partner=plan.partner,
                    partner_name=payload.delivery_partner.name,
                )
            created_any |= created
            partner_bill = _summarize_bill(row, partner=plan.partner, supplier_id=partner_supplier_id, created=created)
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
        "invoices": invoices,
        "schedules": schedules_out,
        "partner_bill": partner_bill,
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
# Schedule runner — one DRAFT per due period
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

    A schedule that fell several periods behind catches up (bounded). Each
    period's draft is idempotent on ``<quote_ref>:recurring:<position>:<n>``.
    Periods that fall inside a closed period are skipped and reported (the
    schedule is left untouched so a re-run after the period reopens picks it
    up). Never posts.
    """
    as_of = as_of or datetime.now(tz=timezone.utc).date()
    cutoff = datetime.combine(as_of, time.max, tzinfo=timezone.utc)
    where = ["s.active", "s.next_run_at IS NOT NULL", "s.next_run_at <= :cutoff"]
    params: dict[str, Any] = {"cutoff": cutoff, "limit": max(1, min(int(limit), 1000))}
    if entity_id:
        where.append("s.entity_id = :eid")
        params["eid"] = str(entity_id)
    rows = (
        await session.execute(
            text(
                f"""
                SELECT s.id::text AS id, s.entity_id::text AS entity_id, s.quote_ref,
                       s.ar_customer_id::text AS ar_customer_id, s.line_position, s.cadence,
                       s.amount_cents, s.currency, s.description, s.periods_total,
                       s.periods_generated, s.next_run_at, s.generated_invoice_ids,
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
    for r in rows:
        refs = r["customer_refs"] or {}
        if isinstance(refs, str):
            try:
                refs = json.loads(refs)
            except ValueError:
                refs = {}
        customer = {
            "name": r["customer_name"],
            "contact_id": refs.get("contact_id"),
            "email": refs.get("email"),
            "billing_address": refs.get("billing_address"),
        }
        periods_total = r["periods_total"]
        periods_generated = int(r["periods_generated"] or 0)
        ids = [str(x) for x in _as_list(r["generated_invoice_ids"])]
        next_date = _as_date(r["next_run_at"])
        cadence = str(r["cadence"])
        touched = False
        for _ in range(max_periods_per_schedule):
            if next_date > as_of:
                break
            if periods_total is not None and periods_generated >= int(periods_total):
                break
            closed_end = await assert_period_open(session, entity_id=r["entity_id"], effective_date=next_date)
            if closed_end is not None:
                skipped.append({
                    "schedule_id": r["id"], "quote_ref": r["quote_ref"], "period_date": next_date.isoformat(),
                    "reason": f"period closed through {closed_end}",
                })
                break
            period_no = periods_generated + 1
            period_label = {"month": "Month", "quarter": "Quarter", "year": "Year"}[cadence]
            of_n = f" of {periods_total}" if periods_total else ""
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
                    issue_date=next_date,
                    net_terms=30,
                    notes=f"{memo}. Quote {r['quote_ref']}. Amounts exclude tax; tax is applied on the invoice.",
                    auto_send=False,
                    post_journal=False,
                    quote_ref=str(r["quote_ref"]),
                )
            except InvoiceError as exc:
                skipped.append({
                    "schedule_id": r["id"], "quote_ref": r["quote_ref"], "period_date": next_date.isoformat(),
                    "reason": f"InvoiceError: {exc}",
                })
                break
            inv_id = str(view["id"])
            if inv_id not in ids:
                ids.append(inv_id)
            periods_generated = period_no
            generated.append({
                "schedule_id": r["id"], "quote_ref": r["quote_ref"], "invoice_id": inv_id,
                "invoice_number": view.get("invoice_number"), "period_no": period_no,
                "period_date": next_date.isoformat(), "amount_cents": int(r["amount_cents"]),
                "materiality_tier": materiality_tier(int(r["amount_cents"])),
            })
            next_date = advance_period(next_date, cadence)
            touched = True
        if not touched:
            continue
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
                "next": None if done else _utc_start(next_date),
                "active": not done,
                "id": r["id"],
            },
        )
    return {
        "as_of": as_of.isoformat(),
        "schedules_due": len(rows),
        "generated": generated,
        "skipped": skipped,
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
                       s.term_months, s.periods_total, s.periods_generated, s.next_run_at,
                       s.generated_invoice_ids, s.active, s.created_at
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
        for k in ("next_run_at", "created_at"):
            if isinstance(d.get(k), datetime):
                d[k] = d[k].isoformat()
        d["amount_cents"] = int(d["amount_cents"])
        out.append(d)
    return out


__all__ = (
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
    "compute_partner_share",
    "full_quote_ref",
    "generate_due_schedule_invoices",
    "list_invoice_schedules",
    "periods_for_term",
    "plan_quote_billing",
    "split_parts",
    "validate_currency",
)
