"""Data-rights export job: the background writer must fail the job row when
the object store is absent, instead of leaving it 'running' forever.

Kit F84 (PR #98 lane): ``_run_export`` only reached its failed branch when
build_export or store_export RAISED. store_export handed the archive bytes to
GarageS3Client, which raises GarageNotConfigured when the S3 credentials are
blank -- but the failure mode reported in the sweep was the job row staying
``running`` indefinitely, so the client polled a job that would never finish.
The acceptance bar: the job row ends ``failed`` with a clear error when
object storage is not configured, and store_export itself refuses to touch
the network in that state (no in-memory fallback, no silent success).

Offline: build_export / store_export / record_job are faked; the background
task runs for real against a recording fake.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest

import app.api.data_rights as api
import app.data_rights.export as export_mod
from app.clients.garage_s3 import GarageNotConfigured


class _Result:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def mappings(self) -> "_Result":
        return self

    def first(self) -> dict | None:
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Answers the read_data_rights_doc SELECT with an empty doc and records
    the record_job UPDATE. session.begin() is a no-op context manager, which
    is what record_job (called inside `async with session.begin()`) needs."""

    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    def begin(self) -> Any:
        @asynccontextmanager
        async def _tx():
            yield self

        return _tx()

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        if sql.strip().upper().startswith("SELECT"):
            return _Result([{"setup_progress": {}}])
        if "UPDATE" in sql.upper():
            assert params is not None
            self.updates.append(params)
        return _Result([])


@asynccontextmanager
async def _fake_workspace_session(session_factory: Any, workspace_id: Any):
    yield _FakeSession()


@asynccontextmanager
async def _fake_plain_session():
    yield _FakeSession()


def _result() -> Any:
    return export_mod.ExportResult(workspace_id=str(uuid4()))


async def test_store_export_fails_fast_when_object_store_absent() -> None:
    """store_export must raise before any network attempt when S3 is unconfigured.

    The blank-credential Settings are the real 'not configured' state
    (is_configured() is False); the exception is the type the download route
    already maps to 503, and it carries a clear message, not the archive.
    """
    from app.config import Settings

    unconfigured = Settings(
        garage_s3_endpoint="",
        garage_s3_access_key="",
        garage_s3_secret_key="",
        garage_bucket_prefix="accounting-ops",
    )
    with pytest.raises(GarageNotConfigured) as excinfo:
        await export_mod.store_export(
            b"zip-bytes", uuid4(), uuid4(), settings=unconfigured
        )
    assert "not configured" in str(excinfo.value)


async def test_run_export_writes_failed_row_when_store_absent(monkeypatch) -> None:
    """The background job must end 'failed' (not stay 'running') when the
    object store is absent, and the archive bytes never become the error."""
    wid, job_id = uuid4(), uuid4()
    archive = b"zip-archive-bytes-must-not-leak-into-the-row"

    async def fake_store(data: bytes, workspace_id: Any, job_id: Any, **kw: Any):
        raise GarageNotConfigured("Object storage (Garage S3) is not configured")

    async def fake_build(session: Any, workspace_id: Any, **kw: Any):
        return archive, _result()

    jobs: list[dict] = []

    async def fake_record_job(session: Any, workspace_id: Any, *, bucket: str, record: dict):
        jobs.append(dict(record))

    monkeypatch.setattr(api, "SessionLocal", _fake_plain_session)
    monkeypatch.setattr(api, "workspace_session", _fake_workspace_session)
    monkeypatch.setattr(export_mod, "build_export", fake_build)
    monkeypatch.setattr(export_mod, "store_export", fake_store)
    monkeypatch.setattr(api.retention_mod, "record_job", fake_record_job)

    await api._run_export(wid, job_id, "owner-1", "acme")

    assert len(jobs) == 1
    record = jobs[0]
    assert record["status"] == "failed"
    assert "not configured" in record["error"]
    # The archive must not be stashed anywhere in the job row.
    assert record.get("bucket") is None
    assert record.get("object_key") is None
    assert archive.decode("latin1") not in str(record)
    assert "finished_at" in record


async def test_run_export_writes_completed_row_when_store_present(monkeypatch) -> None:
    """Control: a configured store still completes the job and records the key."""
    wid, job_id = uuid4(), uuid4()

    async def fake_store(data: bytes, workspace_id: Any, job_id: Any, **kw: Any):
        assert data == b"zip"
        return ("bkt", f"data-rights/{workspace_id}/exports/{job_id}/x.zip")

    async def fake_build(session: Any, workspace_id: Any, **kw: Any):
        return b"zip", _result()

    jobs: list[dict] = []

    async def fake_record_job(session: Any, workspace_id: Any, *, bucket: str, record: dict):
        jobs.append(dict(record))

    monkeypatch.setattr(api, "SessionLocal", _fake_plain_session)
    monkeypatch.setattr(api, "workspace_session", _fake_workspace_session)
    monkeypatch.setattr(export_mod, "build_export", fake_build)
    monkeypatch.setattr(export_mod, "store_export", fake_store)
    monkeypatch.setattr(api.retention_mod, "record_job", fake_record_job)

    await api._run_export(wid, job_id, "owner-1", "acme")

    assert len(jobs) == 1
    record = jobs[0]
    assert record["status"] == "completed"
    assert record["bucket"] == "bkt"
    assert record["object_key"] is not None
