"""Manual real-ledger proof; ONLY a fresh disposable migrated proof database.
See PENDING-JOURNAL-REVIEW.md for exact setup. No production credentials.
"""

import asyncio
import json
import os
from uuid import UUID, uuid4
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.workspace_session import workspace_session
from app.journal.pending_post import JournalRequest, existing_proposal, enqueue, apply_proposal
from app.journal.hash_chain import verify_chain

W = UUID("11111111-1111-4111-8111-111111111111")
E = UUID("bf74827c-c532-5f7e-b50a-a77668d6d717")


async def main():
    dsn = os.environ["AO_PENDING_PROOF_DSN"]
    assert "@127.0.0.1:55439/" in dsn
    engine = create_async_engine(dsn)
    sf = async_sessionmaker(engine)
    async with workspace_session(sf, W) as s:
        await s.execute(
            text(
                "UPDATE chart_of_account SET linked_external_account_id='proof-bank', linked_external_source='proof' WHERE entity_id=:e AND account_number='1010'"
            ),
            {"e": str(E)},
        )
        await s.execute(
            text(
                "INSERT INTO account_entity_mapping(id,workspace_id,entity_id,source,external_account_id,auto_categorize) VALUES(:id,:w,:e,'proof','proof-bank',true) ON CONFLICT DO NOTHING"
            ),
            {"id": str(uuid4()), "w": str(W), "e": str(E)},
        )
    req = JournalRequest(
        source="proof",
        source_external_id="proof-" + str(uuid4()),
        effective_date="2026-09-15",
        amount_cents=200000,
        description="Isolated fixture",
        category="Owner Contribution",
        category_status="auto",
        sign_matched=True,
        external_account_id="proof-bank",
        receipt_link_id=None,
        reasoning_trace="test",
        model_version="test",
    ).checked()
    async with workspace_session(sf, W) as s:
        assert await existing_proposal(s, workspace_id=W, entity_id=E, request=req) is None
        aid = await enqueue(
            s, workspace_id=W, entity_id=E, request=req, actor="test-agent", reason="H"
        )
        count = (await s.execute(text("SELECT count(*) FROM journal_entry"))).scalar_one()
        assert count == 0
    async with workspace_session(sf, W) as s:
        payload = (
            await s.execute(text("SELECT payload FROM approval_queue WHERE id=:id"), {"id": aid})
        ).scalar_one()
    try:
        async with workspace_session(sf, W) as s:
            await apply_proposal(
                s, workspace_id=W, entity_id=E, payload=payload, approving_human="test-human"
            )
            raise RuntimeError("simulated audit failure")
    except RuntimeError:
        pass
    async with workspace_session(sf, W) as s:
        assert (await s.execute(text("SELECT count(*) FROM journal_entry"))).scalar_one() == 0
    async with workspace_session(sf, W) as s:
        await apply_proposal(
            s, workspace_id=W, entity_id=E, payload=payload, approving_human="test-human"
        )
    async with workspace_session(sf, W) as s:
        assert (await s.execute(text("SELECT count(*) FROM journal_entry"))).scalar_one() == 1
        assert await verify_chain(s, str(E)) == []
        row = (
            await s.execute(
                text("SELECT source,source_external_id,approving_human_uc_uid FROM journal_entry")
            )
        ).first()
        assert (
            row.source == "proof"
            and row.source_external_id == req.source_external_id
            and row.approving_human_uc_uid == "test-human"
        )
    print(
        json.dumps(
            {
                "real_migrations": "public0016/tenant0031",
                "queued_without_ledger": True,
                "apply_rollback_no_ledger": True,
                "actual_post_exact_source_human": True,
                "hash_chain_valid": True,
            }
        )
    )
    await engine.dispose()


asyncio.run(main())
