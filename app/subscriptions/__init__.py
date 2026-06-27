"""Subscription detection — the model-agnostic "brain".

A READ-ONLY analysis surface: it inspects the posted journal and detects
recurring subscription charges so the accountant agent can flag what to cancel.
It writes nothing and cancels nothing (cancellation is a separate browser agent).

The actual detection lives in :mod:`app.subscriptions.detect` as a pure,
DB-free function (``detect_subscriptions``) so it is fully unit-testable; the
MCP tool wrapper in ``app.tools.subscriptions`` is the thin DB layer that pulls
the journal window and calls the pure detector.
"""
from app.subscriptions.detect import detect_subscriptions

__all__ = ["detect_subscriptions"]
