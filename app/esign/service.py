"""SignatureRequest lifecycle service.

The state machine over the ``signature_request`` tenant table:

    created --(provider.create_request)--> sent | out_for_signature
                                              |
                  (provider reports / re-upload)
                                              v
                              signed | declined | canceled  (terminal)

Functions run INSIDE the caller's ``workspace_session`` transaction (the
workspace search_path + ``app.current_workspace`` RLS GUC are already bound).
The API layer renders the work-product PDF (via
``app.api.work_products._render_work_product``, which enforces anti-fabrication
provenance) and passes the bytes here as ``pdf_bytes`` — keeping this module
decoupled from the form-rendering layer (no import cycle).

SIGNED-PDF STORAGE
==================
The completed/signed PDF is stored to Garage (the repo's existing S3 helper,
``app.clients.garage_s3.GarageS3Client``) when it is configured — the row's
``signed_garage_key`` is set. Otherwise it falls back to the local filesystem
under ``ESIGN_SIGNED_DIR`` (default: a directory under the system temp dir) and
``signed_path`` is set. If a Garage PUT fails at runtime it also falls back to
the filesystem (with a warning) so completion is never lost. Exactly one of
``signed_garage_key`` / ``signed_path`` is set per stored document.
"""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.garage_s3 import (
    GarageNotConfigured,
    GarageS3Client,
    GarageUploadError,
)
from app.config import get_settings
from app.esign.provider import (
    STATUS_SENT,
    STATUS_SIGNED,
    TERMINAL_STATUSES,
    EsignProvider,
    ManualProvider,
    coerce_status,
    resolve_provider,
)

_log = logging.getLogger("acctops.esign.service")

# Default directory for the filesystem fallback. Production should set
# ESIGN_SIGNED_DIR to a persistent volume (or configure Garage); the temp-dir
# default keeps the capability functional out of the box for dev / dogfood.
_DEFAULT_SIGNED_DIR = os.path.join(tempfile.gettempdir(), "accounting_ops_esign_signed")


def _signed_dir() -> str:
    return os.getenv("ESIGN_SIGNED_DIR", _DEFAULT_SIGNED_DIR)


# ---------------------------------------------------------------------------
# Signed-PDF storage
# ---------------------------------------------------------------------------


async def _store_signed_pdf(
    workspace_id: UUID | str, sr_id: UUID | str, signed_bytes: bytes
) -> tuple[str | None, str | None]:
    """Persist the signed PDF. Returns ``(garage_key, local_path)`` with exactly
    one element non-None.

    Garage when configured; otherwise (or on a Garage error) the local
    filesystem under ``ESIGN_SIGNED_DIR``."""
    settings = get_settings()
    s3 = GarageS3Client(settings)
    if s3.is_configured():
        key = f"{workspace_id}/esign/signed/{sr_id}.pdf"
        try:
            await s3.put_object(
                bucket=settings.garage_bucket_prefix,
                object_key=key,
                body=signed_bytes,
                content_type="application/pdf",
            )
            return key, None
        except (GarageNotConfigured, GarageUploadError) as exc:
            # Never lose a completed signature to a transient object-store issue.
            _log.warning(
                "esign: Garage store failed (%s); falling back to filesystem", exc
            )

    directory = Path(_signed_dir()) / str(workspace_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sr_id}.pdf"
    path.write_bytes(signed_bytes)
    return None, str(path)


# ---------------------------------------------------------------------------
# Provider selection for an existing row
# ---------------------------------------------------------------------------


