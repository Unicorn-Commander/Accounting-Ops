"""Data-rights surface: export, erasure, and retention.

This package implements the GDPR/CCPA-shaped capabilities that the commercial
legal documents in ``docs/legal/`` have to describe truthfully:

  * ``retention``  — per-workspace retention configuration + the documented
                     default that applies when a workspace has not set one.
  * ``export``     — a full, tenant-scoped, owner-gated export of everything a
                     workspace holds, including the stored files.
  * ``erasure``    — workspace-level erasure, designed around the fact that the
                     ledger is append-only and hash-chained.

Design rationale — including WHY erasure is workspace-level rather than
row-level, which is the single most legally significant decision here — is in
``docs/DATA-RIGHTS.md`` and in the module docstring of ``erasure.py``. Read one
of those before changing anything in here: the privacy policy and the DPA both
describe this behaviour to customers, and a change to the code is a change to a
representation we have made.
"""
from __future__ import annotations

__all__ = ("erasure", "export", "retention")
