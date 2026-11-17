"""Stripe approval decisions need an interactive human, on approve, reject and retry."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.api import approvals
from app.auth import AuthContext, validate_bearer_token
from app.config import get_settings
from app.tenancy import WorkspaceContext, resolve_workspace

HUMAN = "fixture-human"


def _callers(kind: str) -> tuple[WorkspaceContext, AuthContext]:
    ctx = WorkspaceContext(uuid4(), "isolated", "accountant", HUMAN, None, None, False)
    auth = AuthContext(
        HUMAN,
        None,
        (),
        {
            "sid": "fixture-session",
            "azp": get_settings().keycloak_client_id,
            "auth_time": 1789520000,
            "preferred_username": HUMAN,
        },
        may_autoprovision=True,
    )
    if kind == "pat":
        ctx = replace(ctx, pat_scopes=("accounting:write",))
    elif kind == "pat_with_human_claims":
        # A PAT resolution must lose even if the bearer looks like a session.
        ctx = replace(ctx, pat_scopes=("accounting:write",))
    elif kind == "service":
        auth = replace(
            auth,
            may_autoprovision=False,
            claims={"azp": "fixture-service", "client_id": "fixture-service",
                    "preferred_username": "service-account-fixture-service"},
        )
    elif kind == "agent":
        ctx = replace(ctx, agent_id="fixture-agent")
        auth = replace(auth, agent_id="fixture-agent")
    elif kind == "agent_ctx_only":
        ctx = replace(ctx, agent_id="fixture-agent")
    elif kind == "m2m":
        auth = replace(auth, is_trusted_m2m=True)
    return ctx, auth


async def _decide(kind: str, decision: str, row_status: str = "pending"):
    ctx, auth = _callers(kind)
    aid = uuid4()
    row = dict(
        id=str(aid),
        entity_id=str(uuid4()),
        subject_type="stripe_post_balance_transaction",
        subject_id=str(aid),
        status=row_status,
        payload={"stripe_balance_txn_id": "txn_fixture_1"},
        reasoning="fixture",
    )
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))
        )
    )

    @asynccontextmanager
    async def ws(*args):
        yield session

    app = FastAPI()
    app.include_router(approvals.router)
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    app.dependency_overrides[validate_bearer_token] = lambda: auth
    lock = AsyncMock(return_value=frozenset())
    apply = AsyncMock(return_value={"journal_entry_id": str(uuid4())})
    mark = AsyncMock()
    audit = AsyncMock(return_value=uuid4())
    with (
        patch.object(approvals, "workspace_session", ws),
        patch(
            "app.connectors.stripe.blocked_resolution.lock_stripe_state_for_approval", lock
        ),
        patch.object(approvals, "_approve_stripe_balance_transaction", apply),
        patch.object(approvals, "_mark_decided", mark),
        patch.object(approvals, "write_decision_audit", audit),
        patch.object(approvals, "_project_approval_decision"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            result = await c.post(
                f"/api/workspaces/{ctx.workspace_id}/approvals/{aid}/decide",
                json={"decision": decision},
            )
    return result, lock, apply, mark, audit


AUTOMATED = ["pat", "pat_with_human_claims", "service", "agent", "agent_ctx_only", "m2m"]


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("kind", AUTOMATED)
def test_automated_caller_cannot_decide_stripe_row(kind, decision):
    result, lock, apply, mark, audit = asyncio.run(_decide(kind, decision))
    assert result.status_code == 403, result.text
    assert "human session" in result.text
    lock.assert_not_awaited()
    apply.assert_not_awaited()
    mark.assert_not_awaited()
    audit.assert_not_awaited()


@pytest.mark.parametrize("kind", AUTOMATED)
def test_automated_caller_cannot_use_approved_retry_shortcut(kind):
    result, lock, apply, mark, audit = asyncio.run(_decide(kind, "approve", "approved"))
    assert result.status_code == 403, result.text
    lock.assert_not_awaited()
    audit.assert_not_awaited()


def test_human_accountant_approves_stripe_row():
    result, lock, apply, mark, audit = asyncio.run(_decide("human", "approve"))
    assert result.status_code == 200, result.text
    assert result.json()["applied"] is True
    assert apply.call_args.kwargs["approving_human"] == HUMAN
    assert mark.call_args.kwargs["decided_by"] == HUMAN


def test_human_accountant_rejects_stripe_row():
    result, lock, apply, mark, audit = asyncio.run(_decide("human", "reject"))
    assert result.status_code == 200, result.text
    assert result.json()["decision"] == "rejected"
    apply.assert_not_awaited()
    assert mark.call_args.kwargs["new_status"] == "rejected"


def test_human_retry_of_approved_row_stays_idempotent():
    result, lock, apply, mark, audit = asyncio.run(_decide("human", "approve", "approved"))
    assert result.status_code == 200, result.text
    assert result.json()["idempotent"] is True
    apply.assert_not_awaited()
