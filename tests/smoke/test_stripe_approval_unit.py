"""Offline tests for the existing approval endpoint's Stripe consumer."""
from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.api import approvals
from app.connectors.stripe import mapper as stripe_mapper


@pytest.mark.asyncio
async def test_stripe_approval_retries_mapper_with_payload_and_human_gate(monkeypatch):
    captured: dict = {}
    journal_entry_id = uuid4()

    async def fake_mapper(session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            outcome="posted",
            journal_entry_id=journal_entry_id,
            reason="fictional success",
        )

    monkeypatch.setattr(
        stripe_mapper, "map_and_post_balance_transaction", fake_mapper
    )
    payload = {
        "action": "stripe_post_balance_transaction",
        "stripe_balance_txn_id": "txn_quarantined_1",
        "stripe_type": "charge",
        "amount_cents": 1250,
        "effective_date": "2026-11-08",
        "debit_account": "6100·Chosen Debit",
        "credit_account": "4300·Chosen Credit",
        "stripe_txn": {
            "id": "txn_quarantined_1",
            "type": "charge",
            "amount": 1250,
            "fee": 25,
            "created": 1794096000,
        },
    }

    result = await approvals._approve_stripe_balance_transaction(
        object(),
        workspace_id=uuid4(),
        entity_id=uuid4(),
        payload=payload,
        approving_human="fictional-reviewer",
        note="fictional operator note",
    )

    assert result["source_external_id"] == "txn_quarantined_1"
    assert captured["txn"]["fee"] == 25
    assert captured["account_override"] == (
        "6100·Chosen Debit",
        "4300·Chosen Credit",
    )
    assert captured["human_approved"] is True
    assert captured["actor_uc_uid"] == "fictional-reviewer"
