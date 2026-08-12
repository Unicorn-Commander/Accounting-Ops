"""Unit tests for app.api.documents pure helpers (no DB, no network).

Covers the validation + payload-shaping logic that gates what gets stored and
what reaches the approval queue:

  1. _looks_like — magic-byte sniffing accepts real headers, rejects mismatches.
  2. _coerce_amount — extraction → (Decimal, currency); bad/None → (None, None).
  3. _build_approval — payload shape + reasoning copy per extraction tier and
     for the no-extraction (PDF/CSV) path; never errors.
  4. _object_key / _bucket_for_workspace — content-addressed, tenant-scoped.
  5. Router is registered with the expected entity-scoped paths.
  6. The decide endpoint does NOT auto-apply a 'document_review' subject (safety:
     it must be handled via review, never silently posted).
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from app.api.documents import (
    _bucket_for_workspace,
    _build_approval,
    _coerce_amount,
    _looks_like,
    _object_key,
    _summary_message,
)
from app.categorization.receipt_vlm import ReceiptExtraction
from tests.route_introspection import registered_paths


def _extraction(**kw) -> ReceiptExtraction:
    base = dict(
        vendor="Synth Co",
        amount="12.50",
        date="2026-01-01",
        currency="USD",
        confidence=0.9,
        tier="high",
        model="fake",
        endpoint="http://x/v1",
        duration_ms=10,
        raw_response="{}",
        error=None,
    )
    base.update(kw)
    return ReceiptExtraction(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
def test_looks_like_accepts_real_headers():
    assert _looks_like("application/pdf", b"%PDF-1.7\n...") is True
    assert _looks_like("image/png", b"\x89PNG\r\n\x1a\n....") is True
    assert _looks_like("image/jpeg", b"\xff\xd8\xff\xe0....") is True
    assert _looks_like("image/gif", b"GIF89a....") is True
    assert _looks_like("image/webp", b"RIFF\x00\x00\x00\x00WEBP") is True
    assert _looks_like("image/tiff", b"II*\x00....") is True
    # CSV is not sniffed — accepted on declared type.
    assert _looks_like("text/csv", b"date,amount\n2026-01-01,5.00") is True


def test_looks_like_rejects_mismatch():
    # A PDF-declared file whose bytes are actually a PNG → rejected.
    assert _looks_like("application/pdf", b"\x89PNG\r\n\x1a\n") is False
    # An EXE/script masquerading as a JPEG → rejected.
    assert _looks_like("image/jpeg", b"MZ\x90\x00") is False
    assert _looks_like("image/png", b"not a png") is False


def test_coerce_amount():
    amt, cur = _coerce_amount(_extraction(amount="42.99", currency="USD"))
    assert amt == Decimal("42.99")
    assert cur == "USD"

    # Missing amount → both None (row + approval still created; human fills in).
    amt, cur = _coerce_amount(_extraction(amount=None))
    assert amt is None and cur is None

    # No extraction at all (PDF/CSV path) → None.
    amt, cur = _coerce_amount(None)
    assert amt is None and cur is None

    # A 3-letter currency is uppercased (shape-valid; the DB only constrains
    # upper-case, and a human reviews the proposed row).
    amt, cur = _coerce_amount(_extraction(amount="5.00", currency="eur"))
    assert amt == Decimal("5.00") and cur == "EUR"

    # A malformed (non-3-alpha) currency falls back to USD when amount present.
    amt, cur = _coerce_amount(_extraction(amount="5.00", currency="dollars"))
    assert amt == Decimal("5.00") and cur == "USD"


def test_build_approval_high_tier():
    rid = uuid4()
    payload, reasoning = _build_approval(
        receipt_id=rid,
        content_hash="a" * 64,
        object_key="w/e/uploads/x.png",
        bucket="accounting-ops",
        mime="image/png",
        ext="png",
        extraction=_extraction(tier="high"),
        filename="receipt.png",
    )
    assert payload["kind"] == "document_review"
    assert payload["document_kind"] == "receipt"
    assert payload["receipt_id"] == str(rid)
    assert payload["extraction"]["vendor"] == "Synth Co"
    assert "high confidence" in reasoning
    # Crucially: never claims a posted/applied state.
    assert "ledger entry" in reasoning


def test_build_approval_none_tier_and_pdf():
    # tier='none' (model read nothing).
    _, reasoning = _build_approval(
        receipt_id=uuid4(), content_hash="b" * 64, object_key="k", bucket="acc",
        mime="image/png", ext="png",
        extraction=_extraction(vendor=None, amount=None, date=None, tier="none",
                               confidence=0.0, error="empty completion"),
        filename="blurry.png",
    )
    assert "nothing could be read" in reasoning

    # PDF path — no extraction attempted.
    payload, reasoning = _build_approval(
        receipt_id=uuid4(), content_hash="c" * 64, object_key="k", bucket="acc",
        mime="application/pdf", ext="pdf", extraction=None, filename="invoice.pdf",
    )
    assert payload["document_kind"] == "pdf_document"
    assert payload["extraction_attempted"] is False
    assert "PDF extraction is not automated" in reasoning

    # CSV path.
    payload, _ = _build_approval(
        receipt_id=uuid4(), content_hash="d" * 64, object_key="k", bucket="acc",
        mime="text/csv", ext="csv", extraction=None, filename="export.csv",
    )
    assert payload["document_kind"] == "statement_or_export"


def test_object_key_is_tenant_scoped_and_content_addressed():
    wid = uuid4()
    eid = uuid4()
    key = _object_key(wid, eid, "f" * 64, "pdf")
    assert key == f"{wid}/{eid}/uploads/{'f' * 64}.pdf"
    # The bucket is the deployment prefix.
    assert _bucket_for_workspace(wid)  # non-empty


def test_summary_message_per_tier():
    assert "confirmation" in _summary_message(_extraction(tier="high"), "png")
    assert "partially" in _summary_message(_extraction(tier="low"), "png")
    assert "couldn't read" in _summary_message(_extraction(tier="none"), "png")
    assert "queued" in _summary_message(None, "pdf").lower()


def test_router_registered_with_entity_scoped_paths():
    import app.main as m

    paths = registered_paths(m.app)
    assert (
        "/api/workspaces/{workspace_id}/entities/{entity_id}/documents/upload"
        in paths
    )
    assert (
        "/api/workspaces/{workspace_id}/entities/{entity_id}/documents" in paths
    )


def test_document_review_is_not_auto_applied_by_decide():
    """Safety: the decide endpoint must NOT have an apply-path for
    'document_review'. Those are human-triage items handled via the review UI;
    a one-click ledger post would bypass categorization + the materiality gate.
    The decide endpoint returns 422 for unknown subject_types (verified by
    inspecting the handled set in source)."""
    import inspect

    from app.api import approvals

    src = inspect.getsource(approvals.decide_approval)
    # The three explicitly-applied subject types — document_review is NOT here.
    assert "journal_recategorization" in src
    assert "compliance_rule" in src
    assert "bank_transaction_import" in src
    assert "document_review" not in src  # never auto-applied
