"""Immutable, non-cash manual journal proposals.

The proposal path deliberately has no ledger side effect.  It resolves and
binds the chart accounts at enqueue time, records the deterministic split
pairing in the payload, and leaves application to the human approval route.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text

from app.journal.posting import (
    AccountPair,
    PostingRefusal,
    assert_period_open,
    post_manual_journal_pair,
)


SUBJECT_TYPE = "manual_journal_entry_proposal"
PAYLOAD_VERSION = 1
MAX_TOTAL_CENTS = 100_000_000_000
MAX_EVIDENCE_LINKS = 50
MAX_EVIDENCE_LENGTH = 2048
_INTERNAL_EVIDENCE = re.compile(
    r"^(?:doc|document|receipt|internal-document):[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$"
)


class ManualJournalLine(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account_code: str = Field(min_length=1, max_length=32)
    side: Literal["debit", "credit"]
    amount_cents: int = Field(gt=0, le=MAX_TOTAL_CENTS)

    @field_validator("account_code")
    @classmethod
    def account_code_is_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("account_code must not be blank")
        return value.strip()


class ManualJournalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: str = Field(min_length=1, max_length=256)
    effective_date: str
    memo: str = Field(min_length=1, max_length=512)
    lines: list[ManualJournalLine] = Field(min_length=2, max_length=50)
    evidence_links: list[str] = Field(default_factory=list, max_length=MAX_EVIDENCE_LINKS)
    reasoning_trace: str = Field(default="", max_length=4000)
    model_version: str = Field(default="", max_length=128)

    @field_validator("idempotency_key", "memo")
    @classmethod
    def text_is_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("evidence_links")
    @classmethod
    def evidence_is_reference_only(cls, values: list[str]) -> list[str]:
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("evidence_links must not contain blank references")
            if len(value) > MAX_EVIDENCE_LENGTH:
                raise ValueError("evidence link is too long")
            if value in seen:
                raise ValueError("evidence_links must not contain duplicates")
            seen.add(value)
            parsed = urlsplit(value)
            if parsed.scheme == "https" and parsed.netloc:
                continue
            if not _INTERNAL_EVIDENCE.fullmatch(value):
                raise ValueError(
                    "evidence links must be HTTPS URLs or scoped internal document identifiers"
                )
        return values

    def checked(self) -> "ManualJournalRequest":
        try:
            dt.date.fromisoformat(self.effective_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("effective_date must be YYYY-MM-DD") from exc
        if not self.memo.strip():
            raise ValueError("memo must not be blank")
        debit = sum(line.amount_cents for line in self.lines if line.side == "debit")
        credit = sum(line.amount_cents for line in self.lines if line.side == "credit")
        if not debit or not credit:
            raise ValueError("manual journal requires at least one debit and one credit line")
        if debit != credit:
            raise ValueError("manual journal debit and credit totals must balance")
        if debit > MAX_TOTAL_CENTS:
            raise ValueError("manual journal total exceeds the cents limit")
        return self


@dataclass(frozen=True)
class _ResolvedAccount:
    account_id: str
    account_code: str
    canonical_form: str
    linked_external_account_id: str | None


def manual_proposal_id(workspace_id: UUID | str, entity_id: UUID | str, idempotency_key: str) -> UUID:
    key = json.dumps(
        [str(workspace_id), str(entity_id), SUBJECT_TYPE, idempotency_key],
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, "accounting-ops:manual-journal-proposal:" + key)


def _digest_request(request: ManualJournalRequest) -> str:
    encoded = json.dumps(
        request.model_dump(mode="json"), separators=(",", ":"), sort_keys=True
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _pair_lines(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Walk debit and credit cursors in submitted order, preserving the rule."""
    debits = [line for line in lines if line["side"] == "debit"]
    credits = [line for line in lines if line["side"] == "credit"]
    debit_index = credit_index = 0
    debit_remaining = debits[0]["amount_cents"] if debits else 0
    credit_remaining = credits[0]["amount_cents"] if credits else 0
    pairs: list[dict[str, Any]] = []
    while debit_index < len(debits) and credit_index < len(credits):
        amount = min(debit_remaining, credit_remaining)
        pairs.append(
            {
                "debit_account": debits[debit_index]["canonical_form"],
                "credit_account": credits[credit_index]["canonical_form"],
                "amount_cents": amount,
            }
        )
        debit_remaining -= amount
        credit_remaining -= amount
        if debit_remaining == 0:
            debit_index += 1
            if debit_index < len(debits):
                debit_remaining = debits[debit_index]["amount_cents"]
        if credit_remaining == 0:
            credit_index += 1
            if credit_index < len(credits):
                credit_remaining = credits[credit_index]["amount_cents"]
    if debit_index != len(debits) or credit_index != len(credits):
        raise ValueError("manual journal lines do not balance")
    return pairs


