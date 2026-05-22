import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.tools import (
    add_specialty_rule,
    categorize_transaction,
    get_consolidated_p_and_l,
    get_p_and_l,
    get_runway,
    get_transaction,
    link_receipt_to_transaction,
    list_transactions,
    propose_payment,
    query_specialty_rules,
)


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("Install the PyPI 'mcp' package to run the MCP server") from exc

    settings = get_settings()
    server = FastMCP("Accounting-Ops")

    # Entity-scoped tools — caller passes workspace_id + entity_id.
    server.tool()(list_transactions)
    server.tool()(get_transaction)
    server.tool()(categorize_transaction)
    server.tool()(link_receipt_to_transaction)
    server.tool()(query_specialty_rules)
    server.tool()(add_specialty_rule)
    server.tool()(get_p_and_l)
    server.tool()(get_runway)

    # Workspace-level consolidated view (cross-entity).
    server.tool()(get_consolidated_p_and_l)

    @server.tool()
    async def propose_payment_tool(
        workspace_id: UUID,
        entity_id: UUID,
        amount: str,
        recipient: dict[str, Any],
        reasoning: str,
    ) -> dict:
        """Wrap propose_payment so MCP-side accepts amount as a string and we cast to Decimal."""
        return await propose_payment(
            workspace_id, entity_id, Decimal(amount), recipient, reasoning
        )

    @server.resource("https://accounting-ops.magicunicorn.dev/.well-known/mcp.json")
    def well_known_mcp() -> dict:
        return {
            "name": "Accounting-Ops",
            "version": "0.1.0",
            "endpoint": f"{settings.public_base_url}/mcp",
            "auth": {
                "type": "oauth2",
                "issuer": str(settings.keycloak_realm_url),
                "token_url": f"{settings.keycloak_realm_url}/protocol/openid-connect/token",
            },
            "addressing": {
                "resource_pattern": "/api/workspaces/{workspace_id}/entities/{entity_id}/...",
            },
        }

    return server


async def main() -> None:
    server = build_server()
    await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
