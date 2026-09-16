"""Follow-up proof on the same disposable DB created by prove_pending_ledger.py."""

import asyncio
import json
import datetime as dt
import os
from uuid import UUID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.workspace_session import workspace_session
from app.journal.pending_post import apply_proposal
from app.journal.hash_chain import verify_chain

W = UUID("11111111-1111-4111-8111-111111111111")
E = UUID("bf74827c-c532-5f7e-b50a-a77668d6d717")


async def main():
    dsn = os.environ["AO_PENDING_PROOF_DSN"]
    assert "@127.0.0.1:55439/" in dsn
    e = create_async_engine(dsn)
    sf = async_sessionmaker(e)
    async with workspace_session(sf, W) as s:
        payload = (
            await s.execute(
                text(
                    "SELECT payload FROM approval_queue WHERE subject_type='journal_entry_post' LIMIT 1"
                )
            )
        ).scalar_one()
        payload["request"]["source_external_id"] = "closed-period-proof"
        await s.execute(
            text(
                "INSERT INTO closed_period(entity_id,workspace_id,period_end,closed_by_uc_uid,reason) VALUES(:e,:w,:d,'test-human','isolated proof') ON CONFLICT DO NOTHING"
            ),
            {"e": str(E), "w": str(W), "d": dt.date(2026, 9, 30)},
        )
    try:
        async with workspace_session(sf, W) as s:
            await apply_proposal(
                s, workspace_id=W, entity_id=E, payload=payload, approving_human="test-human"
            )
        raise AssertionError("Closed-period post was accepted")
    except ValueError as ex:
        assert "Period closed" in str(ex), str(ex)
    async with workspace_session(sf, W) as s:
        assert (await s.execute(text("SELECT count(*) FROM journal_entry"))).scalar_one() == 1
        assert await verify_chain(s, str(E)) == []
    print(
        json.dumps(
            {"real_closed_period_refused": True, "ledger_unchanged": True, "hash_chain_valid": True}
        )
    )
    await e.dispose()


asyncio.run(main())