def _provider_for_row(row) -> EsignProvider:
    """The provider to use when re-syncing an existing request.

    A row created by the Manual provider is ALWAYS handled manually (even if a
    Docuseal instance is configured now — its external_id is not a Docuseal
    submission). Otherwise defer to ``resolve_provider`` (Docuseal if env is
    set, else Manual)."""
    provider_name = (row.get("provider") or "").strip().lower() if hasattr(row, "get") else ""
    if provider_name == ManualProvider.name:
        return ManualProvider()
    return resolve_provider()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def request_signature(
    session: AsyncSession,
    *,
    workspace_id: UUID | str,
    entity_id: UUID | str,
    work_product_id: UUID | str,
    signer_name: str,
    signer_email: str | None,
    pdf_bytes: bytes,
    subject: str | None = None,
    provider: EsignProvider | None = None,
) -> dict:
    """Create a signature request and dispatch it to the provider.

    Inserts a row at ``created``, calls ``provider.create_request`` with the
    rendered PDF bytes, then advances the row to the provider-reported status
    (nominally ``sent``; the Manual provider lands on ``out_for_signature``).

    This runs inside the caller's transaction: if the provider raises
    (e.g. :class:`ProviderUnavailable`), the whole unit of work rolls back and
    no orphan ``created`` row is left behind."""
    provider = provider or resolve_provider()
    sr_id = uuid.uuid4()

    await session.execute(
        text(
            """
            INSERT INTO signature_request (
                id, workspace_id, entity_id, work_product_id, provider, status,
                signer_name, signer_email
            ) VALUES (
                :id, :wid, :eid, :wpid, :provider, 'created',
                :signer_name, :signer_email
            )
            """
        ),
        {
            "id": str(sr_id),
            "wid": str(workspace_id),
            "eid": str(entity_id),
            "wpid": str(work_product_id),
            "provider": provider.name,
            "signer_name": signer_name,
            "signer_email": signer_email,
        },
    )

    ack = await provider.create_request(pdf_bytes, signer_name, signer_email, subject)
    status = coerce_status(ack.get("status"), default=STATUS_SENT)
    external_id = ack.get("external_id")
    signing_url = ack.get("signing_url")

    await session.execute(
        text(
            """
            UPDATE signature_request
            SET external_id = :external_id,
                signing_url = :signing_url,
                status = :status,
                updated_at = now()
            WHERE id = :id
            """
        ),
        {
            "id": str(sr_id),
            "external_id": external_id,
            "signing_url": signing_url,
            "status": status,
        },
    )

    return {
        "id": str(sr_id),
        "work_product_id": str(work_product_id),
        "provider": provider.name,
        "status": status,
        "external_id": external_id,
        "signing_url": signing_url,
        "signer_name": signer_name,
        "signer_email": signer_email,
    }


