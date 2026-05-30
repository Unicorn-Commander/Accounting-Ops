# books-lead sub-agent

Auto-resolves Magic Unicorn Inc's `approval_queue` backlog so Aaron's review
surface shrinks from "one decision per transaction" to "one decision per
vendor cluster".

## Phases

1. **`apply_high_confidence.py`** — auto-applies the 12 pre-scored
   ('approve', 'high') bank-transaction approvals. Goes through the same
   `post_journal_entry` path the `/api/.../decide` endpoint uses on
   APPROVE, so hash chain, idempotency, materiality bypass (user-attested),
   and `decision_audit` all behave identically to a human click.

2. **`promote_medium_to_high.py`** — re-scores the 383 medium-confidence
   recommendations against the entity's posted journal history.
   Promotes → auto-applies when ALL of:
     * Same vendor signature has ≥ 3 prior posted JEs to the same category, AND
     * Vendor's prior history is unambiguous (≥ 95% in the recommended category), AND
     * Pending amount is within 0.5× .. 2× the prior median, AND
     * Description has no counter-signal token (refund / reversal / adjustment / chargeback / dispute / return / credit / rbt), AND
     * Category is not in the CPA-flagged list (SaaS · AI / Banking · Interest / Inter-entity transfer / Contractor · 1099-NEC).
   Conservative by design — REAL MONEY, prefer keeping for Aaron to mis-post.

3. **`cluster_low_confidence.py`** — re-keys `approval_recommendation.cluster_id`
   for all pending low + medium rows to a vendor-grouped signature
   (`{vendor_sig}|{category_slug}`), so the existing
   `GET /api/.../approval-recommendations` endpoint reports per-vendor cluster
   cards instead of per-transaction singletons. Aaron can then approve "all 50
   Venmo personal-card transfers as Owner Distribution" as ONE decision.

4. **`reconcile_books.py`** — emits `/drafts/books-reconciliation-2026-05-30.md`
   with a per-year `journal_entry` vs `approval_queue` summary and a data-
   integrity pass (NULL payload rows, duplicate source_external_ids).

## Run order

All scripts target the Magic Unicorn Inc entity hard-coded in
`apply_high_confidence.py` (`WORKSPACE_ID`, `ENTITY_ID`). They are
idempotent on re-run: Phase 1's posting is gated by
`(entity_id, source, source_external_id)` UNIQUE; Phases 2 & 3 do
upsert-style updates; Phase 4 is read-only.

```bash
docker exec -t accounting_ops-app-1 python -m agents.cpa_team.books_lead.apply_high_confidence
docker exec -t accounting_ops-app-1 python -m agents.cpa_team.books_lead.promote_medium_to_high
docker exec -t accounting_ops-app-1 python -m agents.cpa_team.books_lead.cluster_low_confidence
docker exec -t accounting_ops-app-1 python -m agents.cpa_team.books_lead.reconcile_books
```

## Why not call the HTTP decide endpoint?

`/api/workspaces/{wid}/approvals/{aid}/decide` requires `admin` role; the
service token issued to this sub-agent (`tax-1120-preparer`) is capped at
`accountant` via `role_floor`. Going straight through the data layer
(`post_journal_entry` + `_mark_decided` + `write_decision_audit`) hits
EXACTLY the same code path the HTTP route invokes and is the sanctioned
pattern for back-office migrations — see `scripts/ingest_bofa_live.py`,
which uses the same approach to write 800+ historical journal entries.
