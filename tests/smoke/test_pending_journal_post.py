"""Strict immutable journal proposal validation and exact account binding."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.journal.pending_post import JournalRequest, apply_proposal, existing_proposal, proposal_id


def request(**changes):
    data = dict(
        source="demo-seed",
        source_external_id="fixture-1",
        effective_date="2026-09-15",
        amount_cents=-200000,
        description="Synthetic rent",
        category="Rent",
        category_status="auto",
        sign_matched=True,
        external_account_id="demo-checking",
        receipt_link_id=None,
        reasoning_trace="Synthetic fixture",
        model_version="fixture-v1",
    )
    return JournalRequest(**(data | changes)).checked()


def envelope(wid, eid, req):
    return dict(
        version=1,
        workspace_id=str(wid),
        entity_id=str(eid),
        request=req.model_dump(),
        debit_account="5000·Rent",
        credit_account="1010·Cash",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"amount_cents": True},
        {"amount_cents": "200"},
        {"amount_cents": 0},
        {"effective_date": "not-date"},
        {"receipt_link_id": "not-id"},
        {"extra": "forbidden"},
        {"category_status": "human_override"},
    ],
)
def test_malformed_requests_rejected(changes):
    with pytest.raises((ValueError, ValidationError)):
        request(**changes)


def test_identity_is_independent_of_payload_and_scoped():
    wid, eid = uuid4(), uuid4()
    original = proposal_id(wid, eid, request())
    assert proposal_id(wid, eid, request(amount_cents=-100)) == original
    assert proposal_id(uuid4(), eid, request()) != original
    assert proposal_id(wid, uuid4(), request()) != original
    assert proposal_id(wid, eid, request(source="different-source")) != original


def test_conflicting_replay_rejected_before_post():
    result = SimpleNamespace(
        mappings=lambda: SimpleNamespace(
            first=lambda: {
                "id": str(uuid4()),
                "status": "pending",
                "payload": {"request": request().model_dump()},
            }
        )
    )
    session = SimpleNamespace(execute=AsyncMock(return_value=result))
    with pytest.raises(ValueError, match="Conflicting replay"):
        asyncio.run(
            existing_proposal(
                session, workspace_id=uuid4(), entity_id=uuid4(), request=request(amount_cents=-1)
            )
        )


@pytest.mark.parametrize("change", ["scope", "version", "account", "mapping"])
def test_apply_refuses_scope_mapping_or_account_drift(change):
    wid, eid = uuid4(), uuid4()
    payload = envelope(wid, eid, request())
    if change == "scope":
        payload["workspace_id"] = str(uuid4())
    if change == "version":
        payload["version"] = True
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(first=lambda: None if change == "mapping" else (1,))
        )
    )
    pair = SimpleNamespace(debit_account="changed", credit_account="1010·Cash")
    with (
        patch("app.journal.pending_post.resolve_accounts_for_txn", AsyncMock(return_value=pair)),
        patch("app.journal.pending_post.post_journal_entry", AsyncMock()) as post,
    ):
        with pytest.raises(ValueError):
            asyncio.run(
                apply_proposal(
                    session,
                    workspace_id=wid,
                    entity_id=eid,
                    payload=payload,
                    approving_human="human",
                )
            )
        post.assert_not_awaited()


def test_apply_preserves_exact_request_and_human():
    wid, eid = uuid4(), uuid4()
    req = request(memo="exact memo", payee="Fiction Landlord")
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: (1,))))
    pair = SimpleNamespace(debit_account="5000·Rent", credit_account="1010·Cash")
    posted = SimpleNamespace(journal_entry_id=uuid4(), materiality=SimpleNamespace(tier="H"))
    with (
        patch("app.journal.pending_post.resolve_accounts_for_txn", AsyncMock(return_value=pair)),
        patch(
            "app.journal.pending_post.post_journal_entry", AsyncMock(return_value=posted)
        ) as post,
        patch("app.tools.journal_tools._derived_sign_matched", AsyncMock(return_value=True)),
        patch("app.journal.posting.assert_period_open", AsyncMock(return_value=None)),
    ):
        asyncio.run(
            apply_proposal(
                session,
                workspace_id=wid,
                entity_id=eid,
                payload=envelope(wid, eid, req),
                approving_human="human-reviewer",
            )
        )
        args = post.call_args.kwargs
        for key in [
            "source",
            "source_external_id",
            "amount_cents",
            "category",
            "external_account_id",
            "memo",
            "payee",
        ]:
            assert args[key] == getattr(req, key)
        assert args["approving_human_uc_uid"] == "human-reviewer"
        assert args["posted_by_agent_id"] is None


def test_core_post_checks_actual_second_resolution_before_any_write():
    from app.journal.posting import AccountPair, post_journal_entry
    from app.journal.materiality import MaterialityVerdict

    session = SimpleNamespace(execute=AsyncMock())
    with (
        patch(
            "app.journal.posting.evaluate",
            return_value=MaterialityVerdict(tier="H", outcome="auto_post", reason="human"),
        ),
        patch(
            "app.journal.posting.resolve_accounts_for_txn",
            AsyncMock(
                return_value=AccountPair(
                    debit_account="changed-after-review", credit_account="1010·Cash"
                )
            ),
        ),
    ):
        result = asyncio.run(
            post_journal_entry(
                session,
                workspace_id=str(uuid4()),
                entity_id=str(uuid4()),
                source="demo",
                source_external_id="exact",
                effective_date=__import__("datetime").date(2026, 9, 15),
                amount_cents=-200000,
                description="Fixture",
                payee=None,
                memo=None,
                category="Rent",
                category_status="auto",
                entity_mapped=True,
                sign_matched=True,
                external_account_id="bank",
                has_receipt_link=False,
                receipt_link_id=None,
                posted_by_uc_uid="human",
                user_confirmed=True,
                expected_account_pair=AccountPair(
                    debit_account="5000·Rent", credit_account="1010·Cash"
                ),
            )
        )
        assert result.reason == "Approved account pair changed"
        session.execute.assert_not_awaited()


def test_foreign_or_unrelated_receipt_cannot_attest_request():
    from app.journal.pending_post import validate_receipt

    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(first=lambda: None)))
    with pytest.raises(ValueError, match="Receipt link does not attest"):
        asyncio.run(
            validate_receipt(
                session,
                workspace_id=uuid4(),
                entity_id=uuid4(),
                request=request(receipt_link_id=str(uuid4())),
            )
        )
    query, params = session.execute.call_args.args
    assert "r.workspace_id=:wid" in str(query) and "j.source=:src" in str(query)
    assert params["ext"] == "fixture-1"
