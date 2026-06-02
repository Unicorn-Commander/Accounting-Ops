"""Document-upload — the PRIMARY "drag in everything, we figure it out" path.

  POST /api/workspaces/{wid}/entities/{eid}/documents/upload   (multipart)
  GET  /api/workspaces/{wid}/entities/{eid}/documents          (list)

What a single upload does, in order, inside ONE workspace transaction:

  1. Validate size (<= document_upload_max_bytes) + mime type (allow-list).
  2. Content-hash the bytes (SHA-256). The ``receipt.content_hash`` column is
     globally UNIQUE (migrations tenant/0001), so the hash is the dedup key:
     re-uploading the same file returns the existing row (idempotent), never a
     duplicate and never a second approval item.
  3. Store the bytes in this deployment's Garage S3 bucket under a key that
     embeds the workspace id + content hash (path-style, no PII in the key).
  4. INSERT a ``receipt`` row (workspace + entity scoped).
  5. For RECEIPTS that are raster images, call the LOCAL Qwen VLM (free) to
     extract {vendor, amount, date}. PDFs/CSVs/statements are NOT parsed here
     (Phase-1 scope) — they are stored + queued "for review" with no model call.
  6. Create an ``approval_queue`` row (subject_type='document_review') so the
     proposed item reaches a human. **NEVER auto-posted to the ledger.** Low
     confidence / empty / error → still queued, framed "needs human".

Security model (see the module-level NEEDS-HUMAN-REVIEW notes + the SUMMARY):
  * AuthN: validate_bearer_token (via the router dependency + resolve_workspace).
  * AuthZ role floor: ``client_user`` may upload (the role explicitly granted
    "upload docs" in app/tenancy). Listing requires ``viewer``.
  * Tenant isolation: workspace == Postgres schema (workspace_session) + the
    app.current_workspace RLS GUC. Entity isolation: assert_entity_in_scope +
    an explicit ``entity_id`` row predicate on every write/read.
  * The Garage write happens BEFORE the DB row commits; if the commit later
    fails, the object is orphaned (harmless — it's content-addressed and will be
    re-referenced on a future identical upload) but NO half-state reaches the
    ledger. We never mutate posting.py / the hash chain / closed-period guard.
  * Bytes are bounded in memory by the size cap; the VLM call is best-effort and
    cannot break the upload (its failures degrade to a "needs human" queue item).
"""
from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.authz import assert_entity_in_scope
from app.clients.garage_s3 import (
    GarageNotConfigured,
    GarageS3Client,
    GarageUploadError,
)
from app.config import get_settings
from app.db import SessionLocal
from app.mcp_auth import bind_http_caller
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.tools._audit import write_decision_audit
from app.workspace_session import workspace_session

router = APIRouter(
    tags=["documents"],
    dependencies=[Depends(bind_http_caller)],
)


# Allow-list of accepted mime types. Anything not here is rejected with 415 so
# the endpoint can never be coaxed into storing an arbitrary blob (e.g. a script
# masquerading as a doc). PDFs/images are the receipt path; CSV is for bank
# exports the user may dump (stored + queued, NOT parsed in Phase 1).
_ALLOWED_MIME: dict[str, str] = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/heic": "heic",
    "image/heif": "heif",
    "image/webp": "webp",
    "image/tiff": "tiff",
    "image/gif": "gif",
    "text/csv": "csv",
    "application/csv": "csv",
    "application/vnd.ms-excel": "csv",  # browsers often label .csv as this
}

# Magic-byte sniffers for the formats we can cheaply verify. A declared
# content-type is attacker-controlled (the browser/multipart sets it), so for
# the high-value formats we ALSO check the leading bytes and reject a mismatch.
# CSV/text is intentionally not sniffed (no reliable signature); it is stored as
# opaque bytes and never executed or parsed in Phase 1.
def _looks_like(declared_mime: str, head: bytes) -> bool:
    if declared_mime == "application/pdf":
        return head[:5] == b"%PDF-"
    if declared_mime == "image/png":
        return head[:8] == b"\x89PNG\r\n\x1a\n"
    if declared_mime in ("image/jpeg", "image/jpg"):
        return head[:3] == b"\xff\xd8\xff"
    if declared_mime == "image/gif":
        return head[:6] in (b"GIF87a", b"GIF89a")
    if declared_mime == "image/webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if declared_mime in ("image/heic", "image/heif"):
        # ISO-BMFF 'ftyp' box at offset 4; brand contains heic/heif/mif1.
        return head[4:8] == b"ftyp"
    if declared_mime == "image/tiff":
        return head[:4] in (b"II*\x00", b"MM\x00*")
    # Types we don't sniff (csv variants) — accept on declared type.
    return True