async def _resolve_accounts(session, *, workspace_id: str, entity_id: str, request: ManualJournalRequest):
    resolved: dict[str, _ResolvedAccount] = {}
    for line in request.lines:
        if line.account_code in resolved:
            continue
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, account_number, canonical_form,
                           linked_external_account_id
                    FROM chart_of_account
                    WHERE workspace_id = :wid AND entity_id = :eid
                      AND account_number = :code AND is_active = true
                    LIMIT 2
                    """
                ),
                {"wid": workspace_id, "eid": entity_id, "code": line.account_code},
            )
        ).mappings().all()
        if len(rows) != 1:
            raise ValueError(
                f"Account code {line.account_code!r} must resolve to exactly one active entity account"
            )
        row = rows[0]
        if row["linked_external_account_id"] is not None:
            raise ValueError("manual journal proposals cannot use designated cash or linked bank accounts")
        resolved[line.account_code] = _ResolvedAccount(
            account_id=str(row["id"]),
            account_code=str(row["account_number"]),
            canonical_form=str(row["canonical_form"]),
            linked_external_account_id=row["linked_external_account_id"],
        )
    return resolved


async def _validate_current_accounts(session, *, workspace_id: str, entity_id: str, payload: dict) -> None:
    for line in payload["lines"]:
        account = line["resolved_account"]
        row = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, account_number, canonical_form,
                           linked_external_account_id
                    FROM chart_of_account
                    WHERE workspace_id = :wid AND entity_id = :eid
                      AND id = :id AND is_active = true
                    """
                ),
                {"wid": workspace_id, "eid": entity_id, "id": account["account_id"]},
            )
        ).mappings().first()
        if row is None or row["account_number"] != account["account_code"] or row["canonical_form"] != account["canonical_form"]:
            raise ValueError("manual journal proposal account changed or is inactive")
        if row["linked_external_account_id"] is not None:
            raise ValueError("manual journal proposal account became a cash or linked bank account")


