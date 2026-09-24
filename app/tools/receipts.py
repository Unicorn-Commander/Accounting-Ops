"""MCP write tool: link a stored receipt to a posted journal entry.

Creates a ``recon_link`` row — the per-entity "receipt ↔ journal_entry" link
table (docs/ARCHITECTURE.md). The runtime role may INSERT into recon_link; the
journal entry itself is never mutated (append-only). The receipt and the
transaction must belong to the SAME entity (the tool is entity-scoped, so a
receipt or entry in another entity is simply not found -> 422-style error).

Authorization mirrors the other write tools: ``authorize_workspace`` binds the
caller's token to the workspace and enforces role (accountant) + entity_scope;
the work runs inside ``entity_session`` so the workspace search_path + RLS GUC
are set. A decision_audit row is appended.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.tools._audit import project_decision_audit, write_decision_audit
from app.workspace_session import entity_session


async def link_receipt_to_transaction(
    workspace_id: UUID,
    entity_id: UUID,
    receipt_id: UUID,
    tx_id: UUID,
) -> dict:
    """Link a content-hash receipt to an entity-scoped transaction.

    Closes the IRS purchase/payment/purpose triangle. The receipt and
    transaction MUST belong to the same entity_id, or this raises 422.

    Appends a recon_link row (receipt_id + journal_entry_id), reusing the entry's
    amount/currency so the link is self-describing. Idempotent: a second link of
    the same (receipt, transaction) returns the existing link rather than
    duplicating. Returns the recon_link id and the linked ids.
    """
    ctx = await authorize_workspace(workspace_id, entity_id, min_role="bookkeeper")

    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, eid):
        # Both sides must live in THIS entity. A missing side (wrong entity or
        # nonexistent id) is a 422 — the caller linked across entities.
        entry = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, amount, currency_code,
                           usd_equivalent_at_event_time, status
                    FROM journal_entry
                    WHERE id = :id AND entity_id = :eid
                    """
                ),
                {"id": str(tx_id), "eid": str(eid)},
            )
        ).mappings().first()
        if entry is None:
            raise ValueError(
                f"transaction {tx_id} not found in entity {entity_id} "
                "(it may belong to a different entity)."
            )
        if entry["status"] != "posted":
            raise ValueError(
                f"transaction {tx_id} is '{entry['status']}', not 'posted'; "
                "only posted entries can be linked to a receipt."
            )
        receipt = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, content_hash, currency_code
                    FROM receipt
                    WHERE id = :id AND entity_id = :eid
                    """
                ),
                {"id": str(receipt_id), "eid": str(eid)},
            )
        ).mappings().first()
        if receipt is None:
            raise ValueError(
                f"receipt {receipt_id} not found in entity {entity_id} "
                "(it may belong to a different entity)."
            )

        # Idempotency: one link per (journal_entry, receipt).
        existing = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id FROM recon_link
                    WHERE journal_entry_id = :je AND receipt_id = :rid
                    LIMIT 1
                    """
                ),
                {"je": str(tx_id), "rid": str(receipt_id)},
            )
        ).mappings().first()
        if existing is not None:
            link_id = UUID(existing["id"])
            created = False
        else:
            link_id = uuid4()
            created = True
            usd = entry["usd_equivalent_at_event_time"]
            await session.execute(
                text(
                    """
                    INSERT INTO recon_link (
                        id, workspace_id, entity_id, journal_entry_id, receipt_id,
                        amount, currency_code, usd_equivalent_at_event_time
                    ) VALUES (
                        :id, :wid, :eid, :je, :rid,
                        :amount, :currency, :usd
                    )
                    """
                ),
                {
                    "id": str(link_id),
                    "wid": str(workspace_id),
                    "eid": str(eid),
                    "je": str(tx_id),
                    "rid": str(receipt_id),
                    "amount": Decimal(str(entry["amount"])),
                    "currency": entry["currency_code"],
                    "usd": Decimal(str(usd)) if usd is not None else Decimal(str(entry["amount"])),
                },
            )

        payload = {
            "ok": True,
            "recon_link_id": str(link_id),
            "receipt_id": str(receipt_id),
            "tx_id": str(tx_id),
            "content_hash": receipt["content_hash"],
            "created": created,
        }
        audit_id = await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=eid,
            ctx=ctx,
            tool_name="link_receipt",
            request_payload={"receipt_id": str(receipt_id), "tx_id": str(tx_id)},
            response_payload=payload,
            reasoning=f"Linked receipt {receipt_id} to transaction {tx_id}.",
            outcome="linked" if created else "already_linked",
            source_document_id=receipt_id,
        )
        content_hash = receipt["content_hash"]

    # ---- FalkorDB projection (best-effort, AFTER the entity_session commit) ----
    # Mirror the authoritative writes: the agent Decision, and the
    # (JournalEntry)-[:SUPPORTED_BY]->(SourceDocument) edge that closes the IRS
    # purchase/payment/purpose triangle (the receipt's content_hash is the
    # SourceDocument identity). Best-effort: failures are swallowed in project().
    project_decision_audit(
        workspace_id=workspace_id,
        entity_id=eid,
        audit_id=audit_id,
        ctx=ctx,
        tool_name="link_receipt",
        outcome="linked" if created else "already_linked",
        reasoning=f"Linked receipt {receipt_id} to transaction {tx_id}.",
        subject_id=tx_id,
    )
    if content_hash:
        from app import falkordb

        falkordb.project(
            falkordb.link_journal_entry_to_source(
                workspace_id, eid, str(tx_id), content_hash
            ),
            label=f"je_supported_by:{tx_id}",
        )

    return payload
