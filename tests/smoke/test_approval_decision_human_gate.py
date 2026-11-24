"""AO-APR-2: agents and PATs cannot approve recats or bank imports.

The decide endpoint applies materiality-gated ledger changes with
user_confirmed=True and stamps approving_human, which is a human attestation.
The journal_entry_post branch already rejected agent/PAT callers, but the
journal_recategorization and bank_transaction_import branches accepted any
caller. These tests prove the gate on BOTH surfaces the shared handler is
reached from:

  * REST: the registered /approvals/{id}/decide route (FastAPI ASGI transport,
    mocked DB session so nothing is actually applied),
  * MCP: app.tools.approvals.decide_approval, the tool agents call through the
    /mcp mount (authorize_workspace patched like test_mcp_write_tools_authz,
    mocked DB session).

An agent-principal and a PAT caller approving a journal_recategorization or a
bank_transaction_import get 403 and the apply helper is never awaited; a human
session still approves both (and the attesting human is stamped). Reject is
unchanged: an agent may still reject (no ledger change on reject).

No stack, no network: every DB access is mocked, exactly like
test_stripe_decision_human_unit.py and test_pending_post_decision_http.py.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.api import approvals
from app.auth import AuthContext, validate_bearer_token
from app.config import get_settings
from app.tenancy import WorkspaceContext, resolve_workspace

HUMAN = "fixture-human"
AGENT = "fixture-agent"
WID = uuid4()
EID = uuid4()
AZP = get_settings().keycloak_client_id


# --------------------------------------------------------------------------
# Caller fixtures: a verified interactive human, and the two principals this
# fix denies (agent token, PAT token).
# --------------------------------------------------------------------------
def _human_ctx(role: str = "accountant") -> WorkspaceContext:
    return WorkspaceContext(WID, "isolated", role, HUMAN, None, None, False)


def _human_auth() -> AuthContext:
    return AuthContext(
        HUMAN,
        None,
        (),
        {
            "sid": "fixture-session",
            "azp": AZP,
            "auth_time": 1789520000,
            "preferred_username": HUMAN,
        },
        may_autoprovision=True,
    )


def _agent_caller() -> tuple[WorkspaceContext, AuthContext]:
    ctx = replace(_human_ctx(), agent_id=AGENT)
    auth = replace(_human_auth(), agent_id=AGENT)
    return ctx, auth


def _pat_caller() -> tuple[WorkspaceContext, AuthContext]:
    ctx = replace(_human_ctx(), pat_scopes=("accounting:write",))
    auth = replace(_human_auth(), scopes=("accounting:write",), claims={"pat_id": "fixture-pat"})
    return ctx, auth


def _session_for(subject_type: str) -> SimpleNamespace:
    """Mocked DB session: one approval_queue row, pending, with a valid
    payload for the subject type under test."""
    aid = uuid4()
    if subject_type == "journal_recategorization":
        payload = {"action": "recategorize", "new_category": "Infrastructure · VPS"}
    else:  # bank_transaction_import
        payload = {
            "action": "bank_transaction_import",
            "source_external_id": f"fixture-sxid-{aid}",
            "effective_date": "2026-03-05",
            "amount_cents": -9900,
            "category": "Banking · Fees",
            "category_status": "auto",
        }
    row = dict(
        id=str(aid),
        entity_id=str(EID),
        subject_type=subject_type,
        subject_id=str(aid),
        status="pending",
        payload=payload,
        reasoning="fixture",
    )
    return SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mappings=lambda: SimpleNamespace(first=lambda: row)
            )
        )
    ), row


SUBJECTS = ["journal_recategorization", "bank_transaction_import"]
CALLERS = [pytest.param(_agent_caller, id="agent"), pytest.param(_pat_caller, id="pat")]


# --------------------------------------------------------------------------
# REST surface: registered decide route via ASGI transport
# --------------------------------------------------------------------------
async def _rest_decide(caller, subject_type: str, decision: str):
    ctx, auth = caller()
    session, row = _session_for(subject_type)

    @asynccontextmanager
    async def ws(*args):
        yield session

    app = FastAPI()
    app.include_router(approvals.router)
    app.dependency_overrides[resolve_workspace] = lambda: ctx
    app.dependency_overrides[validate_bearer_token] = lambda: auth
    apply_recat = AsyncMock(return_value={"reversal_entry_id": str(uuid4()), "restated_entry_id": str(uuid4())})
    apply_import = AsyncMock(return_value={"journal_entry_id": str(uuid4())})
    with (
        patch.object(approvals, "workspace_session", ws),
        patch.object(approvals, "_approve_recategorization", apply_recat),
        patch.object(approvals, "_approve_bank_transaction_import", apply_import),
        patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
        patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
        patch.object(approvals, "_project_approval_decision"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.post(
                f"/api/workspaces/{WID}/approvals/{row['id']}/decide",
                json={"decision": decision},
            )
    return result, apply_recat, apply_import, mark, row


@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("subject_type", SUBJECTS)
def test_rest_agent_and_pat_cannot_approve_recat_or_bank_import(caller, subject_type):
    async def run():
        result, apply_recat, apply_import, mark, row = await _rest_decide(
            caller, subject_type, "approve"
        )
        assert result.status_code == 403, result.text
        assert "human session" in result.text
        # Nothing applied: neither apply helper ran, the row was never marked
        # decided, and (with the mocked session) no ledger write was issued.
        apply_recat.assert_not_awaited()
        apply_import.assert_not_awaited()
        mark.assert_not_awaited()
        return row

    row = asyncio.run(run())
    # The pending row is untouched (state read back from the session fixture).
    assert row["status"] == "pending"


@pytest.mark.parametrize("subject_type", SUBJECTS)
def test_rest_human_session_still_approves_recat_and_bank_import(subject_type):
    async def run():
        result, apply_recat, apply_import, mark, _row = await _rest_decide(
            lambda: (_human_ctx(), _human_auth()), subject_type, "approve"
        )
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["ok"] is True and body["decision"] == "approved"
        assert body["applied"] is True
        if subject_type == "journal_recategorization":
            assert apply_recat.await_count == 1
            assert apply_recat.call_args.kwargs["approving_human"] == HUMAN
        else:
            assert apply_import.await_count == 1
            assert apply_import.call_args.kwargs["approving_human"] == HUMAN
        assert mark.call_args.kwargs["new_status"] == "approved"

    asyncio.run(run())


def test_rest_agent_can_still_reject_recat_and_bank_import():
    """Reject stays allowed for agents: the reject path applies no ledger
    change and is unchanged by this fix."""

    async def run():
        for subject_type in SUBJECTS:
            result, apply_recat, apply_import, mark, _row = await _rest_decide(
                _agent_caller, subject_type, "reject"
            )
            assert result.status_code == 200, result.text
            assert result.json()["decision"] == "rejected"
            assert result.json()["applied"] is False
            apply_recat.assert_not_awaited()
            apply_import.assert_not_awaited()
            assert mark.call_args.kwargs["new_status"] == "rejected"

    asyncio.run(run())


# --------------------------------------------------------------------------
# MCP surface: app.tools.approvals.decide_approval (the tool agents call via
# the /mcp mount). authorize_workspace is patched the way
# test_mcp_write_tools_authz.py patches it, so the real MCP tool body runs.
# --------------------------------------------------------------------------
@pytest.mark.parametrize("caller", CALLERS)
@pytest.mark.parametrize("subject_type", SUBJECTS)
def test_mcp_agent_and_pat_cannot_approve_recat_or_bank_import(caller, subject_type):
    async def run():
        session, row = _session_for(subject_type)

        @asynccontextmanager
        async def _fake_ws(*args):
            yield session

        from app.tools import approvals as tools

        with (
            patch.object(approvals, "workspace_session", _fake_ws),
            patch.object(
                tools, "authorize_workspace", AsyncMock(return_value=caller()[0])
            ),
            patch.object(approvals, "_approve_recategorization", AsyncMock()) as apply_recat,
            patch.object(approvals, "_approve_bank_transaction_import", AsyncMock()) as apply_import,
            patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
            patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
            patch.object(approvals, "_project_approval_decision"),
        ):
            with pytest.raises(HTTPException) as denial:
                await tools.decide_approval(WID, UUID(row["id"]), "approve")
            assert denial.value.status_code == 403
            assert "human session" in str(denial.value.detail)
            apply_recat.assert_not_awaited()
            apply_import.assert_not_awaited()
            mark.assert_not_awaited()

    asyncio.run(run())
    # State read back: the pending row was never marked decided.
    _session, row = _session_for(subject_type)
    assert row["status"] == "pending"


@pytest.mark.parametrize("subject_type", SUBJECTS)
def test_mcp_human_session_still_approves_recat_and_bank_import(subject_type):
    async def run():
        session, row = _session_for(subject_type)

        @asynccontextmanager
        async def _fake_ws(*args):
            yield session

        from app.tools import approvals as tools

        with (
            patch.object(approvals, "workspace_session", _fake_ws),
            patch.object(
                tools, "authorize_workspace", AsyncMock(return_value=_human_ctx())
            ),
            patch.object(approvals, "_approve_recategorization", AsyncMock(
                return_value={"reversal_entry_id": str(uuid4()), "restated_entry_id": str(uuid4())}
            )) as apply_recat,
            patch.object(approvals, "_approve_bank_transaction_import", AsyncMock(
                return_value={"journal_entry_id": str(uuid4())}
            )) as apply_import,
            patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
            patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
            patch.object(approvals, "_project_approval_decision"),
        ):
            ctx, auth = _human_ctx(), _human_auth()
            outcome = await approvals.decide_approval(
                WID, UUID(row["id"]),
                approvals.DecideRequest(decision="approve"), ctx, auth,
            )
        assert outcome["ok"] is True and outcome["decision"] == "approved"
        assert outcome["applied"] is True
        if subject_type == "journal_recategorization":
            assert apply_recat.await_count == 1
            assert apply_recat.call_args.kwargs["approving_human"] == HUMAN
        else:
            assert apply_import.await_count == 1
            assert apply_import.call_args.kwargs["approving_human"] == HUMAN
        assert mark.call_args.kwargs["new_status"] == "approved"

    asyncio.run(run())


# --------------------------------------------------------------------------
# AO-APR-1: the gate now covers ebay_post_transaction too. Approve arms a
# durable worker replay via _record_ebay_decision, so an agent or PAT caller
# gets 403 and the record helper never runs; a human session still gets
# through. (compliance_rule + the real-principal recording are covered in the
# follow-up commits.) The real-REST machine 403 + state read-back for the stack
# service token is proven in tests/integration/test_approvals.py.
# --------------------------------------------------------------------------
def test_agent_and_pat_cannot_approve_ebay_or_compliance_rule():
    """Agent + PAT approve of an ebay_post_transaction gets 403 BEFORE the
    record helper runs: no durable decision, row never marked decided."""
    for caller in (_agent_caller, _pat_caller):
        async def run():
            session, row = _session_for("ebay_post_transaction")

            @asynccontextmanager
            async def _fake_ws(*args):
                yield session

            ctx, auth = caller()
            with (
                patch.object(approvals, "workspace_session", _fake_ws),
                patch.object(approvals, "_record_ebay_decision", AsyncMock()) as record,
                patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
                patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
                patch.object(approvals, "_project_approval_decision"),
            ):
                with pytest.raises(HTTPException) as denial:
                    await approvals.decide_approval(
                        WID, UUID(row["id"]),
                        approvals.DecideRequest(decision="approve"), ctx, auth,
                    )
            assert denial.value.status_code == 403
            assert "human session" in denial.value.detail
            record.assert_not_awaited()
            mark.assert_not_awaited()
            assert row["status"] == "pending"

        asyncio.run(run())


def test_human_session_still_approves_ebay():
    """A verified interactive human still approves an ebay_post_transaction:
    the record helper runs (approve, stamped with the human id) and the row is
    marked decided approved."""
    async def run():
        session, row = _session_for("ebay_post_transaction")

        @asynccontextmanager
        async def _fake_ws(*args):
            yield session

        with (
            patch.object(approvals, "workspace_session", _fake_ws),
            patch.object(approvals, "_record_ebay_decision", AsyncMock()) as record,
            patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
            patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
            patch.object(approvals, "_project_approval_decision"),
        ):
            ctx, auth = _human_ctx(), _human_auth()
            outcome = await approvals.decide_approval(
                WID, UUID(row["id"]),
                approvals.DecideRequest(decision="approve"), ctx, auth,
            )
        assert outcome["ok"] is True and outcome["decision"] == "approved"
        assert outcome["applied"] is False  # queued_for_sync, not posted now
        assert record.await_count == 1
        assert record.call_args.kwargs["decision"] == "approve"
        assert record.call_args.kwargs["decided_by"] == HUMAN
        assert mark.call_args.kwargs["new_status"] == "approved"

    asyncio.run(run())


# --------------------------------------------------------------------------
# PWR-99: the gate now covers compliance_rule too. Approve activates a proposed
# rule, so an agent or PAT caller gets 403 and the activate helper never runs;
# a human session still gets through.
# --------------------------------------------------------------------------
def test_agent_and_pat_cannot_approve_compliance_rule():
    """Agent + PAT approve of a compliance_rule gets 403 BEFORE the activate
    helper runs: no rule activation, row never marked decided."""
    for caller in (_agent_caller, _pat_caller):
        async def run():
            session, row = _session_for("compliance_rule")

            @asynccontextmanager
            async def _fake_ws(*args):
                yield session

            ctx, auth = caller()
            with (
                patch.object(approvals, "workspace_session", _fake_ws),
                patch.object(approvals, "_approve_compliance_rule", AsyncMock()) as activate,
                patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
                patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
                patch.object(approvals, "_project_approval_decision"),
            ):
                with pytest.raises(HTTPException) as denial:
                    await approvals.decide_approval(
                        WID, UUID(row["id"]),
                        approvals.DecideRequest(decision="approve"), ctx, auth,
                    )
            assert denial.value.status_code == 403
            assert "human session" in denial.value.detail
            activate.assert_not_awaited()
            mark.assert_not_awaited()
            assert row["status"] == "pending"

        asyncio.run(run())


def test_human_session_still_approves_compliance_rule():
    """A verified interactive human still approves a compliance_rule: the
    activate helper runs and the row is marked decided approved."""
    async def run():
        session, row = _session_for("compliance_rule")

        @asynccontextmanager
        async def _fake_ws(*args):
            yield session

        with (
            patch.object(approvals, "workspace_session", _fake_ws),
            patch.object(approvals, "_approve_compliance_rule", AsyncMock(
                return_value={"rule_id": str(uuid4()), "active_profile_id": str(uuid4())}
            )) as activate,
            patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
            patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
            patch.object(approvals, "_project_approval_decision"),
        ):
            ctx, auth = _human_ctx(), _human_auth()
            outcome = await approvals.decide_approval(
                WID, UUID(row["id"]),
                approvals.DecideRequest(decision="approve"), ctx, auth,
            )
        assert outcome["ok"] is True and outcome["decision"] == "approved"
        assert outcome["applied"] is True
        assert activate.await_count == 1
        assert mark.call_args.kwargs["new_status"] == "approved"

    asyncio.run(run())
