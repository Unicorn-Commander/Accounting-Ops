from typing import Any
from uuid import UUID


async def list_transactions(tenant_id: UUID, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """List tenant-scoped transaction-shaped journal projections for MCP read tools (§11.1)."""
    raise NotImplementedError("Phase 1")


async def get_transaction(tenant_id: UUID, id: UUID) -> dict[str, Any]:
    """Fetch one tenant-scoped transaction projection with audit metadata (§4.2, §11.1)."""
    raise NotImplementedError("Phase 1")