async def enqueue_manual_proposal(
    session,
    *,
    workspace_id: UUID | str,
    entity_id: UUID | str,
    request: ManualJournalRequest,
    proposer_user_uc_uid: str,
    proposer_agent_id: str | None,
) -> tuple[str, bool, int, bool]:
    """Validate and enqueue without ever invoking a posting primitive.

    The fourth return value is true for an exact immutable replay.  The caller
    writes the decision audit on the same transaction.
    """
    request.checked()
    wid, eid = str(workspace_id), str(entity_id)
    approval_id = manual_proposal_id(wid, eid, request.idempotency_key)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(approval_id)},
    )
    existing = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, status, payload
                FROM approval_queue
                WHERE id = :id AND workspace_id = :wid AND entity_id = :eid
                FOR UPDATE
                """
            ),
            {"id": str(approval_id), "wid": wid, "eid": eid},
        )
    ).mappings().first()
    digest = _digest_request(request)
    if existing:
        if (existing["payload"] or {}).get("request_digest") != digest:
            raise ValueError("Conflicting replay: manual journal proposal payload is immutable")
        total = int((existing["payload"] or {}).get("total_debit_cents", 0))
        return existing["id"], existing["status"] == "pending", total, True

    effective_date = dt.date.fromisoformat(request.effective_date)
    if await assert_period_open(session, entity_id=eid, effective_date=effective_date) is not None:
        raise ValueError("Period closed; manual journal proposal was not queued")
    accounts = await _resolve_accounts(session, workspace_id=wid, entity_id=eid, request=request)
    lines: list[dict[str, Any]] = []
    for line in request.lines:
        account = accounts[line.account_code]
        lines.append(
            {
                "account_code": line.account_code,
                "side": line.side,
                "amount_cents": line.amount_cents,
                "resolved_account": {
                    "account_id": account.account_id,
                    "account_code": account.account_code,
                    "canonical_form": account.canonical_form,
                },
                "canonical_form": account.canonical_form,
            }
        )
    pairs = _pair_lines(lines)
    total = sum(line["amount_cents"] for line in lines if line["side"] == "debit")
    payload = {
        "version": PAYLOAD_VERSION,
        "subject_type": SUBJECT_TYPE,
        "workspace_id": wid,
        "entity_id": eid,
        "idempotency_key": request.idempotency_key,
        "request_digest": digest,
        "effective_date": request.effective_date,
        "memo": request.memo,
        "lines": lines,
        "pairs": pairs,
        "evidence_links": list(request.evidence_links),
        "proposer_user_uc_uid": proposer_user_uc_uid,
        "proposer_agent_id": proposer_agent_id,
        "reasoning_trace": request.reasoning_trace,
        "model_version": request.model_version,
        "total_debit_cents": total,
    }
    await session.execute(
        text(
            """
            INSERT INTO approval_queue (
                id, workspace_id, entity_id, subject_type, subject_id,
                amount, currency_code, usd_equivalent_at_event_time, status,
                reasoning, requested_by_identity, payload
            ) VALUES (
                :id, :wid, :eid, :subject_type, :id, :amount, 'USD', :amount,
                'pending', :reasoning, :requested_by, CAST(:payload AS jsonb)
            )
            """
        ),
        {
            "id": str(approval_id),
            "wid": wid,
            "eid": eid,
            "subject_type": SUBJECT_TYPE,
            "amount": Decimal(total) / Decimal(100),
            "reasoning": request.reasoning_trace,
            "requested_by": proposer_agent_id or proposer_user_uc_uid,
            "payload": json.dumps(payload),
        },
    )
    return str(approval_id), True, total, False


def _payload_request(payload: dict) -> ManualJournalRequest:
    request = ManualJournalRequest(
        idempotency_key=payload["idempotency_key"],
        effective_date=payload["effective_date"],
        memo=payload["memo"],
        lines=[
            {
                "account_code": line["account_code"],
                "side": line["side"],
                "amount_cents": line["amount_cents"],
            }
            for line in payload["lines"]
        ],
        evidence_links=payload["evidence_links"],
        reasoning_trace=payload["reasoning_trace"],
        model_version=payload["model_version"],
    )
    return request.checked()


async def apply_manual_proposal(
    session,
    *,
    workspace_id: UUID | str,
    entity_id: UUID | str,
    approval_id: UUID | str,
    payload: dict,
    approving_human: str,
) -> dict[str, Any]:
    """Apply the exact stored pairs; caller owns the surrounding transaction."""
    required = {
        "version", "subject_type", "workspace_id", "entity_id", "idempotency_key",
        "request_digest", "effective_date", "memo", "lines", "pairs",
        "evidence_links", "proposer_user_uc_uid", "proposer_agent_id",
        "reasoning_trace", "model_version", "total_debit_cents",
    }
    if set(payload) != required or payload["version"] != PAYLOAD_VERSION or payload["subject_type"] != SUBJECT_TYPE:
        raise ValueError("Malformed manual journal proposal envelope")
    wid, eid = str(workspace_id), str(entity_id)
    if payload["workspace_id"] != wid or payload["entity_id"] != eid:
        raise ValueError("Manual journal proposal scope mismatch")
    request = _payload_request(payload)
    if _digest_request(request) != payload["request_digest"]:
        raise ValueError("Manual journal proposal digest mismatch")
    if not approving_human:
        raise ValueError("Manual journal proposal requires an approving human")
    await _validate_current_accounts(session, workspace_id=wid, entity_id=eid, payload=payload)
    if await assert_period_open(session, entity_id=eid, effective_date=dt.date.fromisoformat(request.effective_date)) is not None:
        raise ValueError("Period closed; manual journal proposal remains pending")

    expected_pairs = _pair_lines(payload["lines"])
    if payload["pairs"] != expected_pairs:
        raise ValueError("Manual journal proposal pairing changed")
    journal_entry_ids: list[str] = []
    for index, pair in enumerate(payload["pairs"], start=1):
        result = await post_manual_journal_pair(
            session,
            workspace_id=wid,
            entity_id=eid,
            source_external_id=f"{approval_id}:{index}",
            effective_date=dt.date.fromisoformat(request.effective_date),
            amount_cents=pair["amount_cents"],
            debit_account=pair["debit_account"],
            credit_account=pair["credit_account"],
            memo=request.memo,
            reasoning_trace=request.reasoning_trace,
            model_version=request.model_version,
            approving_human_uc_uid=approving_human,
        )
        if isinstance(result, PostingRefusal):
            raise ValueError("Manual journal proposal refused: " + result.reason)
        journal_entry_ids.append(str(result.journal_entry_id))
    return {
        "journal_entry_ids": journal_entry_ids,
        "journal_entry_id": journal_entry_ids[0] if len(journal_entry_ids) == 1 else None,
        "total_debit_cents": payload["total_debit_cents"],
        "source": "manual-agent-approved",
    }


__all__ = [
    "ManualJournalLine", "ManualJournalRequest", "SUBJECT_TYPE",
    "apply_manual_proposal", "enqueue_manual_proposal", "manual_proposal_id",
]
