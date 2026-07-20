"""Erasure unit tests (no database).

The safety properties that must hold regardless of deployment:

  * Erasure REFUSES without an exact confirmation token. This is the guard that
    stops a stray click or a replayed request destroying a customer's books.
  * A receipt never reports "completed" when a step was unavailable or failed —
    the silent-degradation failure mode this codebase has been bitten by, and
    here it would also make the privacy policy untrue.
  * Tier 2 reports `unavailable` (never `done`) when the admin connection is
    absent.
"""
from __future__ import annotations

import pytest

from app.data_rights.erasure import (
    STATUS_DEFERRED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_UNAVAILABLE,
    ErasureConfirmationError,
    ErasureReceipt,
    ErasureStep,
    admin_connection_available,
    purge_workspace_ledger,
    revoke_access,
    verify_confirmation,
)


# --------------------------------------------------------------------------- #
# Confirmation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("supplied", [None, "", "   ", "wrong-slug", "acme", "ACME-BOOKS"])
def test_confirmation_refuses_anything_but_an_exact_match(supplied):
    with pytest.raises(ErasureConfirmationError):
        verify_confirmation(supplied, "acme-books")


def test_confirmation_accepts_exact_slug_and_tolerates_surrounding_whitespace():
    verify_confirmation("acme-books", "acme-books")
    verify_confirmation("  acme-books  ", "acme-books")


def test_confirmation_error_says_nothing_was_deleted():
    # The message is user-facing on a 400. It must be unambiguous that the
    # destructive action did NOT partially run.
    with pytest.raises(ErasureConfirmationError) as exc:
        verify_confirmation("nope", "acme-books")
    assert "Nothing was deleted" in str(exc.value)
    assert "acme-books" in str(exc.value)


# --------------------------------------------------------------------------- #
# Receipt status aggregation — the anti-silent-degradation property
# --------------------------------------------------------------------------- #
def _receipt(*steps: ErasureStep) -> ErasureReceipt:
    return ErasureReceipt(
        workspace_id="00000000-0000-0000-0000-000000000001",
        requested_by_uc_uid="owner-1",
        requested_at="2026-07-20T00:00:00+00:00",
        steps=list(steps),
    )


def test_all_done_reports_completed():
    r = _receipt(ErasureStep("a", STATUS_DONE), ErasureStep("b", STATUS_DONE))
    assert r.overall_status == "completed"


def test_one_unavailable_step_prevents_completed():
    r = _receipt(
        ErasureStep("credentials_purged", STATUS_DONE),
        ErasureStep("ledger_purged", STATUS_UNAVAILABLE, "no admin connection"),
    )
    assert r.overall_status == "partial_unavailable"


def test_a_failure_outranks_everything_else():
    r = _receipt(
        ErasureStep("a", STATUS_DONE),
        ErasureStep("b", STATUS_UNAVAILABLE),
        ErasureStep("c", STATUS_DEFERRED),
        ErasureStep("d", STATUS_FAILED, "boom"),
    )
    assert r.overall_status == "partial_failed"


def test_deferred_ledger_purge_reports_deferred_not_completed():
    r = _receipt(
        ErasureStep("credentials_purged", STATUS_DONE),
        ErasureStep("ledger_purged", STATUS_DEFERRED, "scheduled"),
    )
    assert r.overall_status == "deferred"


def test_receipt_serializes_every_step_with_its_status():
    r = _receipt(
        ErasureStep("credentials_purged", STATUS_DONE, "gone", count=3),
        ErasureStep("ledger_purged", STATUS_UNAVAILABLE, "no admin"),
    )
    d = r.to_dict()
    assert d["overall_status"] == "partial_unavailable"
    assert [s["name"] for s in d["steps"]] == ["credentials_purged", "ledger_purged"]
    assert d["steps"][0]["count"] == 3
    assert d["steps"][1]["status"] == STATUS_UNAVAILABLE
    # A consumer must be able to see WHY a step did not happen.
    assert d["steps"][1]["detail"]


# --------------------------------------------------------------------------- #
# Tier 2 degradation
# --------------------------------------------------------------------------- #
def test_admin_unavailable_when_no_distinct_admin_url(monkeypatch):
    from app import config as config_mod

    monkeypatch.setattr(
        config_mod, "get_settings", lambda: config_mod.Settings(admin_database_url=None)
    )
    monkeypatch.setattr(
        "app.data_rights.erasure.get_settings",
        lambda: config_mod.Settings(admin_database_url=None),
    )
    assert admin_connection_available() is False


async def test_tier2_steps_report_unavailable_not_done_without_admin(monkeypatch):
    """The critical honesty test: no admin connection => never 'done'.

    If this regressed, an erasure would tell a customer their data was destroyed
    while the ledger, their memberships, and their access were all untouched.
    """
    monkeypatch.setattr("app.data_rights.erasure.admin_connection_available", lambda: False)
    wid = "00000000-0000-0000-0000-000000000001"

    steps = await revoke_access(wid)
    assert {s.name for s in steps} == {"access_revoked", "memberships_removed"}
    for s in steps:
        assert s.status == STATUS_UNAVAILABLE
        assert "ADMIN_DATABASE_URL" in s.detail

    ledger = await purge_workspace_ledger(wid)
    assert ledger.name == "ledger_purged"
    assert ledger.status == STATUS_UNAVAILABLE
    assert ledger.status != STATUS_DONE
    assert "NOT purged" in ledger.detail
