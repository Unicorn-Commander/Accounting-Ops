"""AO-MASS-1: POST /journal-entries enforces entity_scope and derives sign_matched.

Two money defects on the HTTP route (the MCP twin already guarded both):

1. entity_id came from the body and was never checked against the membership's
   entity_scope, so an entity-scoped member could post into any entity of the
   workspace. Now 403 before any session opens; no entry is created.
2. body.sign_matched was forwarded as-is into the materiality gate, where it
   is one of the tier-L auto-post trust conditions. The server now derives it
   from the category's COA account type (ANDed with the body, so a caller can
   only tighten), exactly like journal_tools.post_journal_entry.

Offline: the session is faked, and the faked post_journal_entry runs the REAL
materiality gate on what the route forwards, so the outcomes are behavioural.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.api.journal import PostRequest, post_entry
from app.auth import AuthContext
from app.config import get_settings
from app.journal.materiality import evaluate
from app.journal.posting import PostingOutcome, PostingRefusal
from app.tenancy import WorkspaceContext

WID = uuid4()
ENTITY_A = UUID("aaaaaaaa-0000-0000-0000-00000000000a")
ENTITY_B = UUID("bbbbbbbb-0000-0000-0000-00000000000b")
UID = "fixture-human"
L_CENTS = 1_234  # $12.34, tier L (auto-postable when trusted)


def _ws(role: str = "accountant", scope: tuple[UUID, ...] | None = None) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WID,
        workspace_slug="fixture",
        role=role,
        user_uc_uid=UID,
        agent_id=None,
        entity_scope=scope,
        hipaa_protected=False,
    )


def _human() -> AuthContext:
    return AuthContext(
        UID,
        None,
        (),
        {
            "azp": get_settings().keycloak_client_id,
            "auth_time": 1,
            "sid": "fixture-session",
            "preferred_username": UID,
        },
        may_autoprovision=True,
    )


class _Row:
    def __init__(self, account_type: str) -> None:
        self.account_type = account_type


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Session:
    """entity mapped = yes; the category's COA account is an expense account."""

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        if "account_entity_mapping" in sql:
            return _Result((1,))
        if "chart_of_account" in sql:
            return _Result(_Row("expense"))
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


async def _post(ws: WorkspaceContext, *, entity_id: UUID, amount_cents: int,
                sign_matched: bool = True) -> tuple[dict | None, dict[str, Any], dict[str, int]]:
    captured: dict[str, Any] = {}
    counts = {"sessions": 0, "posts": 0}

    class _Ctx:
        async def __aenter__(self):
            counts["sessions"] += 1
            return _Session()

        async def __aexit__(self, *exc):
            return False

    async def fake_post(session: Any, **kw: Any) -> Any:
        counts["posts"] += 1
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
        return PostingRefusal(outcome="queued_for_confirm", reason=verdict.reason)

    body = PostRequest(
        entity_id=entity_id,
        source="fixture-src",
        source_external_id=f"txn-{uuid4()}",
        effective_date=date(2026, 11, 20),
        amount_cents=amount_cents,
        description="Fictional SaaS charge",
        category="SaaS · AI",
        category_status="auto",
        sign_matched=sign_matched,
        external_account_id="ACC-fixture-checking",
    )
    with (
        patch("app.api.journal.workspace_session", lambda *a, **k: _Ctx()),
        patch("app.api.journal.post_journal_entry", fake_post),
        patch("app.journal.projection.project_journal_entry_posted", lambda **kw: None),
    ):
        result = await post_entry(WID, body, workspace=ws, auth=_human())
    return result, captured, counts


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["bookkeeper", "accountant", "admin", "owner"])
async def test_scoped_member_cannot_post_into_another_entity(role):
    counts: dict[str, int] = {}
    with pytest.raises(HTTPException) as exc:
        _, _, counts = await _post(_ws(role, (ENTITY_A,)), entity_id=ENTITY_B, amount_cents=-L_CENTS)
    assert exc.value.status_code == 403
    # Nothing opened, nothing posted: the guard runs before the session.
    assert counts == {}


@pytest.mark.asyncio
async def test_scoped_member_still_posts_into_its_own_entity():
    result, captured, counts = await _post(_ws("accountant", (ENTITY_A,)), entity_id=ENTITY_A,
                                           amount_cents=-L_CENTS)
    assert result["outcome"] == "posted"
    assert captured["entity_id"] == str(ENTITY_A)
    assert counts["posts"] == 1


@pytest.mark.asyncio
async def test_unscoped_owner_posts_into_any_entity():
    result, _, _ = await _post(_ws("owner"), entity_id=ENTITY_B, amount_cents=-L_CENTS)
    assert result["outcome"] == "posted"


@pytest.mark.asyncio
async def test_body_sign_matched_true_cannot_override_server_mismatch():
    # An INFLOW booked to an expense category: the server computes a sign
    # mismatch. The body's sign_matched=True must not make it auto-post.
    result, captured, _ = await _post(_ws("accountant"), entity_id=ENTITY_A,
                                      amount_cents=+L_CENTS, sign_matched=True)
    assert captured["sign_matched"] is False
    assert result["outcome"] == "queued_for_confirm"
    assert "sign_mismatch" in result["reason"]


@pytest.mark.asyncio
async def test_server_match_still_auto_posts_and_body_false_tightens():
    ok, cap_ok, _ = await _post(_ws("accountant"), entity_id=ENTITY_A, amount_cents=-L_CENTS)
    assert cap_ok["sign_matched"] is True and ok["outcome"] == "posted"
    tight, cap_t, _ = await _post(_ws("accountant"), entity_id=ENTITY_A, amount_cents=-L_CENTS,
                                  sign_matched=False)
    assert cap_t["sign_matched"] is False and tight["outcome"] == "queued_for_confirm"
