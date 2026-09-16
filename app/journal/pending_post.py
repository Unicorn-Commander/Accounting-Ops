"""Immutable journal proposals. No decision or ledger write occurs on enqueue."""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from uuid import UUID, NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.journal.posting import PostingRefusal, post_journal_entry, resolve_accounts_for_txn

SUBJECT = "journal_entry_post"


class JournalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(min_length=1, max_length=64)
    source_external_id: str = Field(min_length=1, max_length=256)
    effective_date: str
    amount_cents: int = Field(ge=-100_000_000_000, le=100_000_000_000)
    description: str = Field(min_length=1, max_length=512)
    category: str = Field(min_length=1, max_length=64)
    category_status: str
    payee: str | None = Field(default=None, max_length=256)
    memo: str | None = None
    sign_matched: bool
    external_account_id: str | None
    receipt_link_id: str | None
    reasoning_trace: str
    model_version: str = Field(max_length=128)

    def checked(self):
        dt.date.fromisoformat(self.effective_date)
        if not self.amount_cents or self.category_status not in {"auto", "suggest", "unknown"}:
            raise ValueError("Invalid journal amount or category status")
        if self.receipt_link_id:
            UUID(self.receipt_link_id)
        return self


def proposal_id(workspace_id, entity_id, request: JournalRequest):
    key = json.dumps(
        [str(workspace_id), str(entity_id), request.source, request.source_external_id]
    )
    return uuid5(NAMESPACE_URL, "accounting-ops:journal-entry-post:" + key)


async def existing_proposal(session, *, workspace_id, entity_id, request):
    """Serialize all retries, including a changed request that would auto-post."""
    ident = proposal_id(workspace_id, entity_id, request)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": str(ident)}
    )
    row = (
        (
            await session.execute(
                text(
                    "SELECT id::text, status, payload FROM approval_queue "
                    "WHERE id = :id AND workspace_id = :wid AND entity_id = :eid"
                ),
                {"id": str(ident), "wid": str(workspace_id), "eid": str(entity_id)},
            )
        )
        .mappings()
        .first()
    )
    if row and row["payload"].get("request") != request.model_dump():
        raise ValueError("Conflicting replay: journal proposal payload is immutable")
    return row


async def validate_receipt(session, *, workspace_id, entity_id, request):
    if not request.receipt_link_id:
        return
    row = (
        await session.execute(
            text("""
        SELECT 1 FROM recon_link r JOIN journal_entry j ON j.id = r.journal_entry_id
        WHERE r.id=:rid AND r.workspace_id=:wid AND r.entity_id=:eid
          AND r.receipt_id IS NOT NULL AND j.workspace_id=:wid AND j.entity_id=:eid
          AND j.source=:src AND j.source_external_id=:ext
          AND abs(r.amount) = :amount
    """),
            {
                "rid": request.receipt_link_id,
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "src": request.source,
                "ext": request.source_external_id,
                "amount": Decimal(abs(request.amount_cents)) / 100,
            },
        )
    ).first()
    if row is None:
        raise ValueError("Receipt link does not attest this workspace/entity/source transaction")


