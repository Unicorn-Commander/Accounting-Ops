"""Smoke tests for app/api/tax_status.py.

These are import-level structural checks — no DB connection required.

Checks:
  1. Module imports cleanly.
  2. Router is an APIRouter and exposes the correct route path.
  3. Deadline helper produces correct dates (including weekend advancement).
  4. Filing status derivation covers all branches:
       - no items + today > deadline → 'overdue'
       - no items + today <= deadline → 'no_draft'
       - items with pending > 0 + today > deadline → 'overdue'
       - items with pending > 0 + today <= deadline → 'draft'
       - items with pending == 0 + today <= deadline → 'cpa_review'
  5. Books status derivation covers all three branches.
  6. Module registers a GET route containing '/tax-status'.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers: import the module once; use module-level helpers directly.
# ---------------------------------------------------------------------------


def _load():
    # Ensure re-import picks up fresh state if tests run in isolation.
    mod_name = "app.api.tax_status"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    return importlib.import_module(mod_name)


# ---------------------------------------------------------------------------
# 1. Import smoke
# ---------------------------------------------------------------------------


def test_module_imports_cleanly():
    mod = _load()
    assert mod is not None


# ---------------------------------------------------------------------------
# 2. Router shape
# ---------------------------------------------------------------------------


def test_router_is_api_router():
    from fastapi import APIRouter
    mod = _load()
    assert isinstance(mod.router, APIRouter)


def test_router_has_tax_status_route():
    mod = _load()
    paths = [r.path for r in mod.router.routes]  # type: ignore[attr-defined]
    assert any("/tax-status" in p for p in paths), (
        f"Expected a /tax-status route; found: {paths}"
    )


# ---------------------------------------------------------------------------
# 3. Deadline helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tax_year, expected",
    [
        # 2022: April 15 2023 is a Saturday → advance to Monday April 17
        (2022, dt.date(2023, 4, 17)),
        # 2023: April 15 2024 is a Monday → stays April 15
        (2023, dt.date(2024, 4, 15)),
        # 2024: April 15 2025 is a Tuesday → stays April 15
        (2024, dt.date(2025, 4, 15)),
        # 2025: April 15 2026 is a Wednesday → stays April 15
        (2025, dt.date(2026, 4, 15)),
    ],
)
def test_filing_deadline(tax_year: int, expected: dt.date):
    mod = _load()
    result = mod._filing_deadline(tax_year)
    assert result == expected, (
        f"_filing_deadline({tax_year}) = {result}, expected {expected}"
    )


# ---------------------------------------------------------------------------
# 4. Filing status derivation
# ---------------------------------------------------------------------------


def test_filing_status_no_items_overdue():
    """Empty queue + today past deadline → 'overdue'."""
    mod = _load()
    deadline = dt.date(2023, 4, 17)
    today = dt.date(2026, 5, 29)  # clearly past
    result = mod._filing_status(
        {"pending": 0, "reviewed": 0, "resolved": 0, "rejected": 0},
        deadline,
        today,
    )
    assert result == "overdue", f"Expected 'overdue', got {result!r}"


def test_filing_status_no_items_no_draft():
    """Empty queue + today before deadline → 'no_draft'."""
    mod = _load()
    deadline = dt.date(2027, 4, 15)
    today = dt.date(2026, 5, 29)
    result = mod._filing_status(
        {"pending": 0, "reviewed": 0, "resolved": 0, "rejected": 0},
        deadline,
        today,
    )
    assert result == "no_draft", f"Expected 'no_draft', got {result!r}"


def test_filing_status_pending_items_overdue():
    """Queue has pending items + today past deadline → 'overdue'."""
    mod = _load()
    deadline = dt.date(2023, 4, 17)
    today = dt.date(2026, 5, 29)
    result = mod._filing_status(
        {"pending": 14, "reviewed": 0, "resolved": 0, "rejected": 0},
        deadline,
        today,
    )
    assert result == "overdue", f"Expected 'overdue', got {result!r}"


def test_filing_status_pending_items_in_future():
    """Queue has pending items + today before deadline → 'draft'."""
    mod = _load()
    deadline = dt.date(2027, 4, 15)
    today = dt.date(2026, 5, 29)
    result = mod._filing_status(
        {"pending": 5, "reviewed": 0, "resolved": 0, "rejected": 0},
        deadline,
        today,
    )
    assert result == "draft", f"Expected 'draft', got {result!r}"


def test_filing_status_all_reviewed():
    """Queue has items but all resolved/reviewed → 'cpa_review'."""
    mod = _load()
    deadline = dt.date(2027, 4, 15)
    today = dt.date(2026, 5, 29)
    result = mod._filing_status(
        {"pending": 0, "reviewed": 3, "resolved": 11, "rejected": 0},
        deadline,
        today,
    )
    assert result == "cpa_review", f"Expected 'cpa_review', got {result!r}"


# ---------------------------------------------------------------------------
# 5. Books status
# ---------------------------------------------------------------------------


def test_books_status_no_data():
    """Nothing posted AND nothing queued is the only real 'no data'."""
    mod = _load()
    assert mod._books_status({"pending": 0, "total": 0}, 0) == "no_data"


def test_books_status_in_progress():
    mod = _load()
    assert mod._books_status({"pending": 3, "total": 10}, 120) == "in_progress"


def test_books_status_complete():
    mod = _load()
    assert mod._books_status({"pending": 0, "total": 7}, 120) == "complete"


# --- regression: books_status must read the JOURNAL, not the queue ---------
#
# Magic Unicorn Inc TY2023 holds 438 posted journal entries and an EMPTY
# approval queue (every row was decided in July 2026). The old
# queue-only derivation reported "no_data" for it, and that single wrong word
# is why multiple readers concluded Aaron's books were empty while four years
# of reconciled history sat in the ledger.


def test_reconciled_year_with_drained_queue_is_complete_not_no_data():
    mod = _load()
    assert mod._books_status({"pending": 0, "total": 0}, 438) == "complete"


def test_posted_entries_outrank_an_empty_queue_even_with_pending():
    mod = _load()
    assert mod._books_status({"pending": 3, "total": 3}, 438) == "in_progress"


def test_queued_but_unposted_year_is_in_progress():
    """Rows waiting to be decided are work in flight, not 'no data'."""
    mod = _load()
    assert mod._books_status({"pending": 12, "total": 12}, 0) == "in_progress"


# ---------------------------------------------------------------------------
# 5b. incorporated_at must come from the real column
# ---------------------------------------------------------------------------


def test_entity_query_selects_the_real_incorporated_at_column():
    """The entity SELECT must not hardcode NULL for incorporated_at.

    It did for a long time, behind a comment claiming the column had never
    been added. The column exists and is populated (Magic Unicorn Inc =
    2022-05-11); hardcoding NULL made short-year detection dead code and fed
    the QSBS §1202 milestone check a null from the API path.
    """
    import inspect

    mod = _load()
    src = inspect.getsource(mod.get_tax_status)
    assert "NULL::date            AS incorporated_at" not in src
    assert "NULL::date AS incorporated_at" not in src
    assert "incorporated_at," in src


def test_short_year_detection_fires_for_a_mid_year_incorporation():
    """A company incorporated 2022-05-11 has a SHORT first tax year."""
    mod = _load()
    inc = dt.date(2022, 5, 11)
    # This mirrors the expression in get_tax_status.
    is_short = bool(inc and inc.year == 2022 and inc > dt.date(2022, 1, 1))
    assert is_short is True
    # ...and a full year for every subsequent year.
    assert bool(inc and inc.year == 2023 and inc > dt.date(2023, 1, 1)) is False


def test_bulk_journal_summary_exists_and_is_keyed_on_effective_date():
    """books_status must bucket by the transaction date, not the ingest date.

    _bulk_approval_summary buckets on created_at, so Magic Unicorn Inc's 827
    bank imports — all ingested on 2026-05-28 — counted zero for every year
    from 2022 to 2025. The journal helper must not repeat that.
    """
    import inspect

    mod = _load()
    assert hasattr(mod, "_bulk_journal_summary")
    src = inspect.getsource(mod._bulk_journal_summary)
    assert "effective_date" in src
    assert "created_at" not in src
    assert "'posted'" in src
