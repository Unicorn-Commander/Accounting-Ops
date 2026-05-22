from app.config import get_settings


def accounting_ops_agent_card() -> dict:
    """A2A Agent Card. Published at /.well-known/agent.json + /a2a/card.

    External agents discover Accounting-Ops via this card. We declare our
    skills, auth (OAuth 2.1 + OIDC via Keycloak uchub), and capabilities.
    Per-workspace + per-entity scoping happens at request time via the
    URL path or headers.
    """
    settings = get_settings()
    return {
        "name": "Accounting-Ops",
        "version": "0.1.0",
        "description": (
            "Agent-first financial operations system of record and policy engine. "
            "Per-tenant policies, immutable audit-defensible journal, multi-runtime "
            "(Claude / Gemini / on-prem Qwen). Other agents call in over MCP + A2A; "
            "our agents call out over A2A."
        ),
        "url": f"{settings.public_base_url}/a2a",
        "capabilities": {
            "streaming": True,
            "pushNotifications": True,
        },
        "auth": {
            "schemes": [
                {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": f"{settings.keycloak_realm_url}/protocol/openid-connect/auth",
                            "tokenUrl": f"{settings.keycloak_realm_url}/protocol/openid-connect/token",
                            "scopes": {
                                "accounting:read": "Read tenant accounting records",
                                "accounting:write": "Propose tenant accounting actions",
                                "accounting:admin": "Manage workspace memberships and rules",
                            },
                        }
                    },
                }
            ]
        },
        "skills": [
            {
                "id": "bookkeeping",
                "name": "Bookkeeping",
                "description": "Categorization, journal-entry drafting, learn-from-correction loop. Per-entity within workspace.",
            },
            {
                "id": "reconciliation",
                "name": "Reconciliation",
                "description": "Deposit / receipt / source-event matching across bank, payment processor, and ecosystem-app feeds.",
            },
            {
                "id": "tax-reporting",
                "name": "Tax and reporting",
                "description": "P&L, runway, quarterly rollups, CPA handoff bundles (QBO IIF, Xero CSV, plain-text journal, working papers).",
            },
            {
                "id": "specialty-compliance",
                "name": "Specialty compliance",
                "description": "Per-entity rules engine: FERS 80%, 1099-NEC, multi-state sales tax nexus, HIPAA segregation, IOLTA trust, CAS, QSBS holding-period.",
            },
            {
                "id": "consolidation",
                "name": "Multi-entity consolidation",
                "description": "Cross-entity consolidated views within a workspace. Owner/admin only.",
            },
            {
                "id": "payments",
                "name": "Payment proposals",
                "description": "Non-custodial Stripe ACP payment proposals with materiality-gated human approval. Never auto-executes.",
            },
        ],
        "addressing": {
            "resource_pattern": "/api/workspaces/{workspace_id}/entities/{entity_id}/...",
            "consolidated_pattern": "/api/workspaces/{workspace_id}/consolidated/...",
        },
        "isolation_tiers": ["shared", "premium", "enterprise"],
    }
