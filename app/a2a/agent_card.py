from app.config import get_settings


def accounting_ops_agent_card() -> dict:
    settings = get_settings()
    return {
        "name": "Accounting-Ops",
        "version": "0.1.0",
        "description": "Agent-first financial operations system of record and policy engine.",
        "url": f"{settings.public_base_url}/a2a",
        "capabilities": {"streaming": True, "pushNotifications": True},
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
                            },
                        }
                    },
                }
            ]
        },
        "skills": [
            {"id": "bookkeeping", "name": "Bookkeeping", "description": "Categorization and journal-entry support."},
            {"id": "reconciliation", "name": "Reconciliation", "description": "Deposit, receipt, and source-event matching."},
            {"id": "tax-reporting", "name": "Tax and reporting", "description": "P&L, runway, and CPA handoff preparation."},
            {"id": "specialty-compliance", "name": "Specialty compliance", "description": "Tenant-specific compliance rule monitoring."},
            {"id": "payments", "name": "Payment proposals", "description": "Non-custodial payment proposal workflow."},
        ],
    }
