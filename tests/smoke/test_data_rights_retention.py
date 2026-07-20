"""Retention-configuration unit tests (no database).

Covers the pure logic in app/data_rights/retention.py:
  * the documented default applies when nothing is stored;
  * a stored override is honoured;
  * bounds are enforced, including the 0 == "purge immediately" case the
    privacy policy promises;
  * a CORRUPTED stored document degrades to the default instead of raising —
    an erasure must not be bricked by a bad settings blob;
  * purge_due_at arithmetic.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.data_rights.retention import (
    DEFAULT_LEDGER_RETENTION_DAYS,
    MAX_LEDGER_RETENTION_DAYS,
    MIN_LEDGER_RETENTION_DAYS,
    RetentionConfigError,
    parse_retention,
    purge_due_at,
    validate_retention_days,
)


def test_default_applies_when_nothing_stored():
    for doc in (None, {}, {"version": 1}):
        cfg = parse_retention(doc)
        assert cfg.is_default is True
        assert cfg.ledger_retention_days == DEFAULT_LEDGER_RETENTION_DAYS


def test_stored_override_is_honoured():
    cfg = parse_retention(
        {
            "version": 1,
            "retention": {
                "ledger_retention_days": 90,
                "set_by_uc_uid": "user-1",
                "set_at": "2026-07-20T00:00:00+00:00",
            },
        }
    )
    assert cfg.is_default is False
    assert cfg.ledger_retention_days == 90
    assert cfg.set_by_uc_uid == "user-1"


def test_zero_means_immediate_purge_and_is_valid():
    # The privacy policy promises a customer can demand immediate destruction.
    # If this ever stops being accepted, that promise becomes a lie.
    assert validate_retention_days(0) == 0
    cfg = parse_retention({"retention": {"ledger_retention_days": 0}})
    assert cfg.ledger_retention_days == 0
    assert cfg.is_default is False


@pytest.mark.parametrize("bad", [-1, MAX_LEDGER_RETENTION_DAYS + 1, 10**9])
def test_out_of_range_is_rejected(bad):
    with pytest.raises(RetentionConfigError):
        validate_retention_days(bad)


@pytest.mark.parametrize("bad", ["30", 30.5, None, True, [30]])
def test_non_integer_is_rejected(bad):
    # bool is explicitly excluded: True would otherwise pass isinstance(int).
    with pytest.raises(RetentionConfigError):
        validate_retention_days(bad)


def test_bounds_are_inclusive():
    assert validate_retention_days(MIN_LEDGER_RETENTION_DAYS) == MIN_LEDGER_RETENTION_DAYS
    assert validate_retention_days(MAX_LEDGER_RETENTION_DAYS) == MAX_LEDGER_RETENTION_DAYS


@pytest.mark.parametrize(
    "corrupt",
    [
        {"retention": "not-a-dict"},
        {"retention": {"ledger_retention_days": "many"}},
        {"retention": {"ledger_retention_days": -5}},
        {"retention": {}},
        {"retention": None},
    ],
)
def test_corrupt_document_falls_back_to_default(corrupt):
    cfg = parse_retention(corrupt)
    assert cfg.is_default is True
    assert cfg.ledger_retention_days == DEFAULT_LEDGER_RETENTION_DAYS


def test_purge_due_at():
    now = dt.datetime(2026, 7, 20, tzinfo=dt.timezone.utc)
    cfg = parse_retention({"retention": {"ledger_retention_days": 30}})
    assert purge_due_at(now, cfg) == dt.datetime(2026, 8, 19, tzinfo=dt.timezone.utc)
    # Zero-day retention means the purge is due immediately.
    assert purge_due_at(now, parse_retention({"retention": {"ledger_retention_days": 0}})) == now


def test_to_dict_exposes_the_bounds_and_default():
    d = parse_retention(None).to_dict()
    assert d["default_ledger_retention_days"] == DEFAULT_LEDGER_RETENTION_DAYS
    assert d["min_ledger_retention_days"] == MIN_LEDGER_RETENTION_DAYS
    assert d["max_ledger_retention_days"] == MAX_LEDGER_RETENTION_DAYS
    assert d["is_default"] is True