async def sync_status(
    session: AsyncSession,
    *,
    entity_id: UUID | str,
    work_product_id: UUID | str,
    sr_id: UUID | str,
    provider: EsignProvider | None = None,
) -> dict | None:
    """Refresh a request's status from its provider (best-effort).

    Returns the current state dict, or ``None`` if the request does not exist in
    this entity's scope. Terminal requests are returned unchanged (no provider
    call). For a non-remote provider (Manual) there is nothing to poll, so the
    stored status is returned as-is. When the provider reports the document is
    signed and we have not yet stored it, the completed PDF is fetched and
    persisted (the request is then ``signed``)."""
    row = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, workspace_id::text AS workspace_id,
                       provider, external_id, status,
                       signed_garage_key, signed_path
                FROM signature_request
                WHERE id = :id AND entity_id = :eid AND work_product_id = :wpid
                """
            ),
            {"id": str(sr_id), "eid": str(entity_id), "wpid": str(work_product_id)},
        )
    ).mappings().first()
    if row is None:
        return None

    current = row["status"]
    if current in TERMINAL_STATUSES:
        return {"id": row["id"], "status": current}

    provider = provider or _provider_for_row(row)
    if not getattr(provider, "is_remote", True):
        # No external system to poll — manual requests advance via record_signed.
        return {"id": row["id"], "status": current}

    external_id = row["external_id"]
    if not external_id:
        return {"id": row["id"], "status": current}

    remote = await provider.get_status(external_id)
    new_status = coerce_status(remote, default=current)

    # If signed remotely and not yet captured, pull + store the completed PDF.
    already_stored = bool(row["signed_garage_key"] or row["signed_path"])
    if new_status == STATUS_SIGNED and not already_stored:
        signed_bytes = await provider.fetch_signed(external_id)
        if signed_bytes:
            return await record_signed(
                session,
                entity_id=entity_id,
                work_product_id=work_product_id,
                sr_id=sr_id,
                signed_bytes=signed_bytes,
                workspace_id=row["workspace_id"],
            )

    if new_status != current:
        await session.execute(
            text(
                """
                UPDATE signature_request
                SET status = :status, updated_at = now()
                WHERE id = :id
                """
            ),
            {"id": str(sr_id), "status": new_status},
        )
    return {"id": row["id"], "status": new_status}


async def record_signed(
    session: AsyncSession,
    *,
    entity_id: UUID | str,
    work_product_id: UUID | str,
    sr_id: UUID | str,
    signed_bytes: bytes | None,
    workspace_id: UUID | str | None = None,
) -> dict | None:
    """Mark a request ``signed`` and (when given bytes) store the signed PDF.

    Used by the Manual re-upload route and by ``sync_status`` when a remote
    provider reports completion. ``signed_bytes`` may be ``None`` to record a
    wet signature captured out-of-band (status flips to ``signed`` without a
    stored artifact). Returns the new state, or ``None`` if the request is not
    found. Raises :class:`ValueError` if the request is already canceled /
    declined (cannot be revived)."""
    row = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, workspace_id::text AS workspace_id, status
                FROM signature_request
                WHERE id = :id AND entity_id = :eid AND work_product_id = :wpid
                FOR UPDATE
                """
            ),
            {"id": str(sr_id), "eid": str(entity_id), "wpid": str(work_product_id)},
        )
    ).mappings().first()
    if row is None:
        return None
    if row["status"] in ("canceled", "declined"):
        raise ValueError(
            f"Signature request is '{row['status']}'; it cannot be marked signed."
        )

    wid = workspace_id or row["workspace_id"]
    garage_key: str | None = None
    signed_path: str | None = None
    if signed_bytes:
        garage_key, signed_path = await _store_signed_pdf(wid, sr_id, signed_bytes)

    await session.execute(
        text(
            """
            UPDATE signature_request
            SET status = 'signed',
                signed_garage_key = COALESCE(:gk, signed_garage_key),
                signed_path = COALESCE(:sp, signed_path),
                updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": str(sr_id), "gk": garage_key, "sp": signed_path},
    )
    return {
        "id": str(sr_id),
        "status": STATUS_SIGNED,
        "signed_garage_key": garage_key,
        "signed_path": signed_path,
    }


async def cancel(
    session: AsyncSession,
    *,
    entity_id: UUID | str,
    work_product_id: UUID | str,
    sr_id: UUID | str,
) -> dict | None:
    """Cancel a request (status -> ``canceled``).

    Returns the new state, or ``None`` if not found. Raises
    :class:`ValueError` if the request is already in a terminal state."""
    row = (
        await session.execute(
            text(
                """
                SELECT id::text AS id, status
                FROM signature_request
                WHERE id = :id AND entity_id = :eid AND work_product_id = :wpid
                FOR UPDATE
                """
            ),
            {"id": str(sr_id), "eid": str(entity_id), "wpid": str(work_product_id)},
        )
    ).mappings().first()
    if row is None:
        return None
    if row["status"] in TERMINAL_STATUSES:
        raise ValueError(
            f"Signature request is already '{row['status']}'; cannot cancel."
        )
    await session.execute(
        text(
            """
            UPDATE signature_request
            SET status = 'canceled', updated_at = now()
            WHERE id = :id
            """
        ),
        {"id": str(sr_id)},
    )
    return {"id": str(sr_id), "status": "canceled"}


__all__ = (
    "request_signature",
    "sync_status",
    "record_signed",
    "cancel",
)
