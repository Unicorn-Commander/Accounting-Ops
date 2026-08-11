"""The CI gate itself, pinned so it cannot go back to gating nothing.

The Forgejo `unit` job runs a BLOCKING `ruff check tests scripts` before
pytest. tests/ and scripts/ carried 46 violations, so that job could only ever
die at the lint step — the suite underneath it never ran, on any branch, for
as long as the gate has existed. The lint gate is worth keeping; a
permanently-red one is not, so the clean state is asserted here and a
reintroduced violation now costs one visible test instead of the whole suite.

The rest pin the invocation and the trigger: the job ran `pytest tests/smoke`,
which left tests/unit and the stack-free tests under tests/integration wired to
no CI step at all, `make test` said something different again, and pushes to
`fix/**` triggered no workflow.
"""
from __future__ import annotations

import pathlib
import re
import shlex
import subprocess
import sys

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".forgejo" / "workflows" / "ci.yml"
MAKEFILE = REPO_ROOT / "Makefile"

LINT_TARGETS = ("tests", "scripts")


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def _run_lines(step: dict) -> list[str]:
    return [ln.strip() for ln in step.get("run", "").splitlines() if ln.strip()]


def _pytest_invocations(steps: list[dict]) -> list[list[str]]:
    """Every `pytest ...` command line in the steps, as argv without the program."""
    found = []
    for step in steps:
        for line in _run_lines(step):
            argv = shlex.split(line)
            if not argv:
                continue
            if argv[0] == "pytest":
                found.append(argv[1:])
            elif argv[0].startswith("python") and argv[1:3] == ["-m", "pytest"]:
                found.append(argv[3:])
    return found


def _paths(argv: list[str]) -> list[str]:
    """The positional arguments — i.e. the test paths the invocation pins."""
    return [arg for arg in argv if not arg.startswith("-")]


def test_lint_gate_is_clean():
    """`ruff check tests scripts` is the first blocking step — it must exit 0."""
    pytest.importorskip("ruff", reason="ruff is in the [dev] extra CI installs")
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", *LINT_TARGETS],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "the blocking CI lint step fails, so nothing after it runs:\n"
        + proc.stdout
        + proc.stderr
    )


def test_lint_gate_is_still_blocking(workflow: dict):
    """A green gate is only useful while it can still fail the job."""
    lint = [
        line
        for step in _steps(workflow, "unit")
        for line in _run_lines(step)
        if line.startswith("ruff check") and not line.startswith("ruff check app")
    ]
    assert lint == [f"ruff check {' '.join(LINT_TARGETS)}"], lint
    assert "||" not in lint[0], "the test/script lint gate must not swallow its exit code"


def test_ci_runs_every_test_that_needs_no_stack(workflow: dict):
    """No path argument — `testpaths` in pyproject.toml decides what runs.

    Naming tests/smoke here is what left tests/unit and the stack-free tests
    under tests/integration running nowhere.
    """
    invocations = _pytest_invocations(_steps(workflow, "unit"))
    assert len(invocations) == 1, invocations
    assert _paths(invocations[0]) == [], (
        f"the no-stack job is path-restricted to {_paths(invocations[0])}"
    )


def test_make_test_runs_what_ci_runs(workflow: dict):
    """`make test` is what a human runs before pushing; it must not disagree."""
    recipe = re.search(r"^test:\n\t(.+)$", MAKEFILE.read_text(), re.MULTILINE)
    assert recipe, "no `test` target in the Makefile"
    argv = shlex.split(recipe.group(1))
    assert argv[0] == "$(PYTEST)", argv
    assert _paths(argv[1:]) == [], f"`make test` is path-restricted to {_paths(argv[1:])}"
    assert argv[1:] == _pytest_invocations(_steps(workflow, "unit"))[0]


def test_fix_branches_trigger_ci(workflow: dict):
    """Fixes land on fix/** in this repo — an untriggered branch is unreviewed."""
    # `on` parses as the YAML boolean True, not the string "on".
    triggers = workflow.get("on", workflow.get(True))
    assert "fix/**" in triggers["push"]["branches"]
