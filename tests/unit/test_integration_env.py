from __future__ import annotations

import pytest

from scripts import integration_env


def _isolate_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(integration_env, "ENV_FILE", tmp_path / ".env.integration")
    monkeypatch.setattr(
        integration_env, "SECRET_FILE", tmp_path / "secrets" / "accounting_ops_master_key"
    )
    monkeypatch.delenv("AO_ITEST_PROJECT", raising=False)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    monkeypatch.delenv("FORGEJO_RUN_ID", raising=False)


def test_project_name_uses_explicit_override_and_allows_long_lived_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
):
    _isolate_project_state(monkeypatch, tmp_path)
    monkeypatch.setenv("AO_ITEST_PROJECT", "acctops_itest")

    assert integration_env.resolve_project_name(for_up=True) == "acctops_itest"


def test_project_name_uses_ci_run_id(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _isolate_project_state(monkeypatch, tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")

    assert integration_env.resolve_project_name(for_up=True) == "acctops_itest_12345"


def test_project_name_uses_local_pid_and_random_suffix(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _isolate_project_state(monkeypatch, tmp_path)
    monkeypatch.setattr(integration_env.os, "getpid", lambda: 4242)
    monkeypatch.setattr(integration_env.secrets, "token_hex", lambda _: "abcd")

    assert integration_env.resolve_project_name(for_up=True) == "acctops_itest_4242_abcd"


def test_project_name_is_reused_from_generated_env(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _isolate_project_state(monkeypatch, tmp_path)
    integration_env.ENV_FILE.write_text(
        "AO_ITEST_PROJECT=acctops_itest_saved\nCOMPOSE_PROJECT_NAME=acctops_itest_saved\n"
    )

    assert integration_env.resolve_project_name() == "acctops_itest_saved"


def test_run_tears_down_after_fake_pytest_failure(monkeypatch: pytest.MonkeyPatch, tmp_path):
    _isolate_project_state(monkeypatch, tmp_path)
    monkeypatch.setattr(integration_env, "ROOT", tmp_path)
    monkeypatch.setenv("AO_ITEST_PROJECT", "acctops_itest_fake")
    monkeypatch.setattr(integration_env, "CORE_SERVICES", [])

    calls: list[list[str]] = []

    class Completed:
        def __init__(self, returncode: int = 0, stdout: str = ""):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **_kwargs):
        calls.append(cmd)
        if "pytest" in cmd:
            return Completed(returncode=17)
        return Completed()

    monkeypatch.setattr(integration_env.subprocess, "run", fake_run)

    assert integration_env.cmd_run(False, False, ["-q"]) == 17

    down_commands = [cmd for cmd in calls if "down" in cmd]
    assert len(down_commands) == 1
    assert down_commands[0][3] == "acctops_itest_fake"


def test_run_keep_skips_teardown(monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []
    monkeypatch.setattr(integration_env, "cmd_up", lambda _with_cpa: events.append("up"))
    monkeypatch.setattr(integration_env, "cmd_test", lambda _args, _with_cpa: events.append("test") or 0)
    monkeypatch.setattr(integration_env, "cmd_down", lambda: events.append("down"))

    assert integration_env.cmd_run(False, True, ["-q"]) == 0
    assert events == ["up", "test"]
