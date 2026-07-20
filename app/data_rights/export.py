"""Full workspace data export — the portability half of the data-rights surface.

What it produces
----------------
A single ZIP containing:

  manifest.json      what this export is, when, by whom, and what it contains
  json/<table>.json  every tenant table, one file each, full fidelity
  csv/<table>.csv    the same rows as CSV, for people who open things in Excel
  files/...          the actual stored documents and receipts, not just refs

Tenant scoping — belt AND braces
--------------------------------
Two independent mechanisms, deliberately:

  1. The whole export runs inside ``workspace_session(...)``, so PostgreSQL
     FORCE row-level security restricts every SELECT to the workspace named in
     the ``app.current_workspace`` GUC. If the GUC were unset, RLS returns ZERO
     rows — the failure mode is an empty export, never another tenant's data.
  2. Every SELECT *also* carries an explicit ``WHERE workspace_id = :wid``.

Mechanism 2 is redundant while mechanism 1 is working correctly. That is the
point: this is the code path that hands a customer a file, so a single
misconfiguration must not be sufficient to leak another customer's books. The
cross-tenant test asserts a second workspace's data never appears.

Table discovery is dynamic
--------------------------
Rather than hard-coding a table list that silently goes stale every time someone
adds a table, we ask the catalog for every table in the tenant schema carrying a
``workspace_id`` column — the same technique tenant migration 0026 uses to apply
RLS. A new table is therefore included in exports automatically. An export that
quietly omitted a table added last month would be a bad way to answer a
portability request.

Bounded memory
--------------
The ZIP is assembled in memory, so stored files are included up to
``MAX_EXPORT_FILE_BYTES`` in total. Beyond that, files are LISTED IN THE
MANIFEST AS SKIPPED, with their keys and a reason. They are never silently
dropped — an export that claims completeness it does not have is worse than one
that admits a limit. See ``docs/DATA-RIGHTS.md`` for the streaming follow-up.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.garage_s3 import (
    GarageNotConfigured,
    GarageS3Client,
    GarageUploadError,
)
from app.config import Settings, get_settings
from app.workspace_session import SHARED_SCHEMA

# Total budget for stored files pulled into the ZIP. 256 MB keeps a worker's
# memory bounded; the per-document upload cap is far smaller, so this is a
# many-document ceiling rather than a single-document one.
MAX_EXPORT_FILE_BYTES = 256 * 1024 * 1024

# Rows fetched per table. A guard against a pathological table, not a real
# limit for any workspace we expect. Truncation is recorded in the manifest.
MAX_ROWS_PER_TABLE = 500_000

EXPORT_FORMAT_VERSION = 1

# Table/identifier names come from the system catalog, so they are already
# trustworthy — but they get interpolated into SQL, so they are validated
# anyway. Defence in depth costs one regex.
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class ExportError(RuntimeError):
    """Export could not be produced."""


def _json_default(value: Any) -> Any:
    """Serialize the types that come out of this schema and are not JSON-native.

    ``bytes`` matters specifically: the journal hash-chain columns (prev_hash,
    this_hash) are BYTEA. They are emitted as hex so the customer receives the
    chain values that make their ledger independently verifiable — an export
    that dropped them would hand over books nobody could check.
    """
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        # str, not float — never lose precision on money in an export.
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=_json_default, sort_keys=True)
    coerced = _json_default(value) if not isinstance(value, (str, int, float)) else value
    return str(coerced)


@dataclass
class TableExport:
    name: str
    row_count: int
    truncated: bool = False


@dataclass
class SkippedFile:
    receipt_id: str
    object_key: str
    reason: str


@dataclass
class ExportResult:
    workspace_id: str
    tables: list[TableExport] = field(default_factory=list)
    files_included: int = 0
    file_bytes: int = 0
    files_skipped: list[SkippedFile] = field(default_factory=list)
    archive_bytes: int = 0
    object_key: str | None = None
    bucket: str | None = None

    def manifest_summary(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "tables": {t.name: t.row_count for t in self.tables},
            "tables_truncated": [t.name for t in self.tables if t.truncated],
            "total_rows": sum(t.row_count for t in self.tables),
            "files_included": self.files_included,
            "file_bytes": self.file_bytes,
            "files_skipped": [
                {"receipt_id": s.receipt_id, "object_key": s.object_key, "reason": s.reason}
                for s in self.files_skipped
            ],
            "archive_bytes": self.archive_bytes,
        }


async def discover_tenant_tables(session: AsyncSession) -> list[str]:
    """Every table in the tenant schema carrying a workspace_id column.

    Same catalog query shape tenant migration 0026 uses to decide what to apply
    RLS to. Keeping discovery dynamic means an export cannot silently go stale
    when a table is added.
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT c.relname AS name
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  JOIN pg_attribute a ON a.attrelid = c.oid
                 WHERE n.nspname = :schema
                   AND c.relkind = 'r'
                   AND a.attname = 'workspace_id'
                   AND NOT a.attisdropped
                 ORDER BY c.relname
                """
            ),
            {"schema": SHARED_SCHEMA},
        )
    ).mappings().all()
    return [r["name"] for r in rows if _IDENT_RE.match(r["name"])]


async def _dump_table(
    session: AsyncSession, table: str, workspace_id: UUID
) -> tuple[list[dict[str, Any]], bool]:
    """Read one table's rows for this workspace. Returns (rows, truncated).

    Both RLS and the explicit predicate constrain this — see module docstring.
    """
    if not _IDENT_RE.match(table):  # unreachable via discovery; assert anyway
        raise ExportError(f"refusing to export unsafe table identifier: {table!r}")
    result = await session.execute(
        text(
            f'SELECT * FROM {SHARED_SCHEMA}."{table}" '  # noqa: S608 - validated identifier
            "WHERE workspace_id = :wid LIMIT :lim"
        ),
        {"wid": str(workspace_id), "lim": MAX_ROWS_PER_TABLE + 1},
    )
    rows = [dict(r) for r in result.mappings().all()]
    truncated = len(rows) > MAX_ROWS_PER_TABLE
    return rows[:MAX_ROWS_PER_TABLE], truncated


def _write_table_files(zf: zipfile.ZipFile, table: str, rows: list[dict[str, Any]]) -> None:
    zf.writestr(
        f"json/{table}.json",
        json.dumps(rows, default=_json_default, indent=2, sort_keys=True),
    )
    buf = io.StringIO()
    if rows:
        # Union of keys across rows: a JSONB-bearing table can yield ragged
        # dicts, and a header taken from row 0 alone would drop columns.
        columns: list[str] = []
        seen: set[str] = set()
        for row in rows:
            for key in row:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: _csv_cell(row.get(c)) for c in columns})
    zf.writestr(f"csv/{table}.csv", buf.getvalue())


async def _collect_files(
    session: AsyncSession,
    zf: zipfile.ZipFile,
    workspace_id: UUID,
    result: ExportResult,
    settings: Settings,
) -> None:
    """Pull stored receipts/documents into the archive, within the byte budget."""
    receipts = (
        await session.execute(
            text(
                f"SELECT id, garage_bucket, object_key, mime_type "  # noqa: S608
                f'FROM {SHARED_SCHEMA}."receipt" WHERE workspace_id = :wid '
                "ORDER BY created_at"
            ),
            {"wid": str(workspace_id)},
        )
    ).mappings().all()
    if not receipts:
        return

    s3 = GarageS3Client(settings)
    if not s3.is_configured():
        # Loud, not silent: the customer is told the files were unavailable and
        # why, rather than receiving an archive that looks complete.
        for r in receipts:
            result.files_skipped.append(
                SkippedFile(str(r["id"]), r["object_key"], "object_store_not_configured")
            )
        return

    for r in receipts:
        if result.file_bytes >= MAX_EXPORT_FILE_BYTES:
            result.files_skipped.append(
                SkippedFile(str(r["id"]), r["object_key"], "export_size_budget_exceeded")
            )
            continue
        try:
            body = await s3.get_object(bucket=r["garage_bucket"], object_key=r["object_key"])
        except (GarageNotConfigured, GarageUploadError) as exc:
            result.files_skipped.append(
                SkippedFile(str(r["id"]), r["object_key"], f"fetch_failed: {type(exc).__name__}")
            )
            continue
        if result.file_bytes + len(body) > MAX_EXPORT_FILE_BYTES:
            result.files_skipped.append(
                SkippedFile(str(r["id"]), r["object_key"], "export_size_budget_exceeded")
            )
            continue
        # Key by receipt id, keeping the stored key's basename for readability.
        # The receipt id prefix guarantees uniqueness even if basenames collide.
        basename = r["object_key"].rsplit("/", 1)[-1] or "document"
        zf.writestr(f"files/{r['id']}/{basename}", body)
        result.files_included += 1
        result.file_bytes += len(body)


async def build_export(
    session: AsyncSession,
    workspace_id: UUID,
    *,
    requested_by_uc_uid: str,
    workspace_slug: str | None = None,
    settings: Settings | None = None,
) -> tuple[bytes, ExportResult]:
    """Build the export archive. Returns (zip_bytes, result).

    ``session`` MUST already be workspace-scoped (i.e. opened via
    ``workspace_session``), because RLS is the primary tenant guard here.
    """
    settings = settings or get_settings()
    result = ExportResult(workspace_id=str(workspace_id))
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for table in await discover_tenant_tables(session):
            rows, truncated = await _dump_table(session, table, workspace_id)
            _write_table_files(zf, table, rows)
            result.tables.append(TableExport(table, len(rows), truncated))

        await _collect_files(session, zf, workspace_id, result, settings)

        manifest = {
            "format_version": EXPORT_FORMAT_VERSION,
            "product": "Accounting-Ops",
            "workspace_id": str(workspace_id),
            "workspace_slug": workspace_slug,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "requested_by_uc_uid": requested_by_uc_uid,
            "contents": result.manifest_summary(),
            "notes": {
                "scope": (
                    "Every record held for this workspace. Scoped by PostgreSQL "
                    "row-level security AND an explicit workspace_id predicate."
                ),
                "money": "Monetary values are strings to preserve exact precision.",
                "hash_chain": (
                    "journal_entry.prev_hash / this_hash are hex-encoded SHA-256 "
                    "values. The ledger is an append-only per-entity hash chain: "
                    "each entry's hash covers its contents and links to the "
                    "previous entry for the same entity, so this export can be "
                    "independently verified for tampering."
                ),
                "files": (
                    "Stored documents are under files/<receipt_id>/. Anything "
                    "omitted is listed explicitly in contents.files_skipped — "
                    "this export never drops a file silently."
                ),
            },
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))

    data = buffer.getvalue()
    result.archive_bytes = len(data)
    return data, result


def export_object_key(workspace_id: UUID, job_id: UUID) -> str:
    return f"data-rights/{workspace_id}/exports/{job_id}/accounting-ops-export.zip"


def export_bucket(settings: Settings, workspace_id: UUID) -> str:
    """Same bucket-naming convention the document-upload path uses."""
    return f"{settings.garage_bucket_prefix}-{workspace_id}"


async def store_export(
    data: bytes, workspace_id: UUID, job_id: UUID, *, settings: Settings | None = None
) -> tuple[str, str]:
    """Upload the archive. Returns (bucket, object_key). Raises on failure."""
    settings = settings or get_settings()
    s3 = GarageS3Client(settings)
    bucket = export_bucket(settings, workspace_id)
    key = export_object_key(workspace_id, job_id)
    await s3.put_object(
        bucket=bucket, object_key=key, body=data, content_type="application/zip"
    )
    return bucket, key


__all__ = (
    "EXPORT_FORMAT_VERSION",
    "MAX_EXPORT_FILE_BYTES",
    "MAX_ROWS_PER_TABLE",
    "ExportError",
    "ExportResult",
    "SkippedFile",
    "TableExport",
    "build_export",
    "discover_tenant_tables",
    "export_bucket",
    "export_object_key",
    "store_export",
)
