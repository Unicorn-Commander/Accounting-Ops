"""Smoke coverage for the web "Run drafts" launcher (app/api/tax_run_launch.py).

No live stack, no model call. The HTTP tests use the REAL role floor
(``require_role``) and REAL entity-scope check (``assert_entity_in_scope``)
against a real ``WorkspaceContext``; only the DB helpers are swapped for an
in-memory fake that keeps the same one-active-run contract. The worker tests
run ``execute_run`` against a stand-in coordinator script, so the subprocess,
process-group cancel, timeout, env allow-list and draft publishing are
exercised for real while no model is ever called.
"""
from __future__ import annotations

import asyncio
import json
import os
import textwrap
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.tax_run_launch as launch
from app.tenancy import WorkspaceContext, resolve_workspace

WS = uuid.uuid4()
ENTITY = uuid.uuid4()
OTHER_ENTITY = uuid.uuid4()
YEAR = 2024
FAKE_TOKEN = "svc-token-" + "x" * 40
FAKE_KEY = "sk-or-v1-" + "k" * 40


# ---------------------------------------------------------------------------
# In-memory stand-in for the specialist_run table
# ---------------------------------------------------------------------------


class FakeRuns:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.kinds: dict[uuid.UUID, str] = {ENTITY: "c_corp", OTHER_ENTITY: "c_corp"}
        self.spawned: list[tuple] = []

    def _active(self, eid: uuid.UUID, year: int) -> dict | None:
        for r in self.rows.values():
            if r["entity_id"] == str(eid) and r["tax_year"] == year and r["status"] in launch.ACTIVE_STATUSES:
                return r
        return None

    async def get_entity_kind(self, _wid, eid):
        return self.kinds.get(eid)

    async def create_queued(self, workspace, eid, year, *, model, endpoint_url):
        active = self._active(eid, year)
        if active:
            return None, active
        rid = str(uuid.uuid4())
        row = {
            "id": rid, "workspace_id": str(workspace.workspace_id), "entity_id": str(eid),
            "tax_year": year, "specialist_id": launch.SPECIALIST_ID,
            "agent_token_id": launch.LAUNCHER_AGENT_ID, "invoker_uc_uid": workspace.user_uc_uid,
            "backend": "openai", "model": model, "endpoint_url": endpoint_url, "status": "queued",
        }
        self.rows[rid] = row
        return dict(row), None

    async def get_active(self, _wid, eid, year):
        a = self._active(eid, year)
        return dict(a) if a else None

    async def get_run(self, _wid, rid):
        r = self.rows.get(str(rid))
        return dict(r) if r else None

    async def cancel(self, _wid, rid, _by):
        r = self.rows.get(str(rid))
        if r and r["agent_token_id"] == launch.LAUNCHER_AGENT_ID and r["status"] in launch.ACTIVE_STATUSES:
            r["status"] = "cancelled"
            return True
        return False

    def spawn(self, wid, eid, year, rid):
        self.spawned.append((wid, eid, year, rid))


