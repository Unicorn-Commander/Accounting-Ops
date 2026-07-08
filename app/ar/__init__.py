"""AR / Invoicing — native Accounts-Receivable invoicing for Accounting-Ops.

Invoicing is NATIVE to AO and fully usable STANDALONE (an invoice needs no PO at
all). The Project-Ops cross-app path is just one entitlement-gated caller of the
same native capability (see ``app.ar.entitlement``).

All double-entry is posted through the EXISTING journal primitives
(``app.journal.posting._insert_journal_entry`` + the per-entity SHA-256 hash
chain + ``_log_post_attempt``) — the same path the Stripe/eBay connectors use for
explicit-account postings. We NEVER write the ledger directly and NEVER touch
``posting.py``; void/credit-note appends a NEW reversing entry.

Public surface:
  - INVOICE_SOURCE — the journal_entry.source label for AR postings.
  - upsert_ar_customer — find-or-create a Counterparty (is_customer flavor).
  - create_invoice_from_draft — idempotent on (external_source, external_ref).
  - get_invoice / void_invoice / record_payment.
"""

from app.ar.invoicing import (
    INVOICE_SOURCE,
    InvoiceError,
    create_invoice_from_draft,
    get_invoice,
    record_payment,
    upsert_ar_customer,
    void_invoice,
)

__all__ = [
    "INVOICE_SOURCE",
    "InvoiceError",
    "create_invoice_from_draft",
    "get_invoice",
    "record_payment",
    "upsert_ar_customer",
    "void_invoice",
]
