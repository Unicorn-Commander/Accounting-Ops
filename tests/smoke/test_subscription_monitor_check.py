"""Unit tests for the subscription_monitor Sentinel check — no live Postgres.

Two tiers, mirroring tests/smoke/test_detect_subscriptions.py (zero-I/O):

* Registry/import asserts (the must-haves): the check is wired into
  CHECK_REGISTRY + ALL_CHECK_TYPES + CHECK_CADENCE and the module imports.
* A driven ``run()`` over synthetic detector output (real check logic, no DB):
  the journal SELECT + ``detect_subscriptions`` are stubbed so the test exercises
  the check's OWN behaviour — the active-or-recent filter, the spend-based
  severity, and the per-merchant ``dedupe_key`` it passes to ``execute_action``.

The real ActionContext + DB session are heavy, so the session is a minimal fake
whose ``execute().mappings().all()`` returns a single dated row (enough to clear
run()'s "no entries" guard and derive ``book_as_of``; ``detect_subscriptions``
is monkeypatched to supply the synthetic subs directly), and ``execute_action``
is captured to assert the exact calls ``run()`` makes.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

import agents.sentinel.checks.subscription_monitor as sm
from agents.sentinel.checks import CHECK_REGISTRY
from agents.sentinel.config import ALL_CHECK_TYPES, CHECK_CADENCE


# --------------------------------------------------------------------------- #
# Must-haves: registration + import
# --------------------------------------------------------------------------- #

def test_subscription_monitor_in_check_registry():
    assert "subscription_monitor" in CHECK_REGISTRY
    assert CHECK_REGISTRY["subscription_monitor"] is sm.run


def test_subscription_monitor_in_all_check_types():
    assert "subscription_monitor" in ALL_CHECK_TYPES


def test_subscription_monitor_has_a_cadence():
    assert CHECK_CADENCE.get("subscription_monitor") == "weekly"


def test_module_imports_run_coroutine():
    assert asyncio.iscoroutinefunction(sm.run)


# --------------------------------------------------------------------------- #
# Driven run(): real check logic over stubbed detector output (no DB)
# --------------------------------------------------------------------------- #

class _FakeResult:
    def mappings(self):
        return self

    def all(self):
        # One dated row so run() clears its "no entries" guard and can derive
        # book_as_of; detect_subscriptions is stubbed, so contents past
        # effective_date are irrelevant.
        return [{"effective_date": "2026-05-01", "amount": "1.00"}]


class _FakeSession:
    """Minimal async session: execute() is awaitable, returns empty rows."""

    async def execute(self, *_args, **_kwargs):
        return _FakeResult()


def _sub(merchant: str, *, is_active: bool, months_since_last: float, **over):
    """A detector-shaped subscription dict (the keys run() reads)."""
    base = {
        "merchant": merchant,
        "cadence": "monthly",
        "typical_amount": "38.00",
        "charge_count": 6,
        "first_seen": "2025-01-10",
        "last_seen": "2025-06-10",
        "next_expected": "2025-07-10",
        "funding_source": "bofa-statement",
        "is_active": is_active,
        "months_since_last": months_since_last,
        "total_paid": "228.00",
    }
    base.update(over)
    return base


def _drive_run(monkeypatch, *, entities, subs):
    """Run sm.run() with list_active_entities + detect_subscriptions + execute_action
    stubbed, capturing every execute_action call."""
    calls: list[dict] = []

    async def fake_list_active_entities(_session):
        return entities

    def fake_detect_subscriptions(_entries, as_of=None):
        return subs

    async def fake_execute_action(_ctx, **kwargs):
        calls.append(kwargs)
        return {"ok": True, "action_type": kwargs["action_type"]}

    monkeypatch.setattr(sm, "list_active_entities", fake_list_active_entities)
    monkeypatch.setattr(sm, "detect_subscriptions", fake_detect_subscriptions)
    monkeypatch.setattr(sm, "execute_action", fake_execute_action)

    ctx = SimpleNamespace(session=_FakeSession())
    result = asyncio.run(sm.run(ctx))
    return result, calls


def test_run_flags_only_active_subscriptions(monkeypatch):
    ent_id = uuid4()
    entity = SimpleNamespace(id=ent_id, display_name="Acme LLC", slug="acme")
    subs = [
        _sub("PICTORY", is_active=True, months_since_last=0.5),
        _sub("ADOBE", is_active=False, months_since_last=6.0),   # stale -> skipped
        _sub("SALESFORCE", is_active=True, months_since_last=0.2),
    ]

    result, calls = _drive_run(monkeypatch, entities=[entity], subs=subs)

    # Only the two ACTIVE subscriptions are surfaced.
    assert result["findings"] == 2
    assert result["actions"] == 2
    flagged = {c["title"].split(":")[1].strip().split(" ")[0] for c in calls}
    assert flagged == {"PICTORY", "SALESFORCE"}
    assert all(c["action_type"] == "flag_for_cpa_review" for c in calls)
    assert all(c["entity_id"] == ent_id for c in calls)


def test_run_uses_per_merchant_dedupe_key(monkeypatch):
    ent_id = uuid4()
    entity = SimpleNamespace(id=ent_id, display_name="Acme LLC", slug="acme")
    subs = [_sub("PICTORY", is_active=True, months_since_last=0.5)]

    _result, calls = _drive_run(monkeypatch, entities=[entity], subs=subs)

    assert len(calls) == 1
    # ONE self-refreshing finding per merchant per entity — not period-stamped.
    assert calls[0]["dedupe_key"] == f"subscription:{ent_id}:PICTORY"


def test_run_severity_tracks_monthly_spend(monkeypatch):
    ent_id = uuid4()
    entity = SimpleNamespace(id=ent_id, display_name="Acme LLC", slug="acme")
    subs = [
        _sub("CHEAP", is_active=True, months_since_last=0.5, typical_amount="38.00"),    # <$50/mo -> low
        _sub("PRICEY", is_active=True, months_since_last=0.5, typical_amount="206.64"),  # >=$50/mo -> medium
    ]

    _result, calls = _drive_run(monkeypatch, entities=[entity], subs=subs)

    by_merchant = {c["title"].split(":")[1].strip().split(" ")[0]: c for c in calls}
    assert by_merchant["CHEAP"]["severity"] == "low"
    assert by_merchant["PRICEY"]["severity"] == "medium"


def test_run_carries_full_detector_dict_in_details(monkeypatch):
    ent_id = uuid4()
    entity = SimpleNamespace(id=ent_id, display_name="Acme LLC", slug="acme")
    subs = [_sub("PICTORY", is_active=True, months_since_last=0.5)]

    _result, calls = _drive_run(monkeypatch, entities=[entity], subs=subs)

    details = calls[0]["details"]
    # The whole detector dict is preserved on the finding for the reviewer.
    for k in (
        "merchant", "cadence", "typical_amount", "charge_count",
        "first_seen", "last_seen", "next_expected", "funding_source",
        "is_active", "months_since_last", "total_paid",
    ):
        assert k in details
    assert details["merchant"] == "PICTORY"


def test_run_no_active_subs_returns_zero(monkeypatch):
    entity = SimpleNamespace(id=uuid4(), display_name="Acme LLC", slug="acme")
    subs = [_sub("ADOBE", is_active=False, months_since_last=6.0)]

    result, calls = _drive_run(monkeypatch, entities=[entity], subs=subs)

    assert result["findings"] == 0
    assert result["actions"] == 0
    assert calls == []


def test_run_no_entities_returns_zero(monkeypatch):
    result, calls = _drive_run(monkeypatch, entities=[], subs=[])
    assert result["findings"] == 0
    assert result["actions"] == 0
    assert calls == []