async def enqueue(session, *, workspace_id, entity_id, request, actor, reason):
    request.checked()
    await validate_receipt(session, workspace_id=workspace_id, entity_id=entity_id, request=request)
    if not request.external_account_id:
        raise ValueError("Pending journal proposal requires an explicit external account")
    mapping = (
        await session.execute(
            text(
                "SELECT 1 FROM account_entity_mapping WHERE entity_id=:eid AND source=:src "
                "AND external_account_id=:ext LIMIT 1"
            ),
            {"eid": str(entity_id), "src": request.source, "ext": request.external_account_id},
        )
    ).first()
    if mapping is None:
        raise ValueError("Pending journal proposal requires an exact entity/source/account mapping")
    pair = await resolve_accounts_for_txn(
        session,
        entity_id=str(entity_id),
        category=request.category,
        amount_cents=request.amount_cents,
        external_account_id=request.external_account_id,
        source=request.source,
    )
    if pair is None:
        raise ValueError("Pending journal proposal accounts cannot be resolved")
    ident = proposal_id(workspace_id, entity_id, request)
    payload = {
        "version": 1,
        "workspace_id": str(workspace_id),
        "entity_id": str(entity_id),
        "request": request.model_dump(),
        "debit_account": pair.debit_account,
        "credit_account": pair.credit_account,
    }
    await session.execute(
        text("""
        INSERT INTO approval_queue (id, workspace_id, entity_id, subject_type, subject_id,
          amount, currency_code, usd_equivalent_at_event_time, status, reasoning, requested_by_identity, payload)
        VALUES (:id, :wid, :eid, 'journal_entry_post', :id, :amount, 'USD', :amount,
          'pending', :reason, :actor, CAST(:payload AS jsonb))
    """),
        {
            "id": str(ident),
            "wid": str(workspace_id),
            "eid": str(entity_id),
            "amount": Decimal(abs(request.amount_cents)) / 100,
            "reason": reason,
            "actor": actor,
            "payload": json.dumps(payload),
        },
    )
    return str(ident)


async def apply_proposal(session, *, workspace_id, entity_id, payload, approving_human):
    if set(payload) != {
        "version",
        "workspace_id",
        "entity_id",
        "request",
        "debit_account",
        "credit_account",
    }:
        raise ValueError("Malformed journal proposal envelope")
    if type(payload["version"]) is not int or payload["version"] != 1:
        raise ValueError("Unsupported journal proposal version")
    if payload["workspace_id"] != str(workspace_id) or payload["entity_id"] != str(entity_id):
        raise ValueError("Journal proposal scope mismatch")
    req = JournalRequest.model_validate(payload["request"]).checked()
    await validate_receipt(session, workspace_id=workspace_id, entity_id=entity_id, request=req)
    mapping = (
        await session.execute(
            text(
                "SELECT 1 FROM account_entity_mapping WHERE entity_id=:eid AND source=:src "
                "AND external_account_id=:ext LIMIT 1"
            ),
            {"eid": str(entity_id), "src": req.source, "ext": req.external_account_id},
        )
    ).first()
    if mapping is None:
        raise ValueError("Journal proposal account mapping changed")
    pair = await resolve_accounts_for_txn(
        session,
        entity_id=str(entity_id),
        category=req.category,
        amount_cents=req.amount_cents,
        external_account_id=req.external_account_id,
        source=req.source,
    )
    if pair is None or (pair.debit_account, pair.credit_account) != (
        payload["debit_account"],
        payload["credit_account"],
    ):
        raise ValueError("Journal proposal accounts changed; submit a newly reviewed request with a new external ID")
    from app.journal.posting import assert_period_open

    if (
        await assert_period_open(
            session,
            entity_id=str(entity_id),
            effective_date=dt.date.fromisoformat(req.effective_date),
        )
        is not None
    ):
        raise ValueError("Period closed; journal proposal remains pending")

    from app.tools.journal_tools import _derived_sign_matched

    data = req.model_dump()
    data["sign_matched"] = req.sign_matched and await _derived_sign_matched(
        session, entity_id=entity_id, category=req.category, amount_cents=req.amount_cents
    )

    data["effective_date"] = dt.date.fromisoformat(req.effective_date)
    data["receipt_link_id"] = UUID(req.receipt_link_id) if req.receipt_link_id else None
    result = await post_journal_entry(
        session,
        workspace_id=str(workspace_id),
        entity_id=str(entity_id),
        **data,
        entity_mapped=True,
        has_receipt_link=bool(req.receipt_link_id),
        posted_by_uc_uid=approving_human,
        posted_by_agent_id=None,
        user_confirmed=True,
        approving_human_uc_uid=approving_human,
        expected_account_pair=pair,
    )
    if isinstance(result, PostingRefusal):
        raise ValueError("Journal proposal refused: " + result.reason)
    return {
        "journal_entry_id": str(result.journal_entry_id),
        "materiality_tier": result.materiality.tier,
        "source_external_id": req.source_external_id,
    }
