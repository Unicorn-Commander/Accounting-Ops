"""Contact (counterparty) import + management.

AO's "contacts" are *counterparties* — the customers you bill and the vendors
you pay. This package owns:

  * ``import_parse`` — pure parsers that turn an uploaded vCard (.vcf) or CSV
    (Google / Outlook / macOS Contacts / a spreadsheet) into normalized preview
    rows. NO database writes — the UI previews + lets the user tag
    customer/vendor/skip before committing.
  * ``service``      — list / search / upsert / bulk-import counterparties in a
    workspace's tenant schema. Generalizes the AR ``upsert_ar_customer`` path
    to any ``kind`` and carries a thin ``contact_id`` hook for later
    Contact-Ops reconciliation (the canonical person directory).
"""
from app.contacts.import_parse import ParsedContact, parse_contacts
from app.contacts.service import (
    bulk_import_counterparties,
    list_counterparties,
    upsert_counterparty,
)

__all__ = (
    "ParsedContact",
    "parse_contacts",
    "bulk_import_counterparties",
    "list_counterparties",
    "upsert_counterparty",
)
