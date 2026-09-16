"""Agent confirmation claims never authorize a new journal posting.

Human decisions apply immutable proposals within the locked decision transaction;
a legacy bank-import approval with matching amount/ID is not authority to post
another source, category, date or account.
"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.journal.posting import PostingRefusal
from app.tools import journal_tools


@pytest.mark.parametrize("claimed", [True, False])
def test_claim_cannot_bypass_and_refusal_enqueues_atomically(claimed):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)))
    ctx = SimpleNamespace(user_uc_uid="agent-owner", agent_id="agent")

    @asynccontextmanager
    async def ws(*args):
        yield session

    with (
        patch.object(journal_tools, "authorize_workspace", AsyncMock(return_value=ctx)),
        patch.object(journal_tools, "workspace_session", ws),
        patch.object(journal_tools, "_derived_sign_matched", AsyncMock(return_value=True)),
        patch.object(
            journal_tools,
            "_journal_post",
            AsyncMock(
                return_value=PostingRefusal(
                    outcome="blocked_materiality", reason="Human confirmation required"
                )
            ),
        ) as post,
        patch("app.journal.pending_post.existing_proposal", AsyncMock(return_value=None)),
        patch("app.journal.pending_post.enqueue", AsyncMock(return_value="proposal-id")) as enqueue,
        patch.object(journal_tools, "write_decision_audit", AsyncMock(return_value=uuid4())),
        patch.object(journal_tools, "project_decision_audit"),
    ):
        result = asyncio.run(
            journal_tools.post_journal_entry(
                workspace_id=uuid4(),
                entity_id=uuid4(),
                source="demo-seed",
                source_external_id="exact-id",
                effective_date="2026-09-15",
                amount_cents=-200000,
                description="Synthetic rent",
                category="Rent",
                external_account_id="demo-checking",
                user_confirmed=claimed,
            )
        )
        assert post.call_args.kwargs["user_confirmed"] is False
        assert post.call_args.kwargs["approving_human_uc_uid"] is None
        assert enqueue.call_args.args[0] is session
        assert result["outcome"] == "queued_for_confirm"
        assert result["approval_id"] == "proposal-id"
