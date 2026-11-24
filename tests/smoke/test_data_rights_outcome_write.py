"""AO-SW-4: the export job outcome write must surface, never log-and-succeed.

app/api/data_rights.py:200 swallowed the exception from writing the job
outcome with a bare `except Exception: pass`. If that write failed, the job
row in setup_progress kept its 'running' status FOREVER: the client polled a
job that had actually finished (or crashed) but whose outcome never landed.
The fix logs the failure at ERROR with the job id + the outcome it was
carrying, so the silent swallow is both surfaced and diagnosable.

Offline: the background task runs for real; record_job raises; the session
fakes are recording.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import app.api.data_rights as api
import app.data_rights.export as export_mod


class _FakeSession:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def begin(self) -> Any:
        @asynccontextmanager
        async def _tx():
            yield self

        return _tx()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> Any:
        raise AssertionError("unexpected SQL in outcome-write path")


@asynccontextmanager
async def _fake_workspace_session(session_factory: Any, workspace_id: Any):
    yield _FakeSession()


@asynccontextmanager
async def _fake_plain_session():
    yield _FakeSession()


async def test_failed_outcome_write_is_logged_not_silently_swallowed(
    monkeypatch, caplog
) -> None:
    """record_job raising must NOT leave _run_export quiet: the outcome (and
    the job id) must reach the ERROR log, and the function must not raise
    (it is a background task -- a raise there is invisible to everyone)."""
    wid, job_id = uuid4(), uuid4()

    async def fake_build(session: Any, workspace_id: Any, **kw: Any):
        return b"zip", export_mod.ExportResult(workspace_id=str(workspace_id))

    async def fake_store(data: bytes, workspace_id: Any, job_id: Any, **kw: Any):
        return ("bkt", "data-rights/x/exports/y.zip")

    async def fake_record_job(session: Any, workspace_id: Any, *, bucket: str, record: dict):
        raise RuntimeError("setup_progress is readonly")

    monkeypatch.setattr(api, "SessionLocal", _fake_plain_session)
    monkeypatch.setattr(api, "workspace_session", _fake_workspace_session)
    monkeypatch.setattr(export_mod, "build_export", fake_build)
    monkeypatch.setattr(export_mod, "store_export", fake_store)
    monkeypatch.setattr(api.retention_mod, "record_job", fake_record_job)

    with caplog.at_level("ERROR", logger="acctops.data_rights"):
        await api._run_export(wid, job_id, "owner-1", "acme")  # must not raise

    error_records = [r for r in caplog.records if r.levelno >= 40]
    assert error_records, "a failed outcome write must be logged at ERROR"
    joined = " ".join(r.getMessage() for r in error_records)
    assert str(job_id) in joined
    assert "setup_progress is readonly" in joined
    # It was the COMPLETED outcome that was lost: the log must say so.
    assert "completed" in joined
