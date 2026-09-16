"""Real PostgreSQL transaction/lock/RLS tests on an explicitly isolated DB.

Run only with AO_PENDING_PROOF_DSN pointing at the disposable proof container.
Account resolution is stubbed here; real ledger/hash-chain coverage is separate.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.journal.pending_post import enqueue, existing_proposal, proposal_id
from tests.smoke.test_pending_journal_post import request


@pytest.mark.skipif(
    not os.getenv("AO_PENDING_PROOF_DSN"), reason="Explicit disposable proof DB required"
)
def test_queue_concurrency_rollback_and_workspace_isolation():
    async def run():
        dsn = os.environ["AO_PENDING_PROOF_DSN"]
        assert ":55439/" in dsn, "Never use an application database for this destructive fixture"
        engine = create_async_engine(dsn)
        sessions = async_sessionmaker(engine)
        wid, other, eid = uuid4(), uuid4(), uuid4()
        async with engine.begin() as c:
            await c.execute(text("DROP SCHEMA IF EXISTS pending_proof CASCADE"))
            await c.execute(text("CREATE SCHEMA pending_proof"))
            await c.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE pending_proof_user; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            await c.execute(text("GRANT USAGE ON SCHEMA pending_proof TO pending_proof_user"))
            await c.execute(
                text("""CREATE TABLE pending_proof.approval_queue (
                id uuid PRIMARY KEY, workspace_id uuid NOT NULL, entity_id uuid NOT NULL,
                subject_type text, subject_id uuid, amount numeric, currency_code text,
                usd_equivalent_at_event_time numeric, status text, reasoning text,
                requested_by_identity text, payload jsonb)""")
            )
            await c.execute(
                text("""CREATE TABLE pending_proof.account_entity_mapping (
                workspace_id uuid, entity_id uuid, source text, external_account_id text)""")
            )
            for table in ["approval_queue", "account_entity_mapping"]:
                await c.execute(
                    text(f"ALTER TABLE pending_proof.{table} ENABLE ROW LEVEL SECURITY")
                )
                await c.execute(text(f"ALTER TABLE pending_proof.{table} FORCE ROW LEVEL SECURITY"))
                await c.execute(
                    text(f"""CREATE POLICY workspace_guard ON pending_proof.{table}
                    USING (workspace_id = current_setting('app.current_workspace')::uuid)
                    WITH CHECK (workspace_id = current_setting('app.current_workspace')::uuid)""")
                )
                await c.execute(
                    text(f"GRANT SELECT,INSERT ON pending_proof.{table} TO pending_proof_user")
                )
            await c.execute(
                text("INSERT INTO pending_proof.account_entity_mapping VALUES (:w,:e,:s,:a)"),
                dict(w=str(wid), e=str(eid), s="demo-seed", a="demo-checking"),
            )

        async def pin(s, workspace):
            await s.execute(text("SET LOCAL ROLE pending_proof_user"))
            await s.execute(text("SET LOCAL search_path=pending_proof,public"))
            await s.execute(
                text("SELECT set_config('app.current_workspace',:w,true)"), {"w": str(workspace)}
            )

        async def attempt(req, *, rollback=False):
            async with sessions() as s, s.begin():
                await pin(s, wid)
                existing = await existing_proposal(s, workspace_id=wid, entity_id=eid, request=req)
                if existing:
                    return existing["id"]
                ident = await enqueue(
                    s, workspace_id=wid, entity_id=eid, request=req, actor="agent", reason="M/H"
                )
                await asyncio.sleep(0.03)
                if rollback:
                    raise RuntimeError("Audit failure rolls queue back")
                return ident

        pair = SimpleNamespace(debit_account="5000·Rent", credit_account="1010·Cash")
        with patch(
            "app.journal.pending_post.resolve_accounts_for_txn", AsyncMock(return_value=pair)
        ):
            results = await asyncio.gather(attempt(request()), attempt(request()))
            assert results[0] == results[1]
            with pytest.raises(ValueError, match="Conflicting replay"):
                await attempt(request(amount_cents=-1))
            rolled = request(source_external_id="rollback")
            with pytest.raises(RuntimeError):
                await attempt(rolled, rollback=True)
        async with sessions() as s, s.begin():
            await pin(s, wid)
            rows = (await s.execute(text("SELECT id FROM approval_queue"))).all()
            assert len(rows) == 1
            assert rows[0][0] != proposal_id(wid, eid, rolled)
        async with sessions() as s, s.begin():
            await pin(s, other)
            assert (await s.execute(text("SELECT count(*) FROM approval_queue"))).scalar_one() == 0
        await engine.dispose()

    asyncio.run(run())