def _bucket_for_workspace(workspace_id: UUID) -> str:
    """Per-deployment Garage bucket name. One bucket holds all workspaces'
    objects; cross-workspace isolation is enforced by the ``receipt`` row's
    workspace_id + RLS, and the object KEY embeds the workspace id so keys never
    collide across tenants."""
    return get_settings().garage_bucket_prefix


def _object_key(workspace_id: UUID, entity_id: UUID, content_hash: str, ext: str) -> str:
    """Deterministic, content-addressed key. Embeds workspace + entity so a key
    is self-describing and never collides across tenants. The hash makes it
    idempotent (same bytes → same key)."""
    return f"{workspace_id}/{entity_id}/uploads/{content_hash}.{ext}"


@router.post(
    "/api/workspaces/{workspace_id}/entities/{entity_id}/documents/upload"
)
async def upload_document(
    workspace_id: UUID,
    entity_id: UUID,
    file: UploadFile = File(...),
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """Upload one document. Stores it, extracts (receipts only), queues for human.

    Role floor: ``client_user`` (the lowest role explicitly allowed to upload
    docs — see app/tenancy role table). Entity-scope enforced. NEVER posts to
    the ledger: every proposed item lands in approval_queue as 'pending'.

    Returns a small JSON summary the upload UI renders: the receipt id, whether
    it was a duplicate, the extraction tier, and the queued approval id (if any).
    """
    require_role(workspace, "client_user")
    # BOLA: a scoped member uploading to an entity outside their scope → 403.
    assert_entity_in_scope(workspace, entity_id)

    settings = get_settings()

    # --- Read the body with a hard size cap (stream-bounded). ---------------
    max_bytes = settings.document_upload_max_bytes
    body = await _read_capped(file, max_bytes)
    if body is None:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds the {max_bytes // (1024 * 1024)} MB upload limit.",
        )
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty file."
        )

    # --- Validate mime type (declared) + magic bytes (sniffed). -------------
    declared = (file.content_type or "").split(";")[0].strip().lower()
    ext = _ALLOWED_MIME.get(declared)
    if ext is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type '{declared or 'unknown'}'. "
                "Upload a PDF, image (JPG/PNG/HEIC/WEBP/TIFF/GIF), or CSV."
            ),
        )
    if not _looks_like(declared, body[:16]):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"File contents do not match the declared type '{declared}'. "
                "The file may be corrupt or mislabeled."
            ),
        )

    content_hash = hashlib.sha256(body).hexdigest()
    bucket = _bucket_for_workspace(workspace_id)
    object_key = _object_key(workspace_id, entity_id, content_hash, ext)

    # --- Dedup: if this exact content already exists, return the existing row.
    # The ``receipt.content_hash`` UNIQUE index is workspace-wide (one row per
    # distinct file across the workspace), so dedup is workspace-scoped by the
    # schema's own design — re-uploading identical bytes (even under a different
    # filename or a sibling entity) reuses the one canonical receipt. RLS on the
    # GUC keeps this lookup inside THIS workspace; cross-workspace is impossible.
    async with workspace_session(SessionLocal, workspace_id) as session:
        existing = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, entity_id::text AS entity_id
                    FROM receipt
                    WHERE content_hash = :h
                    LIMIT 1
                    """
                ),
                {"h": content_hash},
            )
        ).mappings().first()
        if existing is not None:
            # Idempotent re-upload. Do NOT create a second receipt or approval.
            return {
                "ok": True,
                "duplicate": True,
                "receipt_id": existing["id"],
                "content_hash": content_hash,
                "extraction_tier": None,
                "approval_id": None,
                "message": "This document was already uploaded; reusing it.",
            }

    # --- Store in Garage BEFORE the DB row. ---------------------------------
    # If this fails we never created a receipt row, so there is no dangling
    # reference. (The reverse — row without object — would be worse.)
    s3 = GarageS3Client(settings)
    try:
        await s3.put_object(
            bucket=bucket,
            object_key=object_key,
            body=body,
            content_type=declared,
        )
    except GarageNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Document storage is not configured on this deployment "
                "(GARAGE_S3_ACCESS_KEY / GARAGE_S3_SECRET_KEY). No file was stored."
            ),
        ) from exc
    except GarageUploadError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not store the document: {exc}",
        ) from exc

    # --- Best-effort VLM extraction (RECEIPT IMAGES ONLY, local + free). ----
    # Runs OUTSIDE the DB transaction so a slow/failed model never holds a
    # connection. Failures degrade to tier='none' (queued "needs human").
    extraction = await _maybe_extract(declared, body)

    # --- INSERT receipt + approval_queue inside ONE transaction. ------------
    receipt_id = uuid4()
    approval_id = uuid4()
    amount_dec, currency = _coerce_amount(extraction)

    async with workspace_session(SessionLocal, workspace_id) as session:
        # Guard against a race: another concurrent upload of the SAME bytes may
        # have inserted between our dedup check and here. The content_hash UNIQUE
        # index would raise; we wrap the INSERT in a SAVEPOINT (begin_nested) so a
        # unique-violation rolls back ONLY the savepoint — the outer transaction
        # stays usable for the follow-up SELECT. (A bare try/except can't do this:
        # in Postgres a failed statement poisons the whole transaction until a
        # rollback-to-savepoint.)
        try:
            async with session.begin_nested():
                await session.execute(
                    text(
                        """
                        INSERT INTO receipt (
                            id, workspace_id, entity_id, content_hash,
                            garage_bucket, object_key, mime_type,
                            amount, currency_code, extracted_at
                        ) VALUES (
                            :id, :wid, :eid, :h,
                            :bucket, :key, :mime,
                            :amount, :currency, :extracted_at
                        )
                        """
                    ),
                    {
                        "id": str(receipt_id),
                        "wid": str(workspace_id),
                        "eid": str(entity_id),
                        "h": content_hash,
                        "bucket": bucket,
                        "key": object_key,
                        "mime": declared,
                        "amount": amount_dec,
                        "currency": currency,
                        "extracted_at": dt.datetime.now(dt.timezone.utc)
                        if extraction is not None
                        else None,
                    },
                )
        except IntegrityError:
            # Concurrent identical upload won the race. The savepoint rolled back;
            # the outer transaction is intact. Fall through to read the row below.
            pass

        # Re-read by content_hash: whether WE inserted it or a racing request
        # did, this is the canonical row.
        row = (
            await session.execute(
                text(
                    "SELECT id::text AS id FROM receipt WHERE content_hash = :h LIMIT 1"
                ),
                {"h": content_hash},
            )
        ).mappings().first()
        if row is None:
            # Our insert genuinely failed for a non-race reason. Surface it.
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to record the uploaded document.",
            )
        canonical_receipt_id = UUID(row["id"])
        # If a race won, our generated receipt_id won't match; align to canonical.
        is_new = canonical_receipt_id == receipt_id
        receipt_id = canonical_receipt_id

        if not is_new:
            # A concurrent identical upload beat us; it owns the approval row.
            return {
                "ok": True,
                "duplicate": True,
                "receipt_id": str(receipt_id),
                "content_hash": content_hash,
                "extraction_tier": None,
                "approval_id": None,
                "message": "This document was already uploaded; reusing it.",
            }

        # --- Build the approval payload + reasoning. ------------------------
        payload, reasoning = _build_approval(
            receipt_id=receipt_id,
            content_hash=content_hash,
            object_key=object_key,
            bucket=bucket,
            mime=declared,
            ext=ext,
            extraction=extraction,
            filename=file.filename,
        )

        await session.execute(
            text(
                """
                INSERT INTO approval_queue (
                    id, workspace_id, entity_id, subject_type, subject_id,
                    amount, currency_code, status, reasoning,
                    requested_by_identity, payload
                ) VALUES (
                    :id, :wid, :eid, 'document_review', :subject_id,
                    :amount, :currency, 'pending', :reasoning,
                    :identity, CAST(:payload AS jsonb)
                )
                """
            ),
            {
                "id": str(approval_id),
                "wid": str(workspace_id),
                "eid": str(entity_id),
                "subject_id": str(receipt_id),
                "amount": amount_dec,
                "currency": currency,
                "reasoning": reasoning,
                "identity": workspace.user_uc_uid,
                "payload": _json(payload),
            },
        )

        # Audit the upload (append-only decision trail), same as other writes.
        await write_decision_audit(
            session,
            workspace_id=workspace_id,
            entity_id=entity_id,
            ctx=workspace,
            tool_name="document_upload",
            request_payload={
                "filename": file.filename,
                "mime_type": declared,
                "content_hash": content_hash,
                "size_bytes": len(body),
            },
            response_payload={
                "receipt_id": str(receipt_id),
                "approval_id": str(approval_id),
                "extraction_tier": extraction.tier if extraction else "skipped",
            },
            reasoning=reasoning,
            outcome="queued_for_review",
            source_document_id=receipt_id,
        )

    return {
        "ok": True,
        "duplicate": False,
        "receipt_id": str(receipt_id),
        "content_hash": content_hash,
        "extraction_tier": extraction.tier if extraction else "skipped",
        "extracted": _extraction_public(extraction),
        "approval_id": str(approval_id),
        "message": _summary_message(extraction, ext),
    }


@router.get("/api/workspaces/{workspace_id}/entities/{entity_id}/documents")
async def list_documents(
    workspace_id: UUID,
    entity_id: UUID,
    limit: int = 50,
    offset: int = 0,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict[str, Any]:
    """List uploaded documents for an entity (metadata only — never the bytes).

    Read floor: viewer. Entity-scope enforced. The object_key/bucket are
    returned so an admin tool could fetch via a signed URL later, but the raw
    file content is never served by this endpoint.
    """
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, entity_id)

    async with workspace_session(SessionLocal, workspace_id) as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT r.id::text AS id, r.content_hash, r.mime_type,
                           r.amount, r.currency_code, r.extracted_at, r.created_at,
                           r.object_key,
                           (SELECT aq.status::text FROM approval_queue aq
                              WHERE aq.subject_type = 'document_review'
                                AND aq.subject_id = r.id
                              ORDER BY aq.created_at DESC LIMIT 1) AS review_status
                    FROM receipt r
                    WHERE r.entity_id = :eid
                    ORDER BY r.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {
                    "eid": str(entity_id),
                    "limit": min(max(limit, 1), 200),
                    "offset": max(offset, 0),
                },
            )
        ).mappings().all()
        total = (
            await session.execute(
                text("SELECT count(*) FROM receipt WHERE entity_id = :eid"),
                {"eid": str(entity_id)},
            )
        ).scalar_one()

    return {"documents": [dict(r) for r in rows], "total": total}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _read_capped(file: UploadFile, max_bytes: int) -> bytes | None:
    """Read an UploadFile into memory, returning None if it exceeds max_bytes.

    Reads in chunks and aborts as soon as the cap is crossed so a multi-GB
    upload can't exhaust memory before we notice.
    """
    chunks: list[bytes] = []
    total = 0
    chunk_size = 1024 * 256
    while True:
        chunk = await file.read(chunk_size)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _maybe_extract(declared_mime: str, body: bytes):
    """Run the local VLM on receipt IMAGES only; never raises.

    Returns a ReceiptExtraction or None. None means "extraction was not
    attempted" (a PDF/CSV in Phase 1) — distinct from tier='none' which means
    "attempted but nothing usable". Both still result in a queued review item.
    """
    from app.categorization.receipt_vlm import ReceiptVLMExtractor

    extractor = ReceiptVLMExtractor()
    if not extractor.can_extract(declared_mime):
        return None
    try:
        return await extractor.extract(image_bytes=body, mime_type=declared_mime)
    except Exception:  # noqa: BLE001 — extraction must NEVER break the upload
        return None


def _coerce_amount(extraction) -> tuple[Decimal | None, str | None]:
    """Turn an extraction into (Decimal amount, currency) for the DB columns.

    The receipt + approval_queue amount columns are NUMERIC; currency is a
    CHAR(3) with an upper-case check constraint. A missing/invalid amount → None
    (the row + approval still get created; the human supplies the number).
    """
    if extraction is None or not extraction.amount:
        return None, None
    try:
        amt = Decimal(extraction.amount)
    except (InvalidOperation, TypeError, ValueError):
        return None, None
    if amt < 0:
        return None, None
    currency = (extraction.currency or "USD").upper()
    if len(currency) != 3 or not currency.isalpha():
        currency = "USD"
    return amt, currency


def _build_approval(
    *,
    receipt_id: UUID,
    content_hash: str,
    object_key: str,
    bucket: str,
    mime: str,
    ext: str,
    extraction,
    filename: str | None,
) -> tuple[dict[str, Any], str]:
    """Build the approval_queue payload + human-readable reasoning string.

    The payload is a self-describing record of a PROPOSED transaction derived
    from an uploaded document. It is intentionally NOT in the shape the
    bank_transaction_import approve-path consumes — a 'document_review' item is
    a human triage task (confirm/categorize/attach), not a one-click ledger
    post. The decide endpoint treats unknown subject_types as non-applying
    (returns 422), which is the safe default: a document_review must be handled
    via the review UI, never silently posted. See the SUMMARY's Phase-2 notes.
    """
    is_image = ext not in ("pdf", "csv")
    doc_kind = "receipt" if is_image else ("statement_or_export" if ext == "csv" else "pdf_document")

    payload: dict[str, Any] = {
        "kind": "document_review",
        "document_kind": doc_kind,
        "receipt_id": str(receipt_id),
        "content_hash": content_hash,
        "garage_bucket": bucket,
        "object_key": object_key,
        "mime_type": mime,
        "original_filename": filename,
        "extraction_attempted": extraction is not None,
    }
    if extraction is not None:
        payload["extraction"] = {
            "vendor": extraction.vendor,
            "amount": extraction.amount,
            "date": extraction.date,
            "currency": extraction.currency,
            "confidence": extraction.confidence,
            "tier": extraction.tier,
            "model": extraction.model,
            "error": extraction.error,
        }

    # Reasoning copy — what a human sees in the queue.
    if extraction is None:
        if ext == "csv":
            reasoning = (
                f"Uploaded CSV '{filename or content_hash[:8]}' stored for review. "
                "Bank-statement/CSV parsing is not automated yet — a human should "
                "review and import the relevant rows."
            )
        else:
            reasoning = (
                f"Uploaded PDF '{filename or content_hash[:8]}' stored for review. "
                "PDF extraction is not automated yet — a human should review it."
            )
    elif extraction.tier == "high":
        reasoning = (
            f"Receipt extracted (high confidence): vendor={extraction.vendor!r}, "
            f"amount={extraction.amount}, date={extraction.date}. "
            "Proposed for human confirmation before any ledger entry."
        )
    elif extraction.tier == "low":
        present = ", ".join(
            f"{k}={v}"
            for k, v in (
                ("vendor", extraction.vendor),
                ("amount", extraction.amount),
                ("date", extraction.date),
            )
            if v
        )
        reasoning = (
            "Receipt partially read (low confidence)"
            + (f": {present}. " if present else ". ")
            + "Needs a human to fill in the rest before any ledger entry."
        )
    else:  # tier == "none"
        why = f" ({extraction.error})" if extraction.error else ""
        reasoning = (
            "Receipt uploaded but nothing could be read automatically"
            + why
            + ". Queued for a human to review."
        )
    return payload, reasoning


def _extraction_public(extraction) -> dict[str, Any] | None:
    """The subset of the extraction safe + useful to return to the upload UI."""
    if extraction is None:
        return None
    return {
        "vendor": extraction.vendor,
        "amount": extraction.amount,
        "date": extraction.date,
        "currency": extraction.currency,
        "confidence": extraction.confidence,
        "tier": extraction.tier,
    }


def _summary_message(extraction, ext: str) -> str:
    if extraction is None:
        return "Stored and queued for review."
    if extraction.tier == "high":
        return "Receipt read and queued for your confirmation."
    if extraction.tier == "low":
        return "Receipt partially read — queued for review."
    return "Stored and queued for review (couldn't read it automatically)."


def _json(value: dict[str, Any]) -> str:
    import json

    return json.dumps(value)


__all__ = ("router",)
