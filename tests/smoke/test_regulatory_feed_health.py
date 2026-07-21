"""Regulatory feed health — a dead feed must become a FINDING, not a log line.

The defect being fixed: the IRS Newsroom RSS URL 404'd on every cycle for an
unknown period. The fetcher swallowed it, logged one warning, returned ``[]``,
and the ``regulatory_updates`` check reported a cheerful "no new entries" —
byte-identical to a quiet news day. The feed was silently inert for weeks.

These tests pin the three properties that make that impossible to repeat:
  1. A failing fetch is RECORDED as a failure (not collapsed into "0 new").
  2. A reachable-but-empty feed also counts as a failure — that is the exact
     shape a silently-retired source takes.
  3. Persistent failure raises a Sentinel finding, and it does so on the
     no-new-entries path, which is the only path a dead feed can reach.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from agents.sentinel.checks import regulatory_updates
from agents.sentinel.config import AutonomyLevel, SentinelConfig
from app.connectors import regulatory_feeds
from app.connectors.regulatory_feeds import FeedFetchError, feed_health_report

NOW = dt.datetime(2026, 7, 20, 12, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    @property
    def rowcount(self) -> int:
        return 1


class _FakeSession:
    """Captures executed statements so health upserts can be asserted."""

    def __init__(self, rows: list[dict[str, Any]] | None = None):
        self.rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params or {}))
        if "regulatory_feed_health" in sql and "SELECT" in sql.upper():
            return _Result(self.rows)
        return _Result([])

    def health_upserts(self) -> list[dict]:
        return [
            p for sql, p in self.calls
            if "regulatory_feed_health" in sql and "INSERT" in sql.upper()
        ]


def _health_row(**over) -> dict[str, Any]:
    row = {
        "source": "irs_federal_register",
        "last_attempt_at": NOW,
        "last_success_at": NOW - dt.timedelta(hours=1),
        "consecutive_failures": 0,
        "last_error": None,
        "last_entry_count": 40,
        "hours_since_success": 1.0,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# Staleness computation
# ---------------------------------------------------------------------------


async def test_healthy_feed_is_not_stale():
    session = _FakeSession([_health_row()])
    report = await feed_health_report(session)
    assert report[0]["stale"] is False


async def test_persistently_failing_feed_is_stale():
    """The live symptom: 404 every cycle, no success in days."""
    session = _FakeSession([_health_row(
        consecutive_failures=27,
        last_success_at=NOW - dt.timedelta(days=14),
        hours_since_success=336.0,
        last_error="Client error '404 Not Found' for url ...",
    )])
    report = await feed_health_report(session)
    assert report[0]["stale"] is True
    assert report[0]["consecutive_failures"] == 27


async def test_never_succeeded_feed_is_stale():
    session = _FakeSession([_health_row(
        last_success_at=None, hours_since_success=None, consecutive_failures=5,
    )])
    report = await feed_health_report(session)
    assert report[0]["never_succeeded"] is True
    assert report[0]["stale"] is True


async def test_single_transient_blip_is_not_stale():
    """One failed poll must not page anyone — both thresholds must trip."""
    session = _FakeSession([_health_row(
        consecutive_failures=1,
        last_success_at=NOW - dt.timedelta(hours=6),
        hours_since_success=6.0,
    )])
    report = await feed_health_report(session)
    assert report[0]["stale"] is False


async def test_many_failures_but_recent_success_is_not_stale():
    session = _FakeSession([_health_row(
        consecutive_failures=9, hours_since_success=2.0,
    )])
    report = await feed_health_report(session)
    assert report[0]["stale"] is False


async def test_missing_health_table_degrades_quietly():
    """Pre-migration deploy: report is empty, nothing raises."""

    class _Broken:
        async def execute(self, *a, **kw):
            raise RuntimeError('relation "regulatory_feed_health" does not exist')

    assert await feed_health_report(_Broken()) == []


# ---------------------------------------------------------------------------
# poll_all records health
# ---------------------------------------------------------------------------


async def test_failing_feed_is_recorded_as_a_failure(monkeypatch):
    async def _boom():
        raise FeedFetchError("Client error '404 Not Found' for url ...")

    async def _fine():
        return [{
            "source": "scdor_notices", "external_id": "x", "title": "Notice",
            "summary": "", "url": "u", "jurisdiction": "SC", "published_at": None,
        }]

    monkeypatch.setattr(
        regulatory_feeds, "FEEDS",
        (("irs_federal_register", _boom), ("scdor_notices", _fine)),
    )
    session = _FakeSession()
    results = await regulatory_feeds.poll_all(session)

    assert results["irs_federal_register"]["ok"] is False
    assert "404" in results["irs_federal_register"]["error"]
    assert results["scdor_notices"]["ok"] is True

    upserts = {u["source"]: u for u in session.health_upserts()}
    assert upserts["irs_federal_register"]["ok"] is False
    assert upserts["scdor_notices"]["ok"] is True


async def test_reachable_but_empty_feed_counts_as_failure(monkeypatch):
    """A 200 returning zero entries is how a retired feed actually looks."""

    async def _empty():
        return []

    monkeypatch.setattr(regulatory_feeds, "FEEDS", (("irs_newsroom", _empty),))
    session = _FakeSession()
    results = await regulatory_feeds.poll_all(session)

    assert results["irs_newsroom"]["ok"] is False
    assert session.health_upserts()[0]["ok"] is False


async def test_one_broken_feed_does_not_block_the_others(monkeypatch):
    async def _crash():
        raise ValueError("unexpected")

    async def _fine():
        return [{
            "source": "scdor_notices", "external_id": "x", "title": "Notice",
            "summary": "", "url": "u", "jurisdiction": "SC", "published_at": None,
        }]

    monkeypatch.setattr(
        regulatory_feeds, "FEEDS", (("broken", _crash), ("scdor_notices", _fine)),
    )
    results = await regulatory_feeds.poll_all(_FakeSession())
    assert results["broken"]["ok"] is False
    assert results["scdor_notices"]["ok"] is True


# ---------------------------------------------------------------------------
# The Sentinel check raises a finding
# ---------------------------------------------------------------------------


@dataclass
class _Ctx:
    session: Any
    config: SentinelConfig
    check_run_id: Any
    check_type: str
    workspace_id: Any


def _ctx(session) -> _Ctx:
    wid = uuid4()
    return _Ctx(
        session=session,
        config=SentinelConfig(
            workspace_id=wid,
            autonomy_level=AutonomyLevel.WATCH_ONLY,
            check_interval_minutes=360,
            max_actions_per_day=50,
            enabled_checks=("regulatory_updates",),
            notify_owners=True,
            notify_accountants=True,
        ),
        check_run_id=uuid4(),
        check_type="regulatory_updates",
        workspace_id=wid,
    )


async def test_stale_feed_raises_a_finding(monkeypatch):
    captured: list[dict] = []

    async def _fake_action(_ctx, **kw):
        captured.append(kw)
        return {"ok": True}

    monkeypatch.setattr(regulatory_updates, "execute_action", _fake_action)

    async def _report(_session, **kw):
        return [{
            "source": "irs_federal_register",
            "last_attempt_at": NOW,
            "last_success_at": NOW - dt.timedelta(days=14),
            "consecutive_failures": 27,
            "last_error": "404 Not Found",
            "last_entry_count": 0,
            "hours_since_success": 336.0,
            "never_succeeded": False,
            "stale": True,
        }]

    monkeypatch.setattr(regulatory_updates, "feed_health_report", _report)

    result = await regulatory_updates._check_feed_health(_ctx(_FakeSession()))

    assert result["findings"] == 1
    assert result["actions"] == 1
    action = captured[0]
    # watch_only workspaces must still get it -> log_finding, not draft_memo.
    assert action["action_type"] == "log_finding"
    assert action["severity"] == "warning"
    assert action["dedupe_key"] == "regulatory_feed_stale:irs_federal_register"
    assert "irs_federal_register" in action["title"]


async def test_finding_fires_on_the_no_new_entries_path(monkeypatch):
    """The only path a dead feed can reach — health must be checked BEFORE it.

    If the health check sat after the early return, it would be unreachable in
    precisely the situation it exists to detect.
    """
    captured: list[dict] = []

    async def _fake_action(_ctx, **kw):
        captured.append(kw)
        return {"ok": True}

    async def _report(_session, **kw):
        return [{
            "source": "irs_newsroom", "last_attempt_at": NOW,
            "last_success_at": None, "consecutive_failures": 12,
            "last_error": "404", "last_entry_count": 0,
            "hours_since_success": None, "never_succeeded": True, "stale": True,
        }]

    async def _no_entries(_session):
        return []

    monkeypatch.setattr(regulatory_updates, "execute_action", _fake_action)
    monkeypatch.setattr(regulatory_updates, "feed_health_report", _report)
    monkeypatch.setattr(regulatory_updates, "_fetch_unclassified", _no_entries)

    result = await regulatory_updates.run(_ctx(_FakeSession()))

    assert len(captured) == 1
    assert result["findings"] == 1
    assert "STALE" in result["summary"]


async def test_healthy_feeds_raise_nothing(monkeypatch):
    async def _fake_action(_ctx, **kw):
        raise AssertionError("must not fire for a healthy feed")

    async def _report(_session, **kw):
        return [{
            "source": "irs_federal_register", "last_attempt_at": NOW,
            "last_success_at": NOW, "consecutive_failures": 0,
            "last_error": None, "last_entry_count": 40,
            "hours_since_success": 0.2, "never_succeeded": False, "stale": False,
        }]

    monkeypatch.setattr(regulatory_updates, "execute_action", _fake_action)
    monkeypatch.setattr(regulatory_updates, "feed_health_report", _report)

    assert await regulatory_updates._check_feed_health(_ctx(_FakeSession())) == {
        "findings": 0, "actions": 0,
    }


async def test_one_finding_per_source_so_they_cannot_mask_each_other(monkeypatch):
    captured: list[dict] = []

    async def _fake_action(_ctx, **kw):
        captured.append(kw)
        return {"ok": True}

    async def _report(_session, **kw):
        return [
            {"source": "irs_federal_register", "last_attempt_at": NOW,
             "last_success_at": None, "consecutive_failures": 5, "last_error": "x",
             "last_entry_count": 0, "hours_since_success": None,
             "never_succeeded": True, "stale": True},
            {"source": "scdor_notices", "last_attempt_at": NOW,
             "last_success_at": None, "consecutive_failures": 5, "last_error": "y",
             "last_entry_count": 0, "hours_since_success": None,
             "never_succeeded": True, "stale": True},
        ]

    monkeypatch.setattr(regulatory_updates, "execute_action", _fake_action)
    monkeypatch.setattr(regulatory_updates, "feed_health_report", _report)

    result = await regulatory_updates._check_feed_health(_ctx(_FakeSession()))

    assert result["findings"] == 2
    assert {c["dedupe_key"] for c in captured} == {
        "regulatory_feed_stale:irs_federal_register",
        "regulatory_feed_stale:scdor_notices",
    }


# ---------------------------------------------------------------------------
# The dead URL must not come back
# ---------------------------------------------------------------------------


def test_no_configured_url_points_at_the_dead_irs_rss_path():
    """Regression guard: irs.gov publishes no RSS — that path 404s.

    Asserts the CONFIGURED endpoints, not the source text: the module docstring
    quotes the dead URL on purpose, as the record of what went wrong.
    """
    configured = (
        regulatory_feeds.IRS_FEDERAL_REGISTER_API,
        regulatory_feeds.IRS_NEWSROOM_PAGE,
        regulatory_feeds.SCDOR_NOTICES_PAGE,
    )
    for url in configured:
        assert "news-releases-rss" not in url
        assert not url.rstrip("/").endswith("/rss")


def test_feed_registry_covers_the_replacement_sources():
    slugs = {slug for slug, _ in regulatory_feeds.FEEDS}
    assert slugs == {"irs_federal_register", "irs_newsroom", "scdor_notices"}
    # The IRS regulatory source is the Federal Register API (verified live).
    assert "federalregister.gov" in regulatory_feeds.IRS_FEDERAL_REGISTER_API
    assert "internal-revenue-service" in regulatory_feeds.IRS_FEDERAL_REGISTER_API
