"""MCP reporting tools (read-only) over the posted journal.

Every tool authorizes the CURRENT MCP caller against the requested workspace
(and entity) via ``app.mcp_auth.authorize_workspace`` before touching data — the
caller's token decides what is reachable, never the tool arguments. The P&L math
is the single shared implementation in ``app.api.reports.compute_profit_and_loss``
so the MCP surface and the HTTP report surface can never produce different
numbers.
"""
from typing import Any
from uuid import UUID

from app.mcp_auth import authorize_workspace


async def get_p_and_l(
    workspace_id: UUID,
    entity_id: UUID,
    period: str,
) -> dict[str, Any]:
    """Generate an entity-scoped P&L view from append-only journal projections.

    `period` is ISO 8601 month/quarter/year (e.g., '2026-05', '2026-Q2', '2026')
    or a relative token ('mtd', 'qtd', 'ytd') or a 'YYYY-MM-DD..YYYY-MM-DD' range.
    Currency is the entity's functional currency.
    """
    from app.api.reports import compute_profit_and_loss

    ctx = await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    return await compute_profit_and_loss(ctx, period, str(entity_id))


async def get_runway(
    workspace_id: UUID,
    entity_id: UUID,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Model entity-scoped runway for treasury scenarios.

    DEFERRED (Track A follow-up): runway is forward-looking financial modeling
    (trailing burn + recurring detection + scenario one-offs), not a reuse of an
    already-tested computation like P&L. It is being implemented separately with
    its own regression tests rather than shipped unverified on the MCP surface.
    The tool is authorized like the others so the contract is stable.
    """
    await authorize_workspace(workspace_id, entity_id, min_role="viewer")
    raise NotImplementedError(
        "get_runway: forward-looking runway modeling is a Track-A follow-up "
        "(shipping with its own regression tests); use get_p_and_l for now."
    )


async def get_consolidated_p_and_l(
    workspace_id: UUID,
    period: str,
    entity_ids: list[UUID] | None = None,
) -> dict[str, Any]:
    """Workspace-level consolidated P&L across multiple entities.

    Requires an admin/owner membership with NO entity_scope restriction
    (enforced by authorize_workspace(require_unscoped=True)). `entity_ids = None`
    consolidates ALL entities in the workspace; a provided list restricts the
    report to exactly those entities.
    """
    from app.api.reports import compute_profit_and_loss

    ctx = await authorize_workspace(
        workspace_id, min_role="admin", require_unscoped=True
    )
    return await compute_profit_and_loss(
        ctx, period, entity_id=None, restrict_entity_ids=entity_ids
    )
