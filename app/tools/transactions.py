from typing import Any
from uuid import UUID


async def list_transactions(
    workspace_id: UUID,
    entity_id: UUID,
    filter: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """List entity-scoped transactions for MCP read tools.

    Reads from journal_entry table within the workspace schema, filtered to
    rows where entity_id matches. Pagination, sort, and filter conditions
    (date range, counterparty, category, status) come from `filter`.
    """
    raise NotImplementedError("Phase 1")


async def get_transaction(
    workspace_id: UUID,
    entity_id: UUID,
    id: UUID,
) -> dict[str, Any]:
    """Fetch one entity-scoped transaction projection with full audit metadata.

    Returns: journal_entry fields + linked receipt thumbnail + reasoning trace
    + rule version + model version. The full "show your work" payload.
    """
    raise NotImplementedError("Phase 1")
