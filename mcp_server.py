import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.tools import (
    add_specialty_rule,
    analyze_pending_approvals,
    categorize_transaction,
    draft_memo,
    flag_for_cpa_review,
    get_1099_payees,
    get_consolidated_p_and_l,
    get_form_1120_data,
    get_p_and_l,
    get_period_journal,
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
    # streamable_http_path="/" so that mounting the app at "/mcp" (app/main.py)
    # yields the endpoint exactly at "/mcp" — not "/mcp/mcp" (the default path is
    # "/mcp", which would double up under the mount). Matches the /.well-known
    # discovery doc and the Traefik /mcp route.
    server = FastMCP(
        "Accounting-Ops",
        streamable_http_path="/",
        instructions=(
            "Agent-first financial operations for Magic Unicorn. Tools operate on "
            "a workspace_id (+ entity_id for entity-scoped tools). The caller's "
            "OAuth2 bearer (uchub realm) is authorized against workspace "
            "membership on every call — you can only reach workspaces you belong "
            "to. Reads operate on the posted append-only journal."
        ),
    )

    # Approval batch pre-analyzer — writes recommendations, never approves.
    server.tool()(analyze_pending_approvals)

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

    # 1120 Preparer specialist tools
    server.tool()(get_form_1120_data)
    server.tool()(get_period_journal)
    server.tool()(get_1099_payees)
    server.tool()(draft_memo)
    server.tool()(flag_for_cpa_review)

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
