"""Pure subscription detector — no DB, no I/O, fully unit-testable.

Given a flat list of journal-entry dicts, group them by a normalized merchant
key and decide which groups look like *recurring subscriptions* (vs. one-off
purchases or owner-equity movements). The function is intentionally
side-effect free so the whole heuristic can be locked down by unit tests with
synthetic entries (see ``tests/smoke/test_detect_subscriptions.py``).

The DB layer (``app.tools.subscriptions.detect_subscriptions_tool``) pulls a
journal window for one entity and feeds the rows straight into
``detect_subscriptions`` — this module never imports SQLAlchemy or the app.

Heuristic (deliberately conservative — precision over recall):

* Group by ``counterparty_id`` when present (strongest key); otherwise by a
  normalized merchant string derived from ``payee`` (preferred) or, failing
  that, a cleaned ``description`` (strip the bank-statement noise: leading
  CHECKCARD/POS prefixes, ``DES:``/``ID:`` processor tails, trailing dates,
  long digit runs, then uppercase + collapse whitespace).
* A group is a SUBSCRIPTION only when it has >= ``MIN_CHARGES`` (3) charges of
  similar amount (each within ``AMOUNT_TOLERANCE`` ~15% of the group median)
  at a *regular* cadence (monthly / annual / weekly), judged from the **median**
  gap between consecutive charge dates.
* Owner contributions / distributions / transfers / loans / other internal
  categories are skipped outright — they recur but are not cancellable
  subscriptions.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any

# --------------------------------------------------------------------------- #
# Tunables
# --------------------------------------------------------------------------- #

#: Minimum number of similar charges before a group can be called recurring.
MIN_CHARGES = 3

#: A charge counts toward a group if its amount is within this fraction of the
#: group's median amount (covers tax/FX wobble and small price changes).
AMOUNT_TOLERANCE = Decimal("0.15")

#: A subscription is "active" if its last charge is within this multiple of one
#: cadence period of ``as_of`` (e.g. a monthly sub seen <= ~1.5 months ago).
ACTIVE_CADENCE_MULTIPLE = Decimal("1.5")

#: Cadence gap windows, in days, keyed by cadence label. Used both to classify
#: the median gap and to project ``next_expected``.
_CADENCE_DAYS = {
    "weekly": 7,
    "monthly": 30,
    "annual": 365,
}

#: Inclusive day-gap ranges that classify the median inter-charge gap. Ordered;
#: first match wins. Anything outside every range is "irregular".
_CADENCE_RANGES = (
    ("weekly", 5, 9),
    ("biweekly", 12, 18),
    ("monthly", 26, 35),
    ("quarterly", 80, 100),
    ("annual", 330, 400),
)

#: Days-per-period for cadences that don't appear in ``_CADENCE_DAYS`` but can
#: still be classified/projected (so ``next_expected`` works for all of them).
_CADENCE_PERIOD_DAYS = {
    "weekly": 7,
    "biweekly": 14,
    "monthly": 30,
    "quarterly": 91,
    "annual": 365,
}

#: Category substrings (case-insensitive) that mark an entry as internal /
#: equity / financing movement — never a cancellable subscription. Owner draws
#: recur monthly but must NOT be flagged.
_SKIP_CATEGORY_SUBSTRINGS = (
    "owner",
    "transfer",
    "loan",
    "distribution",
    "contribution",
    "intercompany",
    "inter-company",
    "internal",
    "draw",
    "equity",
    "capital",
    "reconcil",
)

# --------------------------------------------------------------------------- #
# Normalization
# --------------------------------------------------------------------------- #

# Leading point-of-sale / card prefixes a bank glues onto the merchant name,
# usually followed by a 4-digit terminal/date token: "CHECKCARD 0428 NETFLIX".
_LEADING_PREFIX_RE = re.compile(
    r"^\s*(?:CHECKCARD|CHECK\s*CARD|POS\s*(?:DEBIT|PURCHASE)?|DEBIT\s*CARD|"
    r"PURCHASE\s*AUTHORIZED\s*ON|RECURRING\s*PAYMENT|ACH\s*(?:DEBIT|WEB)?|"
    r"VISA|CARD)\b[\s#:*-]*\d{0,6}\s*",
    re.IGNORECASE,
)

# Processor tail that hides the real merchant in a structured string, e.g.
# "PAYPAL DES:INST XFER ID:NETFLIX INDN:..." -> keep just "PAYPAL". We cut from
# the first DES:/ID:/INDN:/CO ID:/PPD ID: marker onward.
_PROCESSOR_TAIL_RE = re.compile(
    r"\b(?:DES|ID|INDN|CO\s*ID|PPD\s*ID|WEB\s*ID|REF|ARC|CCD)\s*[:#].*$",
    re.IGNORECASE,
)

# A trailing date in common statement formats: 04/28, 2026-04-28, 04/28/26 ...
_TRAILING_DATE_RE = re.compile(
    r"\b\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,4})?\b\s*$"
)

# Standalone long digit runs (txn ids, card fragments, phone numbers) anywhere.
_LONG_DIGITS_RE = re.compile(r"\b\d{3,}\b")

# Trailing dollar amount the bank sometimes appends: "... $38.00".
_TRAILING_AMOUNT_RE = re.compile(r"\$?\s*\d+(?:\.\d{2})\s*$")

# A leading 4-digit card/terminal token left over after the prefix strip.
_LEADING_TOKEN_RE = re.compile(r"^\s*[#*]?\d{3,6}\s+")

# Collapse runs of non-alphanumeric junk to a single space.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9&]+")


def _normalize_merchant(payee: Any, description: Any) -> str:
    """Derive a stable merchant key from ``payee`` (preferred) or ``description``.

    Returns an UPPERCASE, whitespace-collapsed token with statement noise
    (POS prefixes, processor tails, dates, long id runs, trailing amounts)
    stripped. Falls back to ``description`` only when ``payee`` is blank.
    Returns ``""`` when nothing usable remains.
    """
    raw = ""
    if payee is not None and str(payee).strip():
        raw = str(payee)
    elif description is not None and str(description).strip():
        raw = str(description)
    if not raw.strip():
        return ""

    s = raw.strip()
    s = _LEADING_PREFIX_RE.sub("", s)          # drop CHECKCARD/POS/ACH prefixes
    s = _PROCESSOR_TAIL_RE.sub("", s)          # cut DES:/ID:/INDN: tails
    s = _TRAILING_AMOUNT_RE.sub("", s)         # drop trailing "$38.00"
    s = _TRAILING_DATE_RE.sub("", s)           # drop trailing date
    s = _LEADING_TOKEN_RE.sub("", s)           # drop leftover leading id token
    s = _LONG_DIGITS_RE.sub(" ", s)            # blank out long digit runs
    s = _NON_ALNUM_RE.sub(" ", s)              # punctuation -> space
    s = s.strip().upper()
    s = re.sub(r"\s+", " ", s)
    return s


# --------------------------------------------------------------------------- #
# Coercion helpers (input rows may carry str/Decimal/date already)
# --------------------------------------------------------------------------- #

def _to_date(value: Any) -> date | None:
    """Coerce a journal date-ish value to ``date`` (None if unparseable)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    if not s:
        return None
    # Tolerate full ISO timestamps as well as plain dates.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(s).date()
        except ValueError:
            return None


