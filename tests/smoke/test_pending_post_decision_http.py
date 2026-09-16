"""Registered FastAPI decision route: human authority, scope and legacy compatibility."""

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


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("human", 200),
        ("pat", 403),
        ("service", 403),
        ("agent", 403),
        ("m2m", 403),
        ("untrusted_service_account", 403),
        ("other_browser_client", 403),
        ("sid_machine", 403),
        ("invalid_auth_time", 403),
        ("foreign_entity", 403),
        ("viewer", 403),
        ("override", 422),
        ("malformed", 409),
    ],
)
def test_registered_decision_authority(kind, expected):
    async def run():
        wid, eid, aid = uuid4(), uuid4(), uuid4()
        ctx = WorkspaceContext(wid, "isolated", "accountant", "human", None, None, False)
        auth = AuthContext(
            "human",
            None,
            (),
            {
                "sid": "verified-session",
                "azp": get_settings().keycloak_client_id,
                "auth_time": 1789520000,
            },
            may_autoprovision=True,
        )
        if kind == "pat":
            ctx = replace(ctx, pat_scopes=("write:approvals",))
            auth = replace(auth, may_autoprovision=False)
        if kind == "service":
            auth = replace(auth, may_autoprovision=False, scopes=("service",))
        if kind == "agent":
            auth = replace(auth, agent_id="agent")
        if kind == "m2m":
            auth = replace(auth, is_trusted_m2m=True)
        if kind == "untrusted_service_account":
            auth = replace(auth, claims={"preferred_username": "service-account-other-client"})
        if kind == "other_browser_client":
            auth = replace(auth, claims={"sid": "verified-session", "azp": "another-client"})
        if kind == "sid_machine":
            auth = replace(
                auth,
                claims={
                    "sid": "machine-session",
                    "auth_time": 1789520000,
                    "azp": get_settings().keycloak_client_id,
                    "clientId": get_settings().keycloak_client_id,
                },
            )
        if kind == "invalid_auth_time":
            auth = replace(auth, claims={**auth.claims, "auth_time": True})
        if kind == "foreign_entity":
            ctx = replace(ctx, entity_scope=(uuid4(),))
        if kind == "viewer":
            ctx = replace(ctx, role="viewer")
        row = dict(
            id=str(aid),
            entity_id=str(eid),
            subject_type="journal_entry_post",
            subject_id=str(aid),
            status="pending",
            payload={},
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
        apply = AsyncMock(return_value={"journal_entry_id": str(uuid4()), "materiality_tier": "H"})
        if kind == "malformed":
            apply.side_effect = ValueError("Invalid immutable envelope")
        with (
            patch.object(approvals, "workspace_session", ws),
            patch("app.journal.pending_post.apply_proposal", apply),
            patch.object(approvals, "_mark_decided", AsyncMock()) as mark,
            patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
            patch.object(approvals, "_project_approval_decision"),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as c:
                body = {"decision": "approve"}
                if kind == "override":
                    body["category_override"] = "Different"
                result = await c.post(f"/api/workspaces/{wid}/approvals/{aid}/decide", json=body)
            assert result.status_code == expected, result.text
            if expected != 200:
                mark.assert_not_awaited()
            else:
                assert apply.call_args.kwargs["approving_human"] == "human"

    asyncio.run(run())


def test_legacy_direct_tool_delegate_keeps_working_without_new_auth_parameter():
    async def run():
        wid, eid, aid = uuid4(), uuid4(), uuid4()
        ctx = WorkspaceContext(wid, "isolated", "admin", "owner", "legacy-agent", None, False)
        row = dict(
            id=str(aid),
            entity_id=str(eid),
            subject_type="bank_transaction_import",
            subject_id=str(aid),
            status="pending",
            payload={},
            reasoning="legacy",
        )
        session = SimpleNamespace(
            execute=AsyncMock(
                return_value=SimpleNamespace(mappings=lambda: SimpleNamespace(first=lambda: row))
            )
        )

        @asynccontextmanager
        async def ws(*args):
            yield session

        with (
            patch.object(approvals, "workspace_session", ws),
            patch.object(
                approvals,
                "_approve_bank_transaction_import",
                AsyncMock(return_value={"journal_entry_id": str(uuid4())}),
            ),
            patch.object(approvals, "_mark_decided", AsyncMock()),
            patch.object(approvals, "write_decision_audit", AsyncMock(return_value=uuid4())),
            patch.object(approvals, "_project_approval_decision"),
        ):
            result = await approvals.decide_approval(
                wid, aid, approvals.DecideRequest(decision="approve"), ctx
            )
            assert result["applied"] is True

    asyncio.run(run())


def test_new_proposal_cannot_be_decided_or_hidden_through_tools_or_skip():
    from fastapi import HTTPException
    from app.tools import approvals as tools
    from app.api import approval_clusters

    async def run():
        wid, eid, aid = uuid4(), uuid4(), uuid4()
        ctx = WorkspaceContext(wid, "isolated", "admin", "owner", "agent", None, False)
        row = dict(
            id=str(aid),
            entity_id=str(eid),
            subject_type="journal_entry_post",
            subject_id=str(aid),
            status="pending",
            payload={},
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

        with (
            patch.object(approvals, "workspace_session", ws),
            patch.object(approval_clusters, "workspace_session", ws),
            patch.object(tools, "authorize_workspace", AsyncMock(return_value=ctx)),
        ):
            with pytest.raises(HTTPException) as denial:
                await tools.decide_approval(wid, aid, "approve")
            assert denial.value.status_code == 403
            bulk = await tools.bulk_decide_approvals(
                wid, [{"approval_id": str(aid), "decision": "approve"}]
            )
            assert bulk["failed"] == 1 and bulk["approved"] == 0
            for fn in [approvals.skip_approval, approvals.unskip_approval]:
                with pytest.raises(HTTPException) as denial:
                    await fn(wid, aid, approvals.SkipRequest(), ctx)
                assert denial.value.status_code == 409
            skipped = await approval_clusters.bulk_decide(
                wid,
                approval_clusters.BulkDecideRequest(decision="skip", approval_ids=[str(aid)]),
                ctx,
            )
            assert skipped["failed"] == 1 and skipped["skipped"] == 0

    asyncio.run(run())
