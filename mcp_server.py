import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.tools import (
    add_specialty_rule,
    analyze_pending_approvals,
    bulk_decide_approvals,
    categorize_transaction,
    close_period,
    create_category_override,
    create_coa_account,
    create_entity_with_coa,
    create_invoice_from_draft,
    create_workspace,
    decide_approval,
    draft_memo,
    finish_specialist_run,
    flag_for_cpa_review,
    get_1099_nec_report,
    get_1099_payees,
    get_balance_sheet,
    get_cash_flow,
    get_cash_position,
    get_consolidated_p_and_l,
    get_current_rule,
    get_form_1120_data,
    get_invoice,
    get_journal_entry,
    get_p_and_l,
    get_period_journal,
    get_runway,
    get_specialist_run,
    get_tax_knowledge_doc,
    get_tax_narrative,
    get_tax_review_queue,
    get_tax_status,
    get_transaction,
    get_trial_balance,
    link_receipt_to_transaction,
    list_account_mappings,
    list_approval_recommendations,
    list_category_overrides,
    list_chart_of_accounts,
    list_closed_periods,
    list_credentials,
    list_journal_entries,
    list_my_workspaces,
    list_pending_approvals,
    list_posting_log,
    list_recent_transactions,
    list_specialist_runs,
    list_tax_knowledge_topics,
    list_transactions,
    list_workspace_entities,
    mark_intake_complete,
    post_journal_entry,
    propose_payment,
    suggest_connectors,
    query_specialty_rules,
    record_payment,
    resolve_tax_review_item,
    search_tax_knowledge,
    start_specialist_run,
    trigger_tax_narrative,
    update_coa_account,
    upsert_account_mappings,
    upsert_ar_customer,
    verify_journal_chain,
    void_invoice,
)


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
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
        # /mcp is served behind Traefik (TLS) + MCPAuthMiddleware (Bearer token).
        # The MCP SDK's DNS-rebinding Host guard otherwise 421s the public FQDN
        # (accounting-ops.magicunicorn.dev); disable it — the Bearer token and the
        # reverse proxy are the real controls here, not a localhost Host check.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        instructions=(
            "Agent-first financial operations for Magic Unicorn. Tools operate on "
            "a workspace_id (+ entity_id for entity-scoped tools). The caller's "
            "OAuth2 bearer (uchub realm) is authorized against workspace "
            "membership on every call — you can only reach workspaces you belong "
            "to. Reads operate on the posted append-only journal. Discovery: "
            "use list_my_workspaces + list_workspace_entities to find what you "
            "can reach before calling entity-scoped tools."
        ),
    )

    # =====================================================================
    # Discovery (workspace + entity enumeration the caller can reach)
    # =====================================================================
    server.tool()(list_my_workspaces)
    server.tool()(list_workspace_entities)

    # =====================================================================
    # Approval queue
    # =====================================================================
    server.tool()(analyze_pending_approvals)        # writes recommendations only
    server.tool()(list_pending_approvals)
    server.tool()(list_approval_recommendations)
    server.tool()(decide_approval)
    server.tool()(bulk_decide_approvals)

    # =====================================================================
    # Entity-scoped reads (transactions / specialty rules)
    # =====================================================================
    server.tool()(list_transactions)
    server.tool()(get_transaction)
    server.tool()(list_recent_transactions)
    server.tool()(get_cash_position)
    server.tool()(query_specialty_rules)

    # =====================================================================
    # Entity-scoped writes (categorize / link receipt / propose rule)
    # =====================================================================
    server.tool()(categorize_transaction)
    server.tool()(link_receipt_to_transaction)
    server.tool()(add_specialty_rule)

    # =====================================================================
    # Reports (entity or consolidated)
    # =====================================================================
    server.tool()(get_p_and_l)
    server.tool()(get_runway)
    server.tool()(get_consolidated_p_and_l)
    server.tool()(get_balance_sheet)
    server.tool()(get_cash_flow)
    server.tool()(get_trial_balance)
    server.tool()(get_1099_nec_report)

    # =====================================================================
    # Chart of accounts (entity-scoped)
    # =====================================================================
    server.tool()(list_chart_of_accounts)
    server.tool()(create_coa_account)
    server.tool()(update_coa_account)

    # =====================================================================
    # Setup Assistant — conversational entity + workspace provisioning
    # =====================================================================
    server.tool()(create_entity_with_coa)
    server.tool()(create_workspace)
    server.tool()(suggest_connectors)
    server.tool()(mark_intake_complete)

    # =====================================================================
    # Journal + period close
    # =====================================================================
    server.tool()(list_journal_entries)
    server.tool()(get_journal_entry)
    server.tool()(verify_journal_chain)
    server.tool()(list_posting_log)
    server.tool()(post_journal_entry)
    server.tool()(list_closed_periods)
    server.tool()(close_period)

    # =====================================================================
    # AR / Invoicing (native + cross-app-gated)
    # =====================================================================
    server.tool()(upsert_ar_customer)
    server.tool()(create_invoice_from_draft)
    server.tool()(get_invoice)
    server.tool()(void_invoice)
    server.tool()(record_payment)

    # =====================================================================
    # Connectors / mappings / overrides / credentials
    # =====================================================================
    server.tool()(list_credentials)                 # metadata only
    server.tool()(list_account_mappings)
    server.tool()(upsert_account_mappings)
    server.tool()(list_category_overrides)
    server.tool()(create_category_override)

    # =====================================================================
    # Tax Knowledge RAG (global, no workspace scoping)
    # =====================================================================
    server.tool()(get_current_rule)
    server.tool()(list_tax_knowledge_topics)
    server.tool()(search_tax_knowledge)
    server.tool()(get_tax_knowledge_doc)

    # =====================================================================
    # 1120 Preparer + tax review queue
    # =====================================================================
    server.tool()(get_tax_review_queue)
    server.tool()(resolve_tax_review_item)
    server.tool()(get_form_1120_data)
    server.tool()(get_period_journal)
    server.tool()(get_1099_payees)
    server.tool()(draft_memo)
    server.tool()(flag_for_cpa_review)

    # =====================================================================
    # CPA Narrative + tax status dashboard
    # =====================================================================
    server.tool()(get_tax_narrative)
    server.tool()(trigger_tax_narrative)
    server.tool()(get_tax_status)

    # =====================================================================
    # Specialist run observability
    # =====================================================================
    server.tool()(list_specialist_runs)
    server.tool()(get_specialist_run)
    server.tool()(start_specialist_run)
    server.tool()(finish_specialist_run)

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
