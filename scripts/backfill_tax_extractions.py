"""Backfill / reprocess tax-document extractions over ALREADY-stored documents.

The upload path only extracts NEW (non-duplicate) receipts — a re-upload of an
existing document is deduped and returns before the extraction task is scheduled.
So documents uploaded BEFORE the tax-OCR pipeline existed (or any doc we want to
re-run after improving the pipeline) are processed HERE: for each PDF receipt in
a workspace that has no extraction yet (or a failed one, with --include-failed),
fetch the bytes back from Garage, run the vision extractor, and UPSERT the result
into document_extraction (the same row the live upload path writes).

Safe + idempotent: reads Garage, never deletes; the UPSERT is keyed on
receipt_id (one row per source doc). Runs as the least-privilege app role via
workspace_session (RLS enforced on every query). Best-effort per document — one
failure (bad scan, Garage miss) is logged and never aborts the run.

Usage (inside the app container):
  docker exec accounting_ops-app-1 python scripts/backfill_tax_extractions.py \
      --workspace "Magic Unicorn" [--limit 100] [--dry-run] [--include-failed]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.categorization.tax_doc_vlm import TaxDocExtractor
from app.clients.garage_s3 import (
    GarageNotConfigured,
    GarageS3Client,
    GarageUploadError,
)
from app.config import get_admin_database_url, get_settings
from app.db import SessionLocal
from app.workspace_session import workspace_session

# Same UPSERT the live upload path uses (app/api/documents.py :: _run_tax_extraction).
_UPSERT = text(
    """
    INSERT INTO document_extraction (
        id, workspace_id, entity_id, receipt_id,
        doc_type, tax_year, payer_name, recipient_name,
        fields, confidence, status, error, model, duration_ms, extracted_at
    ) VALUES (
        :id, :wid, :eid, :rid,
        :doc_type, :tax_year, :payer, :recipient,
        CAST(:fields AS jsonb), :confidence, :status, :error, :model,
        :duration_ms, now()
    )
    ON CONFLICT (receipt_id) DO UPDATE SET
        doc_type = EXCLUDED.doc_type,
        tax_year = EXCLUDED.tax_year,
        payer_name = EXCLUDED.payer_name,
        recipient_name = EXCLUDED.recipient_name,
        fields = EXCLUDED.fields,
        confidence = EXCLUDED.confidence,
        status = EXCLUDED.status,
        error = EXCLUDED.error,
        model = EXCLUDED.model,
        duration_ms = EXCLUDED.duration_ms,
        extracted_at = now()
    """
)


async def _resolve_workspace(identifier: str) -> tuple[uuid.UUID, str]:
    """Resolve a workspace UUID + display_name from a UUID or a name/slug substring."""
    engine = create_async_engine(get_admin_database_url(), pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            try:
                wid = uuid.UUID(identifier)
            except ValueError:
                rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT id::text, display_name FROM public.workspace
                            WHERE slug ILIKE :q OR display_name ILIKE :q
                            ORDER BY created_at LIMIT 5
                            """
                        ),
                        {"q": f"%{identifier}%"},
                    )
                ).all()
                if not rows:
                    raise SystemExit(f"No workspace matches {identifier!r}")
                if len(rows) > 1:
                    opts = ", ".join(f"{n} ({i})" for i, n in rows)
                    raise SystemExit(f"{identifier!r} is ambiguous: {opts}")
                return uuid.UUID(rows[0][0]), rows[0][1]
            row = (
                await conn.execute(
                    text("SELECT id::text, display_name FROM public.workspace WHERE id = :id"),
                    {"id": str(wid)},
                )
            ).first()
            if row is None:
                raise SystemExit(f"No workspace with id {identifier}")
            return uuid.UUID(row[0]), row[1]
    finally:
        await engine.dispose()


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workspace", required=True, help="Workspace UUID or name/slug substring")
    p.add_argument("--limit", type=int, default=100, help="Max documents to process")
    p.add_argument("--dry-run", action="store_true", help="List candidates, change nothing")
    p.add_argument(
        "--include-failed",
        action="store_true",
        help="Also reprocess documents whose extraction is currently status='failed'",
    )
    args = p.parse_args()

    wid, wname = await _resolve_workspace(args.workspace)
    print(f"workspace: {wname} ({wid})")

    settings = get_settings()
    garage = GarageS3Client(settings)
    extractor = TaxDocExtractor(settings)

    having = "de.id IS NULL" if not args.include_failed else "(de.id IS NULL OR de.status = 'failed')"
    async with workspace_session(SessionLocal, wid) as session:
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT r.id::text AS id, r.entity_id::text AS entity_id,
                           r.garage_bucket, r.object_key, r.mime_type
                    FROM receipt r
                    LEFT JOIN document_extraction de ON de.receipt_id = r.id
                    WHERE r.mime_type = 'application/pdf' AND {having}
                    ORDER BY r.created_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": max(1, args.limit)},
            )
        ).mappings().all()

    print(f"found {len(rows)} PDF document(s) needing extraction")
    if args.dry_run:
        for r in rows:
            print(f"  would process receipt={r['id']} key={r['object_key']}")
        return 0

    ok = bad = 0
    for r in rows:
        rid = r["id"]
        try:
            body = await garage.get_object(
                bucket=r["garage_bucket"], object_key=r["object_key"]
            )
        except (GarageUploadError, GarageNotConfigured) as exc:
            print(f"  receipt={rid}: GARAGE FETCH FAILED ({exc})")
            bad += 1
            continue

        result = await extractor.extract(body=body, mime_type=r["mime_type"])
        status_val = "failed" if result.error else "extracted"
        confidence = (
            Decimal(str(result.confidence)) if result.confidence is not None else None
        )
        async with workspace_session(SessionLocal, wid) as session:
            await session.execute(
                _UPSERT,
                {
                    "id": str(uuid.uuid4()),
                    "wid": str(wid),
                    "eid": r["entity_id"],
                    "rid": rid,
                    "doc_type": result.doc_type,
                    "tax_year": result.tax_year,
                    "payer": result.payer_name,
                    "recipient": result.recipient_name,
                    "fields": json.dumps(result.fields),
                    "confidence": confidence,
                    "status": status_val,
                    "error": result.error,
                    "model": result.model or None,
                    "duration_ms": result.duration_ms or None,
                },
            )
        if status_val == "extracted":
            ok += 1
            print(
                f"  receipt={rid}: OK  {result.doc_type}"
                f"{f'  TY{result.tax_year}' if result.tax_year else ''}"
                f"  payer={result.payer_name!r}  fields={len(result.fields)}"
            )
        else:
            bad += 1
            print(f"  receipt={rid}: unread ({result.error})")

    print(f"done: {ok} extracted, {bad} unread/failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
