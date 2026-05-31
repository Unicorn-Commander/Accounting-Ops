"""Smoke test: bulk_decide_approvals enforces BULK_DECIDE_MAX_ITEMS.

Closes audit Bug 6 in audits/code-audit-2026-05-31.md: the cap was documented
("Cap: 200 per request") but the tool only validated that ``decisions`` was a
list, then iterated every item. A buggy UI or agent could submit thousands of
items and hammer the per-item hash-chain advance.

The fix enforces ``len(decisions) > BULK_DECIDE_MAX_ITEMS`` at the TOP of the
function — BEFORE any auth work — and raises a ValueError. The HTTP wrapper
(``app/api/approval_clusters.py:bulk_decide``) translates this into a 413.

This test runs WITHOUT a live stack — it never reaches the auth or DB path
because the cap-check happens first.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

import app.tools.approvals as approvals_mod


def test_bulk_decide_approvals_max_items_const_is_200():
    """The advertised cap and the actual constant must agree."""
    assert approvals_mod.BULK_DECIDE_MAX_ITEMS == 200


def test_bulk_decide_approvals_rejects_201_items_before_auth():
    """201 items -> ValueError, raised before authorize_workspace runs.

    We monkeypatch ``authorize_workspace`` to a sentinel that raises if invoked.
    The cap check fires first, so the sentinel must never run.
    """
    sentinel = {"called": False}

    async def _should_never_be_called(*args, **kwargs):
        sentinel["called"] = True
        raise AssertionError(
            "authorize_workspace was reached — cap check did not run first"
        )

    workspace_id = uuid.uuid4()
    too_many = [
        {"approval_id": str(uuid.uuid4()), "decision": "approve"}
        for _ in range(201)
    ]

    original = approvals_mod.authorize_workspace
    approvals_mod.authorize_workspace = _should_never_be_called
    try:
        with pytest.raises(ValueError) as exc:
            asyncio.run(
                approvals_mod.bulk_decide_approvals(
                    workspace_id=workspace_id, decisions=too_many
                )
            )
    finally:
        approvals_mod.authorize_workspace = original

    msg = str(exc.value).lower()
    # The error must name the cap (200) and the offending size (201) so the
    # caller can chunk correctly.
    assert "200" in str(exc.value), f"cap value missing from error: {exc.value!r}"
    assert "201" in str(exc.value), f"actual size missing from error: {exc.value!r}"
    assert any(kw in msg for kw in ("cap", "at most", "max")), msg
    # Authorization must NOT have been attempted.
    assert sentinel["called"] is False


def test_bulk_decide_approvals_accepts_200_items():
    """Exactly 200 must pass the cap check (boundary).

    We allow the call to proceed past the cap check and stub authorization
    + the per-item decide so we don't need a stack. The point is to prove 200
    is ACCEPTED — failing here would mean the cap was an off-by-one.
    """
    workspace_id = uuid.uuid4()
    decisions = [
        {"approval_id": str(uuid.uuid4()), "decision": "reject"}
        for _ in range(200)
    ]

    async def _fake_authorize(*args, **kwargs):
        # WorkspaceContext-shaped stub; bulk_decide only checks that the call
        # succeeded (its return value is ignored).
        return None

    async def _fake_per_item(*args, **kwargs):
        return {"decision": "rejected"}

    original_auth = approvals_mod.authorize_workspace
    original_decide = approvals_mod.decide_approval
    approvals_mod.authorize_workspace = _fake_authorize
    approvals_mod.decide_approval = _fake_per_item
    try:
        result = asyncio.run(
            approvals_mod.bulk_decide_approvals(
                workspace_id=workspace_id, decisions=decisions
            )
        )
    finally:
        approvals_mod.authorize_workspace = original_auth
        approvals_mod.decide_approval = original_decide

    assert result["total"] == 200
    assert result["rejected"] == 200
    assert result["failed"] == 0


def test_bulk_decide_approvals_rejects_non_list_input():
    """``decisions`` must be a list — pre-existing behavior preserved."""
    workspace_id = uuid.uuid4()
    with pytest.raises(ValueError):
        asyncio.run(
            approvals_mod.bulk_decide_approvals(
                workspace_id=workspace_id,
                decisions="not a list",  # type: ignore[arg-type]
            )
        )
