from typing import Any
from uuid import UUID


async def get_p_and_l(
    workspace_id: UUID,
    entity_id: UUID,
    period: str,
) -> dict[str, Any]:
    """Generate an entity-scoped P&L view from append-only journal projections.

    `period` is ISO 8601 month/quarter/year (e.g., '2026-05', '2026-Q2', '2026').
    Currency is the entity's functional currency.
    """
    raise NotImplementedError("Phase 1")


async def get_runway(
    workspace_id: UUID,
    entity_id: UUID,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Model entity-scoped runway for treasury scenarios.

    Forward-looking. Uses trailing burn rate + recurring detection +
    user-provided one-offs to project cash position N months out.
    """
    raise NotImplementedError("Phase 1")


async def get_consolidated_p_and_l(
    workspace_id: UUID,
    period: str,
    entity_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    """Workspace-level consolidated P&L across multiple entities.

    Used by the consolidation overlay agent. Requires the caller to have a
    membership role with no entity_scope restriction (i.e., owner/admin).
    `entity_ids = None` means all active entities in workspace.
    """
    raise NotImplementedError("Phase 1")
