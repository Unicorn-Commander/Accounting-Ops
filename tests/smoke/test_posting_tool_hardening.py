"""Hardening of the MCP posting tool: arg bounds, sign derivation, audit trail.

Pure-Python smoke tests, no DB — all DB calls are faked (same style as
test_confirmation_derivation.py / test_period_close.py). These cover the three
gaps left by the 2026-08-10 external report after the ``user_confirmed`` fix:

  1. The MCP surface took bare type hints where the HTTP PostRequest model
     enforces max_lengths, so an agent could send values an HTTP caller could
     not. Plus a sanity bound on magnitude (the units trap: cents vs dollars).
  2. ``sign_matched`` was agent-supplied and feeds the tier-L auto-post trust
     check. The server owns the category's expected direction, so it derives
     the value and ANDs the caller's claim (caller can only tighten).
  3. ``post_journal_entry`` was the ONLY agent write tool that never wrote a
     decision_audit row — the highest-stakes action missing from the trail.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.journal.materiality import MaterialityVerdict
from app.journal.posting import PostingOutcome, PostingRefusal
from app.tools import journal_tools
from app.tools.journal_tools import (
    MAX_ABS_AMOUNT_CENTS,
    _derived_sign_matched,
    _validate_post_arguments,
)


class _Row:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeResult:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def first(self) -> _Row | None:
        return self._row


class _FakeSession:
    """Answers the COA / approval_queue lookups; records every statement."""

    def __init__(
        self,
        *,
        account_type: str | None = None,
        approval_row: _Row | None = None,
        entity_mapped: bool = True,
    ) -> None:
        self.account_type = account_type
        self.approval_row = approval_row
        self.entity_mapped = entity_mapped
        self.executed: list[tuple[str, dict[str, Any] | None]] = []

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append((sql, params))
        if "approval_queue" in sql:
            return _FakeResult(self.approval_row)
        if "chart_of_account" in sql:
            if self.account_type is None:
                return _FakeResult(None)
            return _FakeResult(_Row(account_type=self.account_type))
        if "account_entity_mapping" in sql:
            return _FakeResult(_Row(x=1) if self.entity_mapped else None)
        return _FakeResult(None)

    def audit_inserts(self) -> list[tuple[str, dict[str, Any] | None]]:
        return [(s, p) for s, p in self.executed if "INSERT INTO decision_audit" in s]


class _Ctx:
    user_uc_uid = "agent-caller"
    agent_id = "test-agent"


def _valid_args(**overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "source": "bofa-statement",
        "source_external_id": "txn-1",
        "description": "Coffee",
        "category": "SaaS · AI",
        "category_status": "auto",
        "amount_cents": -1234,
        "payee": "Acme",
        "model_version": "test/v1",
    }
    args.update(overrides)
    return args


# ---------------------------------------------------------------------------
# 1. Argument validation parity with the HTTP PostRequest model
# ---------------------------------------------------------------------------


def test_valid_arguments_pass() -> None:
    _validate_post_arguments(**_valid_args())


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("source", 64),
        ("source_external_id", 256),
        ("description", 512),
        ("category", 64),
        ("payee", 256),
        ("model_version", 128),
    ],
)
def test_overlong_strings_are_rejected(field: str, limit: int) -> None:
    _validate_post_arguments(**_valid_args(**{field: "x" * limit}))  # at limit: OK
    with pytest.raises(ValueError, match=f"{field} exceeds max length {limit}"):
        _validate_post_arguments(**_valid_args(**{field: "x" * (limit + 1)}))


@pytest.mark.parametrize("field", ["source", "source_external_id", "description", "category"])
def test_empty_required_strings_are_rejected(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} is required"):
        _validate_post_arguments(**_valid_args(**{field: ""}))


def test_zero_amount_is_rejected() -> None:
    with pytest.raises(ValueError, match="amount_cents must be non-zero"):
        _validate_post_arguments(**_valid_args(amount_cents=0))


def test_absurd_magnitude_is_rejected_with_units_hint() -> None:
    _validate_post_arguments(**_valid_args(amount_cents=-MAX_ABS_AMOUNT_CENTS))
    with pytest.raises(ValueError, match="this field is CENTS, not dollars"):
        _validate_post_arguments(**_valid_args(amount_cents=MAX_ABS_AMOUNT_CENTS + 1))


def test_bad_category_status_is_rejected() -> None:
    with pytest.raises(ValueError, match="category_status must be"):
        _validate_post_arguments(**_valid_args(category_status="definitely"))


def test_none_payee_is_allowed() -> None:
    _validate_post_arguments(**_valid_args(payee=None))


# ---------------------------------------------------------------------------
# 2. Server-derived sign_matched
# ---------------------------------------------------------------------------


def _derive(account_type: str | None, amount_cents: int) -> bool:
    session = _FakeSession(account_type=account_type)
    return asyncio.run(
        _derived_sign_matched(
            session,
            entity_id=uuid4(),
            category="SaaS · AI",  # a real CATEGORY_TO_ACCOUNT key
            amount_cents=amount_cents,
        )
    )


@pytest.mark.parametrize("account_type", ["expense", "other_expense", "cogs", "tax"])
def test_outflow_accounts_require_negative_amount(account_type: str) -> None:
    assert _derive(account_type, -5_000) is True
    assert _derive(account_type, 5_000) is False


@pytest.mark.parametrize("account_type", ["revenue", "other_income"])
def test_inflow_accounts_require_positive_amount(account_type: str) -> None:
    assert _derive(account_type, 5_000) is True
    assert _derive(account_type, -5_000) is False


@pytest.mark.parametrize("account_type", ["asset", "liability", "equity"])
def test_ambiguous_account_types_never_manufacture_a_mismatch(account_type: str) -> None:
    # An owner contribution (+) and a distribution (-) both legitimately hit
    # equity; the server must not call either one a sign mismatch.
    assert _derive(account_type, 5_000) is True
    assert _derive(account_type, -5_000) is True


def test_unresolvable_account_does_not_manufacture_a_mismatch() -> None:
    # Missing COA row: the post is blocked downstream on resolution, so this
    # lookup must not invent a mismatch of its own.
    assert _derive(None, 5_000) is True


def test_unknown_category_does_not_manufacture_a_mismatch() -> None:
    session = _FakeSession(account_type="expense")
    assert (
        asyncio.run(
            _derived_sign_matched(
                session,
                entity_id=uuid4(),
                category="Not A Real Category",
                amount_cents=5_000,
            )
        )
        is True
    )


# ---------------------------------------------------------------------------
# Tool-level wiring: derivation ANDs the claim; audit row is written
# ---------------------------------------------------------------------------


def _run_tool(
    *,
    session: _FakeSession,
    journal_post: AsyncMock,
    **overrides: Any,
) -> dict[str, Any]:
    @contextlib.asynccontextmanager
    async def fake_ws_session(_factory: Any, _wid: Any):
        yield session

    kwargs: dict[str, Any] = {
        "workspace_id": uuid4(),
        "entity_id": uuid4(),
        "source": "bofa-statement",
        "source_external_id": "txn-123",
        "effective_date": "2026-08-01",
        "amount_cents": -2_500,
        "description": "Vendor charge",
        "category": "SaaS · AI",
        "external_account_id": "acct-1",
    }
    kwargs.update(overrides)

    with (
        patch.object(journal_tools, "authorize_workspace", AsyncMock(return_value=_Ctx())),
        patch.object(journal_tools, "workspace_session", fake_ws_session),
        patch.object(journal_tools, "_journal_post", journal_post),
        patch("app.journal.pending_post.existing_proposal", AsyncMock(return_value=None)),
        patch("app.journal.pending_post.enqueue", AsyncMock(return_value="proposal-id")),
        patch.object(journal_tools, "project_decision_audit", lambda **kw: None),
    ):
        return asyncio.run(journal_tools.post_journal_entry(**kwargs))


def _refusal_post() -> AsyncMock:
    return AsyncMock(
        return_value=PostingRefusal(
            outcome="queued_for_confirm",
            reason="Tier L but categorization not auto-trustable: sign_mismatch",
        )
    )


def _success_post() -> AsyncMock:
    return AsyncMock(
        return_value=PostingOutcome(
            journal_entry_id=uuid4(),
            materiality=MaterialityVerdict(
                tier="L", outcome="auto_post", reason="Vendor rule matched cleanly."
            ),
        )
    )


def test_wrong_signed_expense_is_downgraded_despite_agent_claim() -> None:
    # Agent claims sign_matched=True on a POSITIVE amount against an expense
    # account. The server disagrees and the claim cannot win.
    session = _FakeSession(account_type="expense")
    jp = _refusal_post()
    _run_tool(session=session, journal_post=jp, amount_cents=5_000, sign_matched=True)
    assert jp.call_args.kwargs["sign_matched"] is False


def test_agent_can_still_tighten_but_not_loosen() -> None:
    session = _FakeSession(account_type="expense")
    jp = _refusal_post()
    # Server would derive True (expense + outflow), agent volunteers False.
    _run_tool(session=session, journal_post=jp, amount_cents=-5_000, sign_matched=False)
    assert jp.call_args.kwargs["sign_matched"] is False


def test_correctly_signed_expense_keeps_sign_matched() -> None:
    session = _FakeSession(account_type="expense")
    jp = _success_post()
    _run_tool(session=session, journal_post=jp, amount_cents=-5_000, sign_matched=True)
    assert jp.call_args.kwargs["sign_matched"] is True


def test_decision_audit_written_on_successful_post() -> None:
    session = _FakeSession(account_type="expense")
    out = _run_tool(session=session, journal_post=_success_post(), amount_cents=-5_000)
    assert out["ok"] is True
    [(_sql, params)] = session.audit_inserts()
    assert params["tool_name"] == "post_journal_entry"
    assert params["outcome"] == "posted"
    assert '"confirmation_verified": false' in params["request"]


def test_decision_audit_written_on_refusal_too() -> None:
    # A BLOCKED post is exactly the signal an operator reviewing agent
    # behavior needs, so refusals must land in the trail as well.
    session = _FakeSession(account_type="expense")
    out = _run_tool(session=session, journal_post=_refusal_post(), amount_cents=-5_000)
    assert out["ok"] is False
    [(_sql, params)] = session.audit_inserts()
    assert params["outcome"] == "queued_for_confirm"


def test_validation_rejects_before_any_db_work() -> None:
    session = _FakeSession(account_type="expense")
    jp = _success_post()
    with pytest.raises(ValueError, match="amount_cents must be non-zero"):
        _run_tool(session=session, journal_post=jp, amount_cents=0)
    assert session.executed == []
    jp.assert_not_awaited()
