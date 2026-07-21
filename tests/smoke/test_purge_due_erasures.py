"""Pure unit tests for the deferred-erasure purge runner — no DB / no network.

The safety properties that must hold regardless of deployment:

  * The kill switch (AO_ERASURE_PURGE_ENABLED) defaults OFF and the runner
    REFUSES to run without it. A deployment must opt into destructive sweeps;
    it must never inherit them.
  * Only receipts whose ``ledger_purged`` step is still ``deferred`` are ever
    executed. ``done`` (idempotency), ``failed`` (operator case — never
    auto-retried), ``unavailable``, and malformed receipts are all refused.
  * A purge runs only once its ``ledger_purge_due_at`` has passed.
  * Applying a purge result flips the receipt out of ``deferred``, so a
    re-run refuses it — the idempotency latch.
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

# scripts/ is a top-level directory with NO __init__.py, so the standard
# package import won't find it. Load the module by file path instead (same
# pattern as test_mail_drainer_unit.py).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "purge_due_erasures.py"

NOW = dt.datetime(2026, 7, 20, 12, 0, 0, tzinfo=dt.timezone.utc)
PAST = "2026-07-01T00:00:00+00:00"
FUTURE = "2026-08-01T00:00:00+00:00"


@pytest.fixture
def purger(monkeypatch):
    """Load scripts/purge_due_erasures.py as a module from its file path.

    Registered in ``sys.modules`` before execution so ``@dataclass``
    annotations resolve cleanly (dataclasses looks the defining module up via
    ``sys.modules`` during field analysis).
    """
    import importlib.util

    mod_name = "purge_due_erasures_under_test"
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        sys.modules.pop(mod_name, None)


def _receipt(
    ledger_status: str = "deferred",
    due_at: str | None = PAST,
    access_status: str = "done",
) -> dict:
    """A receipt shaped exactly like ErasureReceipt.to_dict() stores it."""
    return {
        "workspace_id": "00000000-0000-0000-0000-000000000001",
        "requested_by_uc_uid": "owner-1",
        "requested_at": "2026-06-01T00:00:00+00:00",
        "overall_status": "deferred",
        "steps": [
            {"name": "credentials_purged", "status": "done"},
            {"name": "documents_purged", "status": "done"},
            {"name": "tax_identifiers_purged", "status": "done"},
            {"name": "access_revoked", "status": access_status},
            {"name": "memberships_removed", "status": access_status},
            {"name": "ledger_purged", "status": ledger_status, "detail": "scheduled"},
        ],
        "ledger_purge_due_at": due_at,
        "retention_days": 30,
    }


# --------------------------------------------------------------------------- #
# Kill switch — default OFF, explicit truthy required
# --------------------------------------------------------------------------- #
def test_kill_switch_defaults_off(purger):
    assert purger.purge_enabled({}) is False


@pytest.mark.parametrize("value", ["true", "TRUE", " True ", "1", "yes", "on", "ON"])
def test_kill_switch_accepts_explicit_truthy(purger, value):
    assert purger.purge_enabled({"AO_ERASURE_PURGE_ENABLED": value}) is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "banana", "enabled"])
def test_kill_switch_refuses_everything_else(purger, value):
    assert purger.purge_enabled({"AO_ERASURE_PURGE_ENABLED": value}) is False


def test_kill_switch_reads_process_env_by_default(purger, monkeypatch):
    monkeypatch.delenv("AO_ERASURE_PURGE_ENABLED", raising=False)
    assert purger.purge_enabled() is False
    monkeypatch.setenv("AO_ERASURE_PURGE_ENABLED", "true")
    assert purger.purge_enabled() is True


async def test_run_once_refuses_when_kill_switch_off(purger, monkeypatch):
    """The whole pass refuses — nothing considered, let alone purged."""
    monkeypatch.delenv("AO_ERASURE_PURGE_ENABLED", raising=False)
    summary = await purger.run_once()
    assert summary["refused"] == "kill_switch"
    assert summary["considered"] == 0
    assert summary["purged"] == 0


async def test_run_once_refuses_without_admin_connection(purger, monkeypatch):
    """ADMIN_DATABASE_URL gate: no owner role => refuse before touching work."""
    monkeypatch.setenv("AO_ERASURE_PURGE_ENABLED", "true")
    monkeypatch.setattr(
        purger.erasure_mod, "admin_connection_available", lambda: False
    )
    summary = await purger.run_once()
    assert summary["refused"] == "no_admin_url"
    assert summary["considered"] == 0
    assert summary["purged"] == 0


# --------------------------------------------------------------------------- #
# Non-deferred refusal — the idempotency / operator-case gate
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("receipt", [None, {}])
def test_missing_receipt_refuses(purger, receipt):
    decision = purger.evaluate_receipt(receipt, NOW)
    assert decision.should_purge is False
    assert "no erasure receipt" in decision.reason


def test_receipt_without_ledger_step_refuses_with_attention(purger):
    receipt = _receipt()
    receipt["steps"] = [s for s in receipt["steps"] if s["name"] != "ledger_purged"]
    decision = purger.evaluate_receipt(receipt, NOW)
    assert decision.should_purge is False
    assert decision.attention is True


@pytest.mark.parametrize("status", ["done", "failed", "unavailable"])
def test_non_deferred_statuses_refuse(purger, status):
    decision = purger.evaluate_receipt(_receipt(ledger_status=status), NOW)
    assert decision.should_purge is False
    assert status in decision.reason


def test_failed_purge_is_flagged_for_operator_and_never_retried(purger):
    decision = purger.evaluate_receipt(_receipt(ledger_status="failed"), NOW)
    assert decision.should_purge is False
    assert decision.attention is True
    assert "operator" in decision.reason


def test_done_purge_refusal_is_routine_not_operator(purger):
    decision = purger.evaluate_receipt(_receipt(ledger_status="done"), NOW)
    assert decision.should_purge is False
    assert decision.attention is False


# --------------------------------------------------------------------------- #
# Due-date filtering
# --------------------------------------------------------------------------- #
def test_due_in_the_past_purges(purger):
    decision = purger.evaluate_receipt(_receipt(due_at=PAST), NOW)
    assert decision.should_purge is True
    assert decision.due_at == dt.datetime.fromisoformat(PAST)


def test_due_exactly_now_purges(purger):
    decision = purger.evaluate_receipt(_receipt(due_at=NOW.isoformat()), NOW)
    assert decision.should_purge is True


def test_due_in_the_future_refuses(purger):
    decision = purger.evaluate_receipt(_receipt(due_at=FUTURE), NOW)
    assert decision.should_purge is False
    assert "not due" in decision.reason
    assert decision.attention is False


@pytest.mark.parametrize("due_at", [None, "", "not-a-date", 12345])
def test_missing_or_unparseable_due_at_refuses_with_attention(purger, due_at):
    receipt = _receipt()
    receipt["ledger_purge_due_at"] = due_at
    decision = purger.evaluate_receipt(receipt, NOW)
    assert decision.should_purge is False
    assert decision.attention is True


def test_naive_due_at_is_treated_as_utc(purger):
    past_naive = "2026-07-01T00:00:00"  # stored aware in practice; be defensive
    assert purger.evaluate_receipt(_receipt(due_at=past_naive), NOW).should_purge is True
    future_naive = "2026-08-01T00:00:00"
    assert purger.evaluate_receipt(_receipt(due_at=future_naive), NOW).should_purge is False


# --------------------------------------------------------------------------- #
# Applying results — receipt update + the idempotency latch
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        (["done", "done"], "completed"),
        (["done", "deferred"], "deferred"),
        (["done", "unavailable", "deferred"], "partial_unavailable"),
        (["done", "unavailable", "deferred", "failed"], "partial_failed"),
    ],
)
def test_recompute_overall_status_precedence(purger, statuses, expected):
    steps = [{"name": f"s{i}", "status": s} for i, s in enumerate(statuses)]
    assert purger.recompute_overall_status(steps) == expected


def test_apply_step_results_marks_done_and_stamps_executed_at(purger):
    receipt = _receipt()
    result = {"name": "ledger_purged", "status": "done", "detail": "purged", "count": 42}
    updated = purger.apply_step_results(
        receipt, [result], executed_at="2026-07-20T12:00:00+00:00"
    )
    ledger = [s for s in updated["steps"] if s["name"] == "ledger_purged"]
    assert len(ledger) == 1  # replaced in place, not duplicated
    assert ledger[0]["status"] == "done"
    assert ledger[0]["count"] == 42
    assert updated["overall_status"] == "completed"
    assert updated["ledger_purge_executed_at"] == "2026-07-20T12:00:00+00:00"
    # Historical fields survive.
    assert updated["ledger_purge_due_at"] == PAST
    assert updated["requested_by_uc_uid"] == "owner-1"
    # Pure: the input receipt is untouched.
    assert receipt["overall_status"] == "deferred"
    assert receipt["steps"][-1]["status"] == "deferred"
    assert "ledger_purge_executed_at" not in receipt


def test_apply_failed_result_reports_partial_failed(purger):
    updated = purger.apply_step_results(
        _receipt(),
        [{"name": "ledger_purged", "status": "failed", "detail": "boom"}],
        executed_at="2026-07-20T12:00:00+00:00",
    )
    assert updated["overall_status"] == "partial_failed"


def test_apply_appends_a_step_the_receipt_never_had(purger):
    receipt = _receipt()
    receipt["steps"] = [s for s in receipt["steps"] if s["name"] != "access_revoked"]
    updated = purger.apply_step_results(
        receipt,
        [
            {"name": "access_revoked", "status": "done"},
            {"name": "ledger_purged", "status": "done"},
        ],
        executed_at="2026-07-20T12:00:00+00:00",
    )
    assert {"name": "access_revoked", "status": "done"} in updated["steps"]
    assert purger.recompute_overall_status(updated["steps"]) == "completed"


@pytest.mark.parametrize("outcome", ["done", "failed"])
def test_executed_receipt_is_never_purged_again(purger, outcome):
    """The idempotency latch: once executed (or failed), a re-run refuses."""
    updated = purger.apply_step_results(
        _receipt(),
        [{"name": "ledger_purged", "status": outcome, "detail": "ran"}],
        executed_at="2026-07-20T12:00:00+00:00",
    )
    decision = purger.evaluate_receipt(updated, NOW + dt.timedelta(days=1))
    assert decision.should_purge is False
