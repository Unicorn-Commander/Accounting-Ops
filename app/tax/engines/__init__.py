"""Vendored deterministic tax engines (synced from Tax-Planning-Ops).

These are PURE functions (Decimal math, pydantic models — no I/O) copied from the
TPO ``backend/app/engines`` so Accounting-Ops can compute returns on behalf of
clients WITHOUT a cross-service call (sovereign, always-available, deterministic).

DO NOT hand-edit the engine math here — it is the authoritative TPO computation.
Re-sync with scripts/sync_tax_engines.sh if TPO's engines change. The year
constants live in ``configs_1040`` (extracted from TPO's cited bracket seed).
"""
