"""MCP transaction tools (read-only) over the posted journal.

A "transaction" here is a ``journal_entry`` row (the append-only ledger), not the
live SimpleFIN feed the dashboard renders. The caller is authorized + entity-
scoped via ``authorize_workspace`` before any query, and all reads run inside a
``workspace_session`` so the workspace search_path + RLS GUC are bound (a query
that ever landed in the wrong schema would see zero rows, not another tenant's).
"""
from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.db import SessionLocal
from app.mcp_auth import authorize_workspace
from app.workspace_session import entity_session

# Columns surfaced in the list projection. Kept narrow + display-oriented; the
# full audit payload (reasoning trace, hash chain, model version) is on the
# single-transaction get_transaction call.
_LIST_COLUMNS = (
    "id, effective_date, posted_at, amount, currency_code, "
    "usd_equivalent_at_event_time, debit_account, credit_account, category, "
    "payee, description, memo, status, materiality_tier, source, "
    "counterparty_id, receipt_link_id"
)

_MAX_LIMIT = 200


def _b(value: Any) -> Any:
    """JSON-safe scalar: stringify Decimals/dates/UUIDs, pass through the rest."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool, str)):
        return value
    return str(value)


async def list_transactions(
    workspace_id: UUID,
    entity_id: UUID,
    filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List entity-scoped journal transactions, newest effective_date first.

    `filter` (all optional):
      - status:      journal status (e.g. 'posted', 'pending')
      - start_date / end_date: ISO dates bounding effective_date (inclusive)
      - category:    exact category match
      - limit:       page size (default 50, max 200)
      - offset:      page offset (default 0)
    Unknown keys are ignored. Returns a list of journal-row projections.
    """
    f = filter or {}
    clauses = ["entity_id = :entity_id"]
    params: dict[str, Any] = {"entity_id": str(entity_id)}
    if f.get("status"):
        clauses.append("status = :status")
        params["status"] = str(f["status"])
    if f.get("start_date"):
        clauses.append("effective_date >= :start_date")
        params["start_date"] = str(f["start_date"])
    if f.get("end_date"):
        clauses.append("effective_date <= :end_date")
        params["end_date"] = str(f["end_date"])
    if f.get("category"):
        clauses.append("category = :category")
        params["category"] = str(f["category"])
    try:
        limit = min(int(f.get("limit", 50)), _MAX_LIMIT)
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(int(f.get("offset", 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    params["limit"] = limit
    params["offset"] = offset

    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    where = " AND ".join(clauses)
    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, _eid):
        rows = (
            await session.execute(
                text(
                    f"SELECT {_LIST_COLUMNS} FROM journal_entry "
                    f"WHERE {where} "
                    "ORDER BY effective_date DESC, posted_at DESC "
                    "LIMIT :limit OFFSET :offset"
                ),
                params,
            )
        ).mappings().all()
    return [{k: _b(v) for k, v in dict(r).items()} for r in rows]


async def get_transaction(
    workspace_id: UUID,
    entity_id: UUID,
    id: UUID,
) -> dict[str, Any]:
    """Fetch one entity-scoped journal entry with its full "show your work" payload.

    Returns the ledger fields plus the audit/provenance metadata: reasoning
    trace, applied rule version, model version, decision verdict, hash-chain
    pointers (hex), linked receipt + source-document hash. Raises a not-found
    error if the id is not in this entity.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    async with entity_session(SessionLocal, workspace_id, entity_id) as (session, _eid):
        row = (
            await session.execute(
                text(
                    "SELECT id, entity_id, event_id, effective_date, posted_at, "
                    "amount, currency_code, usd_equivalent_at_event_time, fx_source, "
                    "debit_account, credit_account, category, payee, description, "
                    "memo, status, materiality_tier, source, source_external_id, "
                    "counterparty_id, receipt_link_id, source_document_id, "
                    "source_document_hash, reasoning_trace, rule_version_applied, "
                    "model_version, decision_verdict, identity, "
                    "approving_human_uc_uid, reverses_entry_id, "
                    "encode(prev_hash, 'hex') AS prev_hash, "
                    "encode(this_hash, 'hex') AS this_hash "
                    "FROM journal_entry WHERE id = :id AND entity_id = :entity_id"
                ),
                {"id": str(id), "entity_id": str(entity_id)},
            )
        ).mappings().first()
    if row is None:
        raise LookupError(f"transaction {id} not found in entity {entity_id}")
    return {k: _b(v) for k, v in dict(row).items()}
