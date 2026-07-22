"""Mock-fixture tests for scripts/audit_gateway_consumers.sh.

No real SSH, no live gateway. The script is exercised against
tests/fixtures/gateway_consumers/ which contains:

  * healthy consumers with non-blank LITELLM/GATEWAY key vars
  * blank-key-service with LITELLM_API_KEY=  (must fail the audit)

These tests pin the flip-day checklist generator so a regression cannot
silently stop flagging blank keys.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import textwrap

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "audit_gateway_consumers.sh"
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "gateway_consumers"


@pytest.fixture(scope="module", autouse=True)
def _script_is_executable():
    assert SCRIPT.is_file(), f"missing audit script: {SCRIPT}"
    # Ensure +x even on a fresh checkout that lost the mode bit.
    mode = SCRIPT.stat().st_mode
    if not (mode & 0o111):
        SCRIPT.chmod(mode | 0o755)


def _run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SCRIPT), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=check,
    )


def test_fixture_tree_exists():
    assert FIXTURE_ROOT.is_dir()
    assert (FIXTURE_ROOT / "blank-key-service" / ".env.example").is_file()
    assert (FIXTURE_ROOT / "accounting-ops" / "docker-compose.yml").is_file()


def test_full_fixture_flags_blank_key_service():
    """Default consumer map includes blank-key-service → exit 1 + BLANK row."""
    proc = _run("--fixture-root", str(FIXTURE_ROOT))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SERVICE" in proc.stdout
    assert "blank-key-service" in proc.stdout
    assert "BLANK" in proc.stdout
    # Must never echo secret material from healthy fixtures either.
    assert "sk-fixture-" not in proc.stdout
    assert "sk-fixture-" not in proc.stderr


def test_healthy_subset_exits_zero(tmp_path):
    """A map of only PRESENT consumers must exit 0."""
    consumer_map = tmp_path / "healthy.tsv"
    consumer_map.write_text(
        textwrap.dedent(
            """\
            accounting-ops\taccounting-ops
            email-ops\temail-ops
            contact-ops\tcontact-ops
            """
        )
    )
    proc = _run(
        "--fixture-root",
        str(FIXTURE_ROOT),
        "--map",
        str(consumer_map),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PRESENT" in proc.stdout
    assert "RESULT: OK" in proc.stdout
    assert "BLANK" not in proc.stdout


def test_missing_dir_is_failure(tmp_path):
    consumer_map = tmp_path / "missing.tsv"
    consumer_map.write_text("ghost-service\tghost-service-dir-does-not-exist\n")
    proc = _run("--fixture-root", str(FIXTURE_ROOT), "--map", str(consumer_map))
    assert proc.returncode == 1
    assert "MISSING_DIR" in proc.stdout


def test_script_help_exits_2():
    proc = _run("--help")
    assert proc.returncode == 2
    assert "audit_gateway_consumers" in proc.stdout or "Usage" in proc.stdout


def test_default_root_is_fixture_when_unspecified():
    """Bare invocation (no --root) must use the in-repo fixture, not SSH."""
    env = os.environ.copy()
    # Ensure no accidental override.
    env.pop("GATEWAY_AUDIT_ROOT", None)
    proc = subprocess.run(
        [str(SCRIPT)],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        env=env,
    )
    # Fixture includes blank-key-service → non-zero is expected and proves
    # the default root resolved to the mock tree (not an empty/SSH path).
    assert "blank-key-service" in proc.stdout
    assert str(FIXTURE_ROOT) in proc.stdout or "fixtures/gateway_consumers" in proc.stdout