def _to_decimal(value: Any) -> Decimal | None:
    """Coerce an amount to a non-negative ``Decimal`` magnitude (None if bad).

    Subscriptions are outflows; sign conventions vary by source, so we compare
    on absolute magnitude.
    """
    if value is None:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return abs(d)


def _entry_date(entry: dict) -> date | None:
    """Pick the charge date: prefer effective_date, fall back to posted_at."""
    return _to_date(entry.get("effective_date")) or _to_date(entry.get("posted_at"))


def _is_skippable_category(category: Any) -> bool:
    """True when the category marks an internal/equity/financing movement."""
    if not category:
        return False
    cat = str(category).lower()
    return any(token in cat for token in _SKIP_CATEGORY_SUBSTRINGS)


# --------------------------------------------------------------------------- #
# Cadence
# --------------------------------------------------------------------------- #

def _classify_cadence(median_gap_days: float) -> str:
    """Map a median inter-charge gap (days) to a cadence label."""
    for label, lo, hi in _CADENCE_RANGES:
        if lo <= median_gap_days <= hi:
            return label
    return "irregular"


def _amounts_similar(amounts: list[Decimal]) -> bool:
    """True when every amount is within AMOUNT_TOLERANCE of the median amount."""
    if not amounts:
        return False
    med = median(amounts)
    if med == 0:
        # All-zero (or median zero) groups aren't real recurring spend.
        return all(a == 0 for a in amounts)
    bound = med * AMOUNT_TOLERANCE
    return all(abs(a - med) <= bound for a in amounts)


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def detect_subscriptions(
    entries: list[dict],
    as_of: date | None = None,
) -> list[dict]:
    """Detect recurring subscription charges from journal-entry dicts.

    Args:
        entries: Journal rows. Each dict may carry ``payee``, ``description``,
            ``amount``, ``counterparty_id``, ``category``, ``source``,
            ``effective_date`` and/or ``posted_at`` (strings, ``Decimal``,
            ``date`` — all coerced here). Unknown keys are ignored.
        as_of: Anchor date for activity/recency math (defaults to today). Used
            to compute ``is_active`` and ``months_since_last``.

    Returns:
        A list of subscription dicts, sorted by ``typical_amount`` descending.
        Each item::

            {
              "merchant":          str,        # normalized merchant key
              "cadence":           str,        # monthly|annual|weekly|...|irregular
              "typical_amount":    str,        # median charge, 2dp, as string
              "charge_count":      int,
              "first_seen":        "YYYY-MM-DD",
              "last_seen":         "YYYY-MM-DD",
              "next_expected":     "YYYY-MM-DD" | None,  # last_seen + cadence
              "funding_source":    str | None, # `source` (bofa-statement|simplefin|..)
              "is_active":         bool,       # last charge within ~1.5x cadence
              "months_since_last": float,      # 30-day months since last charge
              "total_paid":        str,        # sum of |amount|, 2dp, as string
            }

    The function is pure (no DB, no clock other than the explicit ``as_of``)
    and conservative: one-off purchases and owner-equity / transfer / loan
    movements are never reported.
    """
    if as_of is None:
        as_of = date.today()

    # ----- 1. group by merchant key ------------------------------------- #
    # Key precedence: counterparty_id (strong) else normalized merchant string.
    groups: dict[str, list[dict]] = defaultdict(list)
    # Track a human-readable label per group so a counterparty-keyed group still
    # surfaces a real merchant name.
    labels: dict[str, str] = {}

    for entry in entries:
        if _is_skippable_category(entry.get("category")):
            continue

        when = _entry_date(entry)
        amount = _to_decimal(entry.get("amount"))
        if when is None or amount is None:
            continue

        merchant = _normalize_merchant(entry.get("payee"), entry.get("description"))

        cp = entry.get("counterparty_id")
        if cp:
            key = f"cp:{cp}"
            # Prefer a real normalized name as the label; fall back to the id.
            if key not in labels:
                labels[key] = merchant or f"counterparty {cp}"
        elif merchant:
            key = f"m:{merchant}"
            labels.setdefault(key, merchant)
        else:
            # No usable identity at all — can't responsibly group it.
            continue

        groups[key].append(
            {
                "date": when,
                "amount": amount,
                "source": entry.get("source"),
            }
        )

    # ----- 2. evaluate each group --------------------------------------- #
    results: list[dict] = []
    for key, rows in groups.items():
        if len(rows) < MIN_CHARGES:
            continue

        rows.sort(key=lambda r: r["date"])
        amounts = [r["amount"] for r in rows]
        if not _amounts_similar(amounts):
            continue

        dates = [r["date"] for r in rows]
        gaps = [
            (dates[i + 1] - dates[i]).days
            for i in range(len(dates) - 1)
            if (dates[i + 1] - dates[i]).days > 0  # collapse same-day dupes
        ]
        if len(gaps) < MIN_CHARGES - 1:
            # Not enough distinct charge dates to establish a cadence.
            continue

        med_gap = float(median(gaps))
        cadence = _classify_cadence(med_gap)
        if cadence == "irregular":
            # Be conservative: a 3+ charge cluster with no regular rhythm is
            # most likely incidental repeat spend, not a subscription.
            continue

        first_seen = dates[0]
        last_seen = dates[-1]
        typical = median(amounts).quantize(Decimal("0.01"))
        total_paid = sum(amounts, Decimal("0")).quantize(Decimal("0.01"))

        period_days = _CADENCE_PERIOD_DAYS.get(cadence)
        if period_days:
            from datetime import timedelta

            next_expected: str | None = (last_seen + timedelta(days=period_days)).isoformat()
            active_window = period_days * float(ACTIVE_CADENCE_MULTIPLE)
        else:  # pragma: no cover - irregular already filtered out above
            next_expected = None
            active_window = med_gap * float(ACTIVE_CADENCE_MULTIPLE)

        days_since_last = (as_of - last_seen).days
        is_active = days_since_last <= active_window
        months_since_last = round(days_since_last / 30.0, 1)

        # Funding source = the source seen on the charges (the funding account).
        # Use the most recent non-null source so a backfilled history with a
        # later canonical source reports the current funder.
        funding_source = None
        for r in reversed(rows):
            if r["source"]:
                funding_source = r["source"]
                break

        results.append(
            {
                "merchant": labels.get(key, key),
                "cadence": cadence,
                "typical_amount": str(typical),
                "charge_count": len(rows),
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
                "next_expected": next_expected,
                "funding_source": funding_source,
                "is_active": is_active,
                "months_since_last": months_since_last,
                "total_paid": str(total_paid),
            }
        )

    # ----- 3. sort by typical_amount desc ------------------------------- #
    results.sort(key=lambda r: Decimal(r["typical_amount"]), reverse=True)
    return results
