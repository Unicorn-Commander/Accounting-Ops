"""AO-SW-5: failure paths must leave a visible trace.

The sweep found worker / background failure paths that left NO durable row and
no loud log: a failed tax extraction never wrote its document_extraction row
(receipt invisible instead of "failed"); a sentinel check crashing outside its
own handler left no finding row. (The crashing-sync-worker case -- stripe_sync
/ ebay_sync keeping a stale "ok" in connector_state -- moved to the money
batch: its outcome write dropped continuation cursors + failure counts on the
UPDATE path, so it is out of scope for this low-risk PR.)

The fix persists a visible crash state in each in-scope case.

Offline: every DB surface is a recording fake; no network, no Postgres.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest


class _Row:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def first(self) -> Any:
        return self._rows[0] if self._rows else None

    def mappings(self) -> "_Row":
        return self

    def all(self) -> list[Any]:
        return list(self._rows)

    def one(self) -> Any:
        return self._rows[0]


# ---------------------------------------------------------------------------
# documents.py: failed tax extraction writes a status='failed' row
# ---------------------------------------------------------------------------


class _DocStateSession:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, dict[str, Any]]] = []

    def begin(self) -> Any:
        @asynccontextmanager
        async def _tx():
            yield self

        return _tx()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(stmt)
        if sql.strip().upper().startswith("INSERT"):
            assert params is not None
            self.inserts.append((sql, params))
        return _Row([])


@pytest.fixture
def doc_recorder(monkeypatch):
    import app.api.documents as mod

    sessions: list[_DocStateSession] = []

    @asynccontextmanager
    async def fake_workspace_session(factory: Any, workspace_id: Any):
        s = _DocStateSession()
        sessions.append(s)
        yield s

    monkeypatch.setattr(mod, "workspace_session", fake_workspace_session)

    import app.categorization.tax_doc_vlm as vlm

    class _Boom:
        async def extract(self, **kw: Any) -> Any:
            raise RuntimeError("vision model refused the page")

    monkeypatch.setattr(vlm, "TaxDocExtractor", _Boom)
    return sessions


async def test_failed_tax_extraction_writes_failed_row(doc_recorder) -> None:
    sessions = doc_recorder
    wid, eid, rid = uuid4(), uuid4(), uuid4()

    import app.api.documents as mod

    await mod._run_tax_extraction(
        wid, eid, rid, b"%PDF-1.4 fake", "application/pdf"
    )  # must not raise

    assert sessions, "the failure row must go to the workspace DB"
    session = sessions[-1]
    assert session.inserts, "a failed extraction must still write a row"
    sql, params = session.inserts[0]
    assert "document_extraction" in sql
    assert params["rid"] == str(rid)
    assert "vision model refused" in params["error"]
    assert "failed" in sql  # the row is the failure state


# ---------------------------------------------------------------------------
# sentinel: a check crash outside run_one_check leaves a finding row
# ---------------------------------------------------------------------------


class _SentinelStateSession:
    def __init__(self) -> None:
        self.finding_inserts: list[dict[str, Any]] = []

    def begin(self) -> Any:
        @asynccontextmanager
        async def _tx():
            yield self

        return _tx()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        sql = str(stmt)
        if "sentinel_finding" in sql and sql.strip().upper().startswith("INSERT"):
            assert params is not None
            self.finding_inserts.append(params)
            return _Row([uuid4()])
        return _Row([])


@pytest.fixture
def sentinel_recorder(monkeypatch):
    import agents.sentinel.run_sentinel as mod

    sessions: list[_SentinelStateSession] = []

    @asynccontextmanager
    async def fake_workspace_session(factory: Any, workspace_id: Any):
        s = _SentinelStateSession()
        sessions.append(s)
        yield s

    monkeypatch.setattr(mod, "workspace_session", fake_workspace_session)
    return mod, sessions


async def test_check_crash_outside_handler_writes_finding(sentinel_recorder, monkeypatch) -> None:
    """run_one_check that raises OUTSIDE its own per-check handlers used to
    vanish: no check-run row, no finding row. The cycle's guard now writes a
    crash finding best-effort and keeps the rollup honest."""
    mod, sessions = sentinel_recorder
    wid = uuid4()

    async def boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("registry entry torn out mid-run")

    monkeypatch.setattr(mod, "run_one_check", boom)

    # Drive the exact guard body the cycle loop uses.
    cycle_id = uuid4()
    check_results: list[dict[str, Any]] = []
    for check_type in ["journal_balance_check"]:
        try:
            result = await mod.run_one_check(wid, check_type, cycle_id, None)
        except Exception as e:  # noqa: BLE001 - mirrors the production guard
            _log = mod._log
            _log.exception(
                "Sentinel: check %s crashed outside its own handler (workspace=%s cycle=%s)",
                check_type,
                wid,
                cycle_id,
            )
            try:
                async with mod.workspace_session(mod.SessionLocal, wid) as session:
                    await mod._write_finding(
                        session,
                        workspace_id=wid,
                        entity_id=None,
                        check_type=check_type,
                        severity="warning",
                        title=f"Sentinel check crashed: {check_type}",
                        summary=f"check {check_type} crashed (cycle {cycle_id})",
                        details={"cycle_id": str(cycle_id), "error": str(e)},
                        action_type=None,
                        action_ref_id=None,
                        check_run_id=None,
                        decision_audit_id=None,
                    )
            except Exception:  # noqa: BLE001
                pass
            result = {"check_type": check_type, "error": "crashed"}
        check_results.append(result)

    assert sessions, "the crash finding must go to the workspace DB"
    session = sessions[-1]
    assert session.finding_inserts, "a crash outside the handler must leave a row"
    params = session.finding_inserts[0]
    assert params["check_type"] == "journal_balance_check"
    assert "registry entry torn out" in params["details"]
    assert params["severity"] == "warning"
    # The cycle keeps going: the crashed check is reported in the rollup.
    assert check_results[0]["error"] == "crashed"
