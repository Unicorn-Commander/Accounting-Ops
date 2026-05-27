"""Append-only journal: posting, hash chain, materiality gate.

The journal lives in the per-workspace schema. Every posted transaction
becomes a `journal_entry` row with a sha256 hash chain per-entity. Reversals
are NEW entries that point at the original; we never UPDATE or DELETE.

Public surface:
  - canonical_form(account_number, account_name) → "1010·Cash·BofA·Magic Unicorn"
  - compute_hash(prev_hash, entry_payload) → bytes(32)
  - GENESIS_HASH (32 zero bytes)
  - resolve_accounts_for_txn(...) — picks debit_account + credit_account
  - materiality_tier(amount_cents) → "L" | "M" | "H"
  - post_journal_entry(session, ...) — atomic post with chain advance
  - verify_chain(session, entity_id) — walk the chain, return list of tampered ids
"""

from app.journal.hash_chain import GENESIS_HASH, compute_hash, verify_chain
from app.journal.materiality import materiality_tier, MaterialityVerdict
from app.journal.posting import (
    CorrectionOutcome,
    CorrectionQueued,
    PostingOutcome,
    PostingRefusal,
    post_correction,
    post_journal_entry,
    resolve_accounts_for_txn,
)
from app.journal.accounts import canonical_form

__all__ = [
    "GENESIS_HASH",
    "CorrectionOutcome",
    "CorrectionQueued",
    "MaterialityVerdict",
    "PostingOutcome",
    "PostingRefusal",
    "canonical_form",
    "compute_hash",
    "materiality_tier",
    "post_correction",
    "post_journal_entry",
    "resolve_accounts_for_txn",
    "verify_chain",
]
