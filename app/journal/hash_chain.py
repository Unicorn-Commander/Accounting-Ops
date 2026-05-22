"""Per-entity append-only hash chain for journal entries.

  prev_hash = previous entry's this_hash (or GENESIS_HASH for the first)
  this_hash = sha256(prev_hash || canonical_payload(entry))

A canonical payload is the entry's effective_date, amount_cents, debit
+ credit accounts, source + source_external_id, payee, description, and
posted_at — joined by '\\x1f' (Unit Separator) so injection is impossible.

verify_chain walks the chain for one entity and returns the list of
entries whose computed this_hash doesn't match the stored one. Tampering
shows as a single broken link; the chain re-converges if a later block's
prev_hash still points at a valid this_hash earlier in the chain (but the
unique constraint on (entity_id, this_hash) prevents forks).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

GENESIS_HASH: bytes = b"\x00" * 32
_SEP: bytes = b"\x1f"  # ASCII Unit Separator


def _bytes(value: object) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat().encode("utf-8")
    return str(value).encode("utf-8")


def compute_hash(
    prev_hash: bytes,
    *,
    effective_date: date,
    amount_cents: int,
    debit_account: str,
    credit_account: str,
    source: str,
    source_external_id: str | None,
    payee: str | None,
    description: str,
    posted_at: datetime,
) -> bytes:
    """Compute this_hash for a journal entry given its predecessor + payload.

    Order of fields is part of the contract — never change it without bumping
    a chain-version and re-verifying historical entries. Adding fields
    requires a versioned hash function.
    """
    if len(prev_hash) != 32:
        raise ValueError(f"prev_hash must be 32 bytes, got {len(prev_hash)}")
    h = hashlib.sha256()
    h.update(prev_hash)
    for field in (
        effective_date,
        amount_cents,
        debit_account,
        credit_account,
        source,
        source_external_id,
        payee,
        description,
        posted_at,
    ):
        h.update(_SEP)
        h.update(_bytes(field))
    return h.digest()


@dataclass(frozen=True)
class TamperedEntry:
    entry_id: str
    stored_hash: bytes
    computed_hash: bytes
    position: int  # index in the chain (0 = oldest)


async def get_last_chain_hash(session: AsyncSession, entity_id: str) -> bytes:
    """Return the this_hash of the most recent entry for this entity.

    If no entries exist yet, returns GENESIS_HASH.
    """
    row = (
        await session.execute(
            text(
                """
                SELECT this_hash
                FROM journal_entry
                WHERE entity_id = :eid
                  AND this_hash IS NOT NULL
                ORDER BY posted_at DESC, id DESC
                LIMIT 1
                """
            ),
            {"eid": entity_id},
        )
    ).first()
    if row is None:
        return GENESIS_HASH
    return bytes(row.this_hash)


async def verify_chain(
    session: AsyncSession, entity_id: str
) -> list[TamperedEntry]:
    """Walk the chain for one entity and return tampered/broken entries.

    Replays the chain from oldest to newest, comparing computed this_hash
    against stored this_hash and verifying each prev_hash matches the
    previous entry's this_hash. Tampered entries break this comparison.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, effective_date, amount,
                       debit_account, credit_account, source,
                       source_external_id, payee, description,
                       posted_at, prev_hash, this_hash
                FROM journal_entry
                WHERE entity_id = :eid
                  AND this_hash IS NOT NULL
                ORDER BY posted_at ASC, id ASC
                """
            ),
            {"eid": entity_id},
        )
    ).mappings().all()

    from decimal import Decimal

    tampered: list[TamperedEntry] = []
    expected_prev: bytes = GENESIS_HASH
    for idx, r in enumerate(rows):
        stored_prev = bytes(r["prev_hash"]) if r["prev_hash"] else GENESIS_HASH
        stored_this = bytes(r["this_hash"])
        if stored_prev != expected_prev:
            tampered.append(
                TamperedEntry(
                    entry_id=r["id"],
                    stored_hash=stored_this,
                    computed_hash=expected_prev,  # what prev_hash *should* be
                    position=idx,
                )
            )
            # Don't try to chain through a broken entry — reset expectation
            # to the stored prev_hash so the rest of the chain can still be
            # checked.
            expected_prev = stored_prev

        # journal_entry.amount is NUMERIC(20,4) absolute value; the hash
        # was computed against amount_cents = abs(amount_decimal * 100).
        amount_cents = int((Decimal(str(r["amount"])) * 100).to_integral_value())
        computed_this = compute_hash(
            stored_prev,
            effective_date=r["effective_date"],
            amount_cents=amount_cents,
            debit_account=r["debit_account"],
            credit_account=r["credit_account"],
            source=r["source"],
            source_external_id=r["source_external_id"],
            payee=r["payee"],
            description=r["description"] or "",
            posted_at=r["posted_at"],
        )
        if computed_this != stored_this:
            tampered.append(
                TamperedEntry(
                    entry_id=r["id"],
                    stored_hash=stored_this,
                    computed_hash=computed_this,
                    position=idx,
                )
            )
        expected_prev = stored_this

    return tampered


def chain_summary(entries: Iterable[TamperedEntry]) -> dict:
    """Format verify_chain output for the audit-log API surface."""
    items = list(entries)
    return {
        "ok": len(items) == 0,
        "tampered_count": len(items),
        "tampered": [
            {
                "entry_id": t.entry_id,
                "position": t.position,
                "stored_hash_hex": t.stored_hash.hex(),
                "computed_hash_hex": t.computed_hash.hex(),
            }
            for t in items
        ],
    }
