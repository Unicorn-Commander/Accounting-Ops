"""AO111-R2-F4: POST /journal-entries validates receipt_link_id (MONEY, no DB).

Before the fix the route passed has_receipt_link=body.receipt_link_id is not
None, so any made-up UUID lifted the IRS 274(d) block: the $12.34 Meals probe
with a random receipt_link_id auto-posted. Now the route runs the same
validate_receipt the MCP twin uses (journal_tools.post_journal_entry ->
app.journal.pending_post.validate_receipt) and refuses (422) a receipt id that
does not attest this workspace/entity/source transaction; nothing is posted.

Offline: the session is faked; the faked post_journal_entry runs the REAL
materiality gate on what the route forwards.
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
ENTITY_A = UUID("aaaaaaaa-0000-0000-0000-00000000003a")
UID = "fixture-human"
MEALS_CENTS = -1_234  # $12.34 Meals, tier L
GOOD_RECEIPT = UUID("cccccccc-0000-0000-0000-0000000000c1")


def _ws() -> WorkspaceContext:
    return WorkspaceContext(
        workspace_id=WID,
        workspace_slug="fixture",
        role="accountant",
        user_uc_uid=UID,
        agent_id=None,
        entity_scope=None,
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
    account_type = "expense"


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def first(self) -> Any:
        return self._row


class _Session:
    """Mapped account, expense COA; recon_link attests GOOD_RECEIPT only,
    and only for this workspace + entity."""

    def __init__(self, seen: dict[str, Any]) -> None:
        self.seen = seen

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = str(stmt)
        params = params or {}
        if "recon_link" in sql:
            self.seen["receipt_checks"].append(params)
            ok = (
                params["rid"] == str(GOOD_RECEIPT)
                and params["wid"] == str(WID)
                and params["eid"] == str(ENTITY_A)
            )
            return _Result((1,) if ok else None)
        if "account_entity_mapping" in sql:
            return _Result((1,))
        if "chart_of_account" in sql:
            return _Result(_Row())
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


@pytest.fixture
def seen() -> dict[str, Any]:
    return {"receipt_checks": [], "posts": []}


async def _post(seen: dict[str, Any], receipt_link_id: UUID | None) -> dict:
    class _Ctx:
        async def __aenter__(self):
            return _Session(seen)

        async def __aexit__(self, *exc):
            return False

    async def fake_post(session: Any, **kw: Any) -> Any:
        seen["posts"].append(kw)
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
        return PostingRefusal(outcome="blocked_materiality", reason=verdict.reason)

    body = PostRequest(
        entity_id=ENTITY_A,
        source="fixture-src",
        source_external_id="txn-meals-1",
        effective_date=date(2026, 11, 20),
        amount_cents=MEALS_CENTS,
        description="Fictional team lunch",
        category="Meals",
        category_status="auto",
        external_account_id="ACC-fixture-checking",
        receipt_link_id=receipt_link_id,
    )
    with (
        patch("app.api.journal.workspace_session", lambda *a, **k: _Ctx()),
        patch("app.api.journal.post_journal_entry", fake_post),
        patch("app.journal.projection.project_journal_entry_posted", lambda **kw: None),
    ):
        return await post_entry(WID, body, workspace=_ws(), auth=_human())


async def test_meals_probe_with_made_up_receipt_is_refused_and_not_posted(seen):
    with pytest.raises(HTTPException) as exc:
        await _post(seen, uuid4())
    assert exc.value.status_code == 422
    assert len(seen["receipt_checks"]) == 1
    assert seen["posts"] == []


async def test_meals_probe_without_receipt_returns_block_274d(seen):
    result = await _post(seen, None)
    assert result["ok"] is False
    assert "274(d)" in result["reason"]
    assert seen["posts"][0]["has_receipt_link"] is False
    assert seen["receipt_checks"] == []


async def test_validated_receipt_still_lifts_the_274d_block(seen):
    result = await _post(seen, GOOD_RECEIPT)
    assert result["outcome"] == "posted"
    check = seen["receipt_checks"][0]
    assert check["src"] == "fixture-src" and check["ext"] == "txn-meals-1"
    assert seen["posts"][0]["has_receipt_link"] is True
    assert seen["posts"][0]["receipt_link_id"] == GOOD_RECEIPT
