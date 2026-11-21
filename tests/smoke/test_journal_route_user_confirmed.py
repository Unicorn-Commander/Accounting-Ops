"""HTTP journal-post route: body.user_confirmed is trusted only for humans.

Audit c1360430 F1. ``POST /api/workspaces/{id}/journal-entries`` checked only
``require_role("bookkeeper")`` and passed ``body.user_confirmed`` straight to
``post_journal_entry``, while ``materiality.evaluate`` auto-posts ANY tier when
``user_confirmed`` is True. An agent or service token (which resolve_workspace
never blocks, and which act as a real user) could therefore post an H-tier
entry with no human — violating the trust contract at materiality.py:76-80.
The MCP tool (journal_tools.py forces ``approving_human=None``) and the
approval decide path (``_is_interactive_human``) already guard; this route
must apply the same rule: ``user_confirmed`` counts ONLY when the caller is a
verified interactive human session, never because a body field said so.

Offline smoke tests: the DB session and ``post_journal_entry`` are faked; the
fake runs the REAL materiality gate on the flag the route forwards, so the
assertions are behavioral (posted vs staged for review), not just plumbing.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from app.api.journal import PostRequest, post_entry
from app.auth import AuthContext
from app.config import get_settings
from app.journal.materiality import evaluate
from app.journal.posting import PostingOutcome, PostingRefusal
from app.tenancy import WorkspaceContext

WID = uuid4()
EID = uuid4()
HUMAN_UID = "fixture-human"

H_AMOUNT_CENTS = -500_000  # $5,000 outflow — tier H (>= $1,000)
L_AMOUNT_CENTS = -1_234    # $12.34 outflow — tier L (auto-postable)


def _workspace(*, agent_id: str | None = None) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WID,
        workspace_slug="fixture",
        role="accountant",  # >= bookkeeper, and NOT bookkeeper (no force_queue)
        user_uc_uid=HUMAN_UID,
        agent_id=agent_id,
        entity_scope=None,
        hipaa_protected=False,
    )


def _human_auth() -> AuthContext:
    """Keycloak interactive-session claims (what _is_interactive_human wants)."""
    return AuthContext(
        HUMAN_UID,
        None,
        (),
        {
            "azp": get_settings().keycloak_client_id,
            "auth_time": 1,
            "sid": "fixture-session",
            "preferred_username": HUMAN_UID,
        },
        may_autoprovision=True,
    )


def _agent_auth() -> AuthContext:
    """Agent bearer: agent_id set, no interactive-session claims."""
    return AuthContext(HUMAN_UID, "fixture-agent", (), {}, may_autoprovision=False)


def _service_auth() -> AuthContext:
    """Service token: acts as the real user, no interactive-session claims."""
    return AuthContext(HUMAN_UID, None, (), {"clientId": "fixture-service"}, may_autoprovision=False)


class _FakeResult:
    def __init__(self, row: tuple[int] | None) -> None:
        self._row = row

    def first(self) -> tuple[int] | None:
        return self._row


class _FakeSession:
    """Answers only the account_entity_mapping probe (entity_mapped=True)."""

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _FakeResult:
        if "account_entity_mapping" in str(stmt):
            return _FakeResult((1,))
        raise AssertionError(f"unexpected SQL: {str(stmt)[:80]}")


class _SessionCtx:
    async def __aenter__(self) -> _FakeSession:
        return _FakeSession()

    async def __aexit__(self, *exc: Any) -> bool:
        return False


def _fake_workspace_session(*args: Any, **kwargs: Any) -> _SessionCtx:
    return _SessionCtx()


async def _post(*, workspace: WorkspaceContext, auth: AuthContext, amount_cents: int) -> tuple[dict, dict]:
    """Call the route with a faked session + a post_journal_entry that runs
    the REAL materiality gate on the forwarded user_confirmed flag."""
    captured: dict[str, Any] = {}

    async def fake_post(session: Any, **kw: Any) -> Any:
        captured.update(kw)
        verdict = evaluate(
            amount_cents=kw["amount_cents"],
            category=kw["category"],
            category_status=kw["category_status"],
            entity_mapped=kw["entity_mapped"],
            sign_matched=kw["sign_matched"],
            has_receipt_link=kw["has_receipt_link"],
            user_confirmed=kw["user_confirmed"],
        )
        if verdict.outcome == "auto_post":
            return PostingOutcome(journal_entry_id=uuid4(), materiality=verdict)
        outcome = {
            "queue_for_confirm": "queued_for_confirm",
            "block_high_value": "blocked_materiality",
            "block_274d": "blocked_materiality",
        }.get(verdict.outcome, "blocked_materiality")
        return PostingRefusal(outcome=outcome, reason=verdict.reason)

    body = PostRequest(
        entity_id=EID,
        source="fixture-src",
        source_external_id=f"txn-{uuid4()}",
        effective_date=date(2026, 11, 20),
        amount_cents=amount_cents,
        description="Fictional expense",
        category="SaaS · AI",
        category_status="auto",
        external_account_id="ACC-fixture-checking",
        user_confirmed=True,
    )
    with (
        patch("app.api.journal.workspace_session", _fake_workspace_session),
        patch("app.api.journal.post_journal_entry", fake_post),
        patch("app.journal.projection.project_journal_entry_posted", lambda **kw: None),
    ):
        result = await post_entry(WID, body, workspace=workspace, auth=auth)
    return result, captured


@pytest.mark.asyncio
async def test_agent_token_user_confirmed_true_does_not_post_h_tier() -> None:
    result, captured = await _post(
        workspace=_workspace(agent_id="fixture-agent"),
        auth=_agent_auth(),
        amount_cents=H_AMOUNT_CENTS,
    )
    assert captured["user_confirmed"] is False
    assert result["outcome"] != "posted"
    assert result["outcome"] == "blocked_materiality"  # staged for human review


@pytest.mark.asyncio
async def test_service_token_user_confirmed_true_does_not_post_h_tier() -> None:
    result, captured = await _post(
        workspace=_workspace(),
        auth=_service_auth(),
        amount_cents=H_AMOUNT_CENTS,
    )
    assert captured["user_confirmed"] is False
    assert result["outcome"] != "posted"
    assert result["outcome"] == "blocked_materiality"


@pytest.mark.asyncio
async def test_interactive_human_user_confirmed_true_still_posts() -> None:
    result, captured = await _post(
        workspace=_workspace(),
        auth=_human_auth(),
        amount_cents=H_AMOUNT_CENTS,
    )
    assert captured["user_confirmed"] is True
    assert result["outcome"] == "posted"


@pytest.mark.asyncio
async def test_agent_below_auto_tier_unchanged() -> None:
    result, captured = await _post(
        workspace=_workspace(agent_id="fixture-agent"),
        auth=_agent_auth(),
        amount_cents=L_AMOUNT_CENTS,
    )
    assert captured["user_confirmed"] is False
    assert result["outcome"] == "posted"  # L tier auto-posts without any confirm
