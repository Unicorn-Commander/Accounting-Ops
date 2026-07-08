"""Counterparty list / search / upsert / bulk-import (tenant-schema scoped).

All functions take an ``AsyncSession`` already bound to the workspace schema
(via ``app.workspace_session.workspace_session``) — they never open their own
session, so the caller controls the transaction + RLS context. This mirrors the
AR service in ``app/ar/invoicing.py`` and reuses the same ``counterparty``
table + ``external_refs`` JSONB convention (a thin ``contact_id`` reference is
kept for later Contact-Ops reconciliation; email/phone/address are convenience
snapshots for rendering).
"""
from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Kinds we accept for an imported contact. Anything else is coerced to
# "customer" so a bad client payload can't write arbitrary kinds.
_ALLOWED_KINDS = frozenset({"customer", "vendor", "both", "other"})


def _norm_kind(kind: str | None) -> str:
    k = (kind or "").strip().lower()
    return k if k in _ALLOWED_KINDS else "customer"


def _refs_snapshot(
    *,
    contact_id: str | None,
    email: str | None,
    phone: str | None,
    address: str | None,
    company: str | None,
    person_name: str | None,
    source: str | None,
) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    if contact_id:
        refs["contact_id"] = str(contact_id)
    if email:
        refs["email"] = email
    if phone:
        refs["phone"] = phone
    if address:
        refs["billing_address"] = address
    if company:
        refs["company"] = company
    if person_name:
        refs["person_name"] = person_name
    if source:
        refs["import_source"] = source
    return refs


async def list_counterparties(
    session: AsyncSession,
    *,
    q: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """List/search counterparties in the active workspace schema.

    Returns ``{"items": [...], "total": N}``. Search matches display_name OR
    the email snapshot, case-insensitively.
    """
    where = ["1=1"]
    params: dict[str, Any] = {"limit": max(1, min(limit, 1000)), "offset": max(0, offset)}
    if kind:
        where.append("kind = :kind")
        params["kind"] = _norm_kind(kind)
    if q:
        where.append(
            "(lower(display_name) LIKE :q OR lower(external_refs->>'email') LIKE :q)"
        )
        params["q"] = f"%{q.strip().lower()}%"
    where_sql = " AND ".join(where)

    total = (
        await session.execute(
            text(f"SELECT count(*) AS c FROM counterparty WHERE {where_sql}"),
            params,
        )
    ).scalar_one()

    rows = (
        await session.execute(
            text(
                f"""
                SELECT
                    id::text                          AS id,
                    display_name,
                    kind,
                    external_refs->>'email'           AS email,
                    external_refs->>'phone'           AS phone,
                    external_refs->>'billing_address' AS address,
                    external_refs->>'company'         AS company,
                    external_refs->>'contact_id'      AS contact_id,
                    external_refs->>'import_source'    AS source,
                    created_at
                FROM counterparty
                WHERE {where_sql}
                ORDER BY lower(display_name)
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
    ).mappings().all()
    return {"items": [dict(r) for r in rows], "total": int(total)}


async def upsert_counterparty(
    session: AsyncSession,
    *,
    workspace_id: str,
    name: str,
    kind: str = "customer",
    contact_id: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    address: str | None = None,
    company: str | None = None,
    person_name: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """Find-or-create a counterparty. Idempotent.

    Match order: (1) external_refs->>'contact_id', (2) email snapshot,
    (3) case-insensitive display_name within the same kind. New refs are MERGED
    onto an existing row (never clobbering unrelated keys).

    Returns ``{"id", "existed_before", "display_name", "kind"}``.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("contact name is required")
    kind = _norm_kind(kind)
    refs = _refs_snapshot(
        contact_id=contact_id,
        email=email,
        phone=phone,
        address=address,
        company=company,
        person_name=person_name,
        source=source,
    )

    existing = None
    if contact_id:
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id, display_name FROM counterparty "
                    "WHERE external_refs->>'contact_id' = :cid LIMIT 1"
                ),
                {"cid": str(contact_id)},
            )
        ).mappings().first()
    if existing is None and email:
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id, display_name FROM counterparty "
                    "WHERE lower(external_refs->>'email') = lower(:em) LIMIT 1"
                ),
                {"em": email},
            )
        ).mappings().first()
    if existing is None:
        existing = (
            await session.execute(
                text(
                    "SELECT id::text AS id, display_name FROM counterparty "
                    "WHERE kind = :kind AND lower(display_name) = lower(:name) LIMIT 1"
                ),
                {"kind": kind, "name": name},
            )
        ).mappings().first()

    if existing is not None:
        await session.execute(
            text(
                "UPDATE counterparty "
                "SET external_refs = external_refs || CAST(:refs AS jsonb) "
                "WHERE id = :id"
            ),
            {"refs": json.dumps(refs), "id": existing["id"]},
        )
        return {
            "id": existing["id"],
            "existed_before": True,
            "display_name": existing["display_name"],
            "kind": kind,
        }

    new_id = uuid4()
    await session.execute(
        text(
            "INSERT INTO counterparty (id, workspace_id, display_name, kind, external_refs) "
            "VALUES (:id, :wid, :name, :kind, CAST(:refs AS jsonb))"
        ),
        {
            "id": str(new_id),
            "wid": workspace_id,
            "name": name,
            "kind": kind,
            "refs": json.dumps(refs),
        },
    )
    return {
        "id": str(new_id),
        "existed_before": False,
        "display_name": name,
        "kind": kind,
    }


async def bulk_import_counterparties(
    session: AsyncSession,
    *,
    workspace_id: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Upsert many contacts. Each row: ``{display_name|name, kind?, email?,
    phone?, address?, company?, person_name?, contact_id?, source?}``.

    Rows with no name are skipped. Per-row failures are collected (one bad row
    never aborts the batch). Returns counts + the created/updated ids.
    """
    created = 0
    updated = 0
    skipped = 0
    errors: list[dict[str, Any]] = []
    ids: list[str] = []

    for idx, row in enumerate(rows):
        name = (row.get("display_name") or row.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        try:
            res = await upsert_counterparty(
                session,
                workspace_id=workspace_id,
                name=name,
                kind=row.get("kind") or "customer",
                contact_id=row.get("contact_id"),
                email=row.get("email"),
                phone=row.get("phone"),
                address=row.get("address"),
                company=row.get("company"),
                person_name=row.get("person_name"),
                source=row.get("source") or "import",
            )
            ids.append(res["id"])
            if res["existed_before"]:
                updated += 1
            else:
                created += 1
        except Exception as exc:  # noqa: BLE001 — collect, don't abort the batch
            skipped += 1
            errors.append({"index": idx, "name": name, "error": str(exc)})

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total": len(rows),
        "ids": ids,
        "errors": errors,
    }


__all__ = (
    "list_counterparties",
    "upsert_counterparty",
    "bulk_import_counterparties",
)