@pytest.fixture()
def fake(monkeypatch) -> FakeRuns:
    f = FakeRuns()
    monkeypatch.setattr(launch, "db_get_entity_kind", f.get_entity_kind)
    monkeypatch.setattr(launch, "db_create_queued_run", f.create_queued)
    monkeypatch.setattr(launch, "db_get_active", f.get_active)
    monkeypatch.setattr(launch, "db_get_run", f.get_run)
    monkeypatch.setattr(launch, "db_cancel", f.cancel)
    monkeypatch.setattr(launch, "spawn_worker", f.spawn)
    monkeypatch.setattr(launch, "_service_token_blocker", lambda _wid: None)
    monkeypatch.setenv("AO_TAX_RUN_LAUNCH_ENABLED", "1")
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    for k in ("CPATEAM_LLM_ENDPOINT_URL", "CPATEAM_LLM_MODEL", "CPATEAM_LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    return f


def _client(role: str = "accountant", scope: tuple[uuid.UUID, ...] | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(launch.router)
    ctx = WorkspaceContext(
        workspace_id=WS, workspace_slug="ws", role=role, user_uc_uid=f"user-{role}",
        agent_id=None, entity_scope=scope, hipaa_protected=False,
    )
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    return TestClient(app)


def _launch_url(eid: uuid.UUID = ENTITY, year: int = YEAR) -> str:
    return f"/api/workspaces/{WS}/entities/{eid}/tax/{year}/specialist-runs/launch"


# ---------------------------------------------------------------------------
# Role floor + entity scope (BOLA)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["viewer", "client_user", "bookkeeper"])
def test_below_accountant_cannot_launch_or_preview(fake, role):
    c = _client(role)
    assert c.post(_launch_url()).status_code == 403
    assert c.get(_launch_url()).status_code == 403
    assert fake.spawned == []


@pytest.mark.parametrize("role", ["accountant", "cpa", "admin", "owner"])
def test_accountant_and_above_can_launch(fake, role):
    r = _client(role).post(_launch_url())
    assert r.status_code == 202, r.text
    run = r.json()["run"]
    assert run["status"] == "queued"
    assert run["agent_token_id"] == launch.LAUNCHER_AGENT_ID
    assert run["invoker_uc_uid"] == f"user-{role}"
    assert "endpoint_url" not in run
    assert len(fake.spawned) == 1


def test_entity_outside_member_scope_is_403(fake):
    r = _client("accountant", scope=(OTHER_ENTITY,)).post(_launch_url(ENTITY))
    assert r.status_code == 403
    assert fake.spawned == []


def test_scoped_member_cannot_launch_even_in_scope(fake):
    """The runner's service identity reaches the whole workspace; a member
    limited to some entities must not borrow that reach."""
    r = _client("accountant", scope=(ENTITY,)).post(_launch_url(ENTITY))
    assert r.status_code == 403
    assert "workspace-wide" in r.json()["detail"]
    assert fake.spawned == []


def test_unknown_entity_is_404(fake):
    r = _client().post(_launch_url(uuid.uuid4()))
    assert r.status_code == 404


def test_non_c_corp_is_422(fake):
    fake.kinds[ENTITY] = "s_corp"
    r = _client().post(_launch_url())
    assert r.status_code == 422
    assert fake.spawned == []


def test_future_year_is_400(fake):
    assert _client().post(_launch_url(year=2099)).status_code == 400


# ---------------------------------------------------------------------------
# Off switch + configuration
# ---------------------------------------------------------------------------


def test_disabled_by_default(fake, monkeypatch):
    monkeypatch.delenv("AO_TAX_RUN_LAUNCH_ENABLED")
    r = _client().post(_launch_url())
    assert r.status_code == 503
    assert "AO_TAX_RUN_LAUNCH_ENABLED" in r.json()["detail"]
    assert fake.spawned == []


def test_missing_model_key_blocks_launch(fake, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY")
    r = _client().post(_launch_url())
    assert r.status_code == 503
    assert fake.spawned == []


def test_service_token_blocker_real(monkeypatch):
    import app.auth as auth

    monkeypatch.delenv("ACCTOPS_SERVICE_TOKEN", raising=False)
    assert "unset" in launch._service_token_blocker(WS)
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKEN", FAKE_TOKEN)
    monkeypatch.setattr(auth, "_match_service_token", lambda _t: None)
    assert "not registered" in launch._service_token_blocker(WS)
    monkeypatch.setattr(auth, "_match_service_token", lambda _t: {"uc_uid": "svc", "workspace_ids": [uuid.uuid4()]})
    assert "not scoped" in launch._service_token_blocker(WS)
    monkeypatch.setattr(auth, "_match_service_token", lambda _t: {"uc_uid": "svc", "workspace_ids": [WS]})
    assert launch._service_token_blocker(WS) is None


def test_preview_describes_route_and_leaks_no_secret(fake):
    r = _client().get(_launch_url())
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["provider"].startswith("OpenRouter")
    assert body["third_party"] is True
    assert body["model"]
    assert body["data_sent"] and body["specialists"]
    assert body["active_run"] is None
    raw = r.text
    assert FAKE_KEY not in raw and FAKE_TOKEN not in raw
    assert "openrouter.ai/api" not in raw  # endpoint URL itself is not echoed


def test_internal_gateway_is_not_named():
    label, third = launch._provider_label("http://litellm.internal:4000/v1")
    assert third is False
    assert "litellm" not in label


# ---------------------------------------------------------------------------
# One active run per entity + year, cancel
# ---------------------------------------------------------------------------


def test_second_launch_same_entity_year_is_409(fake):
    c = _client()
    first = c.post(_launch_url())
    assert first.status_code == 202
    second = c.post(_launch_url())
    assert second.status_code == 409
    assert second.json()["detail"]["active_run_id"] == first.json()["run"]["id"]
    # A different year or entity is independent.
    assert c.post(_launch_url(year=YEAR - 1)).status_code == 202
    assert c.post(_launch_url(OTHER_ENTITY)).status_code == 202
    assert len(fake.spawned) == 3


def test_preview_reports_active_run(fake):
    c = _client()
    rid = c.post(_launch_url()).json()["run"]["id"]
    assert c.get(_launch_url()).json()["active_run"]["id"] == rid


def test_cancel_then_relaunch(fake):
    c = _client()
    rid = c.post(_launch_url()).json()["run"]["id"]
    r = c.post(f"/api/workspaces/{WS}/specialist-runs/{rid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    again = c.post(f"/api/workspaces/{WS}/specialist-runs/{rid}/cancel")
    assert again.status_code == 409
    assert c.post(_launch_url()).status_code == 202


def test_cancel_role_scope_and_ownership(fake):
    rid = _client().post(_launch_url()).json()["run"]["id"]
    cancel = f"/api/workspaces/{WS}/specialist-runs/{rid}/cancel"
    assert _client("viewer").post(cancel).status_code == 403
    assert _client("accountant", scope=(OTHER_ENTITY,)).post(cancel).status_code == 403
    assert _client().post(f"/api/workspaces/{WS}/specialist-runs/{uuid.uuid4()}/cancel").status_code == 404
    # An externally started run cannot be cancelled from the app.
    fake.rows[rid]["agent_token_id"] = "receipt-reconciler"
    assert _client().post(cancel).status_code == 409


# ---------------------------------------------------------------------------
# The GUI request really dispatches the C-corp trio, and the argv is valid
# ---------------------------------------------------------------------------


def test_request_text_dispatches_full_trio():
    from agents.tax_specialists.coordinator.dispatch_plan import build_dispatch_plan

    plan = build_dispatch_plan(
        user_request=launch._request_text(YEAR), entity_id=str(ENTITY),
        tax_year=YEAR, workspace_id=str(WS),
    )
    assert plan.intent == "full_trio"
    assert [s.display_name for s in plan.available_steps()] == list(launch._SPECIALISTS)


def test_coordinator_accepts_the_launch_argv(tmp_path):
    from agents.tax_specialists.coordinator import run_coordinator

    argv = launch._coordinator_cmd(WS, ENTITY, YEAR, tmp_path)[2:]
    args = run_coordinator._parse_args(argv)
    assert args.backend == "openai"
    assert args.output_dir == tmp_path
    assert args.tax_year == YEAR


def test_scrub_removes_configured_secrets(monkeypatch):
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    out = launch._scrub(f"401 with {FAKE_TOKEN} and {FAKE_KEY} Bearer abcdefghijklmnopqrstuvwxyz")
    assert FAKE_TOKEN not in out and FAKE_KEY not in out
    assert "abcdefghijklmnopqrstuvwxyz" not in out


# ---------------------------------------------------------------------------
# Worker — real subprocess, stand-in coordinator, no model call
# ---------------------------------------------------------------------------

_FAKE_COORDINATOR = textwrap.dedent(
    """
    import argparse, json, os, pathlib, subprocess, sys, time
    p = argparse.ArgumentParser()
    for a in ("--workspace-id", "--entity-id", "--tax-year", "--request", "--output-dir", "--backend"):
        p.add_argument(a)
    a = p.parse_args()
    out = pathlib.Path(a.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "env.json").write_text(json.dumps(sorted(os.environ)))
    (out / "argv.json").write_text(json.dumps(sys.argv[1:]))
    mode = pathlib.Path(out, "..", "..", "mode").resolve().read_text().strip()
    if mode == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        (out / "child.pid").write_text(str(child.pid))
        time.sleep(120)
    if mode == "fail":
        print("[coordinator]   Form 1120 Preparer: ERROR - 401 from gateway", file=sys.stderr)
        print("[coordinator] Summary: 0 complete, 2 error(s), 0 gap(s), 0 total CPA flag(s)")
        print("  [ERROR] Form 1120 Preparer: 401 from gateway")
        sys.exit(0)
    ent = a.entity_id[:8]
    for prefix in ("form-1120", "1099-spec", "sc-state", "coordinator"):
        (out / f"{prefix}-{ent}-{a.tax_year}-20260924-000000.md").write_text(f"DRAFT {prefix}")
    print("[coordinator] Summary: 3 complete, 0 error(s), 0 gap(s), 2 total CPA flag(s)")
    """
)


class WorkerDB:
    def __init__(self) -> None:
        self.status = "queued"
        self.finish: dict | None = None

    async def mark_running(self, _wid, _rid):
        if self.status != "queued":
            return False
        self.status = "running"
        return True

    async def get_status(self, _wid, _rid):
        return self.status

    async def finish_(self, _wid, _rid, **kw):
        if self.status not in ("queued", "running"):
            return False
        self.status = kw["new_status"]
        self.finish = kw
        return True


@pytest.fixture()
def worker(tmp_path, monkeypatch):
    script = tmp_path / "fake_coordinator.py"
    script.write_text(_FAKE_COORDINATOR)
    drafts = tmp_path / "drafts"
    monkeypatch.setattr(launch, "_COORDINATOR", script)
    monkeypatch.setenv("DRAFTS_DIR", str(drafts))
    monkeypatch.setattr(launch, "_POLL_S", 0.2)
    monkeypatch.setattr(launch, "_semaphore", None)
    db = WorkerDB()
    monkeypatch.setattr(launch, "db_mark_running", db.mark_running)
    monkeypatch.setattr(launch, "db_get_status", db.get_status)
    monkeypatch.setattr(launch, "db_finish", db.finish_)
    # Secrets / unrelated env the child must NOT inherit.
    monkeypatch.setenv("ACCTOPS_SERVICE_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("LLM_API_KEY", "vision-key-should-not-leak")
    entity_dir = drafts / str(WS) / str(ENTITY)
    entity_dir.mkdir(parents=True)

    def set_mode(mode: str) -> None:
        (entity_dir / "mode").write_text(mode)

    return db, entity_dir, set_mode


def _run(rid: str) -> None:
    asyncio.run(launch.execute_run(WS, ENTITY, YEAR, rid))


def test_worker_success_publishes_canonical_drafts(worker):
    db, entity_dir, set_mode = worker
    set_mode("ok")
    (entity_dir / f"{YEAR}-form-1120-draft.md").write_text("REVIEWED EARLIER")
    rid = str(uuid.uuid4())
    _run(rid)

    assert db.status == "completed", db.finish
    summary = db.finish["output_summary"]
    assert summary["specialists_complete"] == 3
    assert summary["published_drafts"] == ["1099-nec", "coordinator-summary", "form-1120", "sc1120"]
    assert (entity_dir / f"{YEAR}-form-1120-draft.md").read_text() == "DRAFT form-1120"
    assert (entity_dir / f"{YEAR}-sc1120-draft.md").read_text() == "DRAFT sc-state"
    # The earlier canonical draft was kept, not overwritten.
    superseded = entity_dir / f"{YEAR}-form-1120-draft.superseded-{rid[:8]}.md"
    assert superseded.read_text() == "REVIEWED EARLIER"
    run_dir = entity_dir / "runs" / rid
    # Child env is the allow-list: no DB URL, no vision key, no Anthropic key.
    env_keys = set(json.loads((run_dir / "env.json").read_text()))
    assert {"ACCTOPS_SERVICE_TOKEN", "OPENROUTER_API_KEY"} <= env_keys
    assert not env_keys & {"DATABASE_URL", "ANTHROPIC_API_KEY", "LLM_API_KEY"}
    # No secret on the command line.
    argv = (run_dir / "argv.json").read_text()
    assert FAKE_TOKEN not in argv and FAKE_KEY not in argv
    assert json.loads(argv)[-2:] == ["--backend", "openai"]
    assert rid not in launch._LIVE_RUNS


def test_worker_no_specialist_completed_is_failed_and_scrubbed(worker, monkeypatch):
    db, entity_dir, set_mode = worker
    set_mode("fail")
    _run(str(uuid.uuid4()))
    assert db.status == "failed"
    assert "401 from gateway" in db.finish["error_summary"]
    assert not (entity_dir / f"{YEAR}-form-1120-draft.md").exists()


def test_worker_cancel_kills_process_group(worker):
    db, entity_dir, set_mode = worker
    set_mode("hang")
    rid = str(uuid.uuid4())

    async def scenario():
        task = asyncio.create_task(launch.execute_run(WS, ENTITY, YEAR, rid))
        pid_file = entity_dir / "runs" / rid / "child.pid"
        for _ in range(100):
            if pid_file.exists():
                break
            await asyncio.sleep(0.1)
        assert pid_file.exists(), "fake coordinator never started"
        db.status = "cancelled"  # what POST /cancel writes
        await asyncio.wait_for(task, timeout=20)
        return int(pid_file.read_text())

    child_pid = asyncio.run(scenario())
    assert db.status == "cancelled"
    assert db.finish is None  # the worker never overwrote the cancel
    # The specialist-like grandchild died with the group.
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived cancel")
    assert not (entity_dir / f"{YEAR}-form-1120-draft.md").exists()


def test_worker_timeout_marks_failed(worker, monkeypatch):
    db, _entity_dir, set_mode = worker
    set_mode("hang")
    monkeypatch.setattr(launch, "_max_runtime_s", lambda: 1)
    _run(str(uuid.uuid4()))
    assert db.status == "failed"
    assert "Timed out" in db.finish["error_summary"]


def test_worker_cancelled_while_queued_never_starts(worker):
    db, entity_dir, _set_mode = worker
    db.status = "cancelled"
    rid = str(uuid.uuid4())
    _run(rid)
    assert not (entity_dir / "runs" / rid).exists()


def test_worker_crash_records_failure(worker, monkeypatch):
    db, _e, set_mode = worker
    set_mode("ok")

    async def boom(*_a, **_k):
        raise OSError("exec format error")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    _run(str(uuid.uuid4()))
    assert db.status == "failed"
    assert "exec format error" in db.finish["error_summary"]


def test_route_registered_on_main_app():
    from app.main import app
    from tests.route_introspection import methods_for

    assert methods_for(app, launch._LAUNCH_PATH) == {"GET", "POST"}
    assert "POST" in methods_for(app, "/api/workspaces/{workspace_id}/specialist-runs/{run_id}/cancel")

