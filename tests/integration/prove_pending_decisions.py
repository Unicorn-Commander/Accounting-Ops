"""Actual decision transaction/concurrency proof on the disposable migrated DB."""

import asyncio
import os
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.api import approvals
from app.auth import AuthContext
from app.config import get_settings
from app.journal.hash_chain import verify_chain
from app.journal.pending_post import JournalRequest, existing_proposal, enqueue
from app.tenancy import WorkspaceContext
from app.workspace_session import workspace_session

W = UUID("11111111-1111-4111-8111-111111111111")
E = UUID("bf74827c-c532-5f7e-b50a-a77668d6d717")


async def main():
    dsn = os.environ["AO_PENDING_PROOF_DSN"]
    assert "@127.0.0.1:55439/" in dsn
    engine = create_async_engine(dsn)
    sf = async_sessionmaker(engine)
    ctx = WorkspaceContext(W, "isolated-journal-proof", "owner", "test-human", None, None, False)
    auth = AuthContext(
        "test-human",
        None,
        (),
        {"sid": "test-session", "auth_time": 1789520000, "azp": get_settings().keycloak_client_id},
        may_autoprovision=True,
    )

    async def queue():
        req = JournalRequest(
            source="proof",
            source_external_id=str(uuid4()),
            effective_date="2026-10-01",
            amount_cents=200000,
            description="Isolated decision fixture",
            category="Owner Contribution",
            category_status="auto",
            sign_matched=True,
            external_account_id="proof-bank",
            receipt_link_id=None,
            reasoning_trace="isolated test",
            model_version="test",
        ).checked()
        async with workspace_session(sf, W) as s:
            await existing_proposal(s, workspace_id=W, entity_id=E, request=req)
            return UUID(
                await enqueue(
                    s, workspace_id=W, entity_id=E, request=req, actor="test-agent", reason="H"
                )
            )

    async def decide(aid):
        return await approvals.decide_approval(
            W, aid, approvals.DecideRequest(decision="approve"), ctx, auth
        )

    async def count():
        async with workspace_session(sf, W) as s:
            return (await s.execute(text("SELECT count(*) FROM journal_entry"))).scalar_one()

    baseline = await count()
    aid = await queue()
    with (
        patch.object(approvals, "SessionLocal", sf),
        patch.object(approvals, "_project_approval_decision"),
    ):
        outcomes = await asyncio.gather(decide(aid), decide(aid), return_exceptions=True)
        assert sum(isinstance(x, dict) and x.get("applied") is True for x in outcomes) == 1
        assert sum(isinstance(x, HTTPException) and x.status_code == 409 for x in outcomes) == 1
        assert await count() == baseline + 1
        rollback_id = await queue()
        with patch.object(
            approvals, "write_decision_audit", AsyncMock(side_effect=RuntimeError("audit failure"))
        ):
            try:
                await decide(rollback_id)
                raise AssertionError("Expected audit failure")
            except RuntimeError:
                pass
        assert await count() == baseline + 1
        async with workspace_session(sf, W) as s:
            status = (
                await s.execute(
                    text("SELECT status FROM approval_queue WHERE id=:id"), {"id": str(rollback_id)}
                )
            ).scalar_one()
            assert status == "pending"
            assert await verify_chain(s, str(E)) == []
    print(
        "PASS actual decision concurrency: one apply, one409; audit failure rolls back ledger+decision; chain valid"
    )
    await engine.dispose()


asyncio.run(main())
