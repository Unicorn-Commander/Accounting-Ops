"""E-signature capability for finished work products.

A genuinely-usable capability (the taxpayer signs their OWN document — no UPL or
IRS-transmission concern), gated only behind a per-workspace feature flag. See
``app/esign/provider.py`` for the provider abstraction (Docuseal + Manual) and
``app/esign/service.py`` for the SignatureRequest lifecycle.
"""
from app.esign.provider import (
    SIGNATURE_STATUSES,
    TERMINAL_STATUSES,
    DocusealProvider,
    EsignProvider,
    ManualProvider,
    ProviderError,
    ProviderUnavailable,
    coerce_status,
    resolve_provider,
)
from app.esign.service import cancel, record_signed, request_signature, sync_status

__all__ = (
    "EsignProvider",
    "DocusealProvider",
    "ManualProvider",
    "ProviderError",
    "ProviderUnavailable",
    "resolve_provider",
    "coerce_status",
    "SIGNATURE_STATUSES",
    "TERMINAL_STATUSES",
    "request_signature",
    "sync_status",
    "record_signed",
    "cancel",
)
