from typing import Any
from uuid import UUID


async def query_specialty_rules(tenant_id: UUID) -> list[dict[str, Any]]:
    """Return active tenant compliance and specialty rules from the policy catalog (§6.2, §11.1)."""
    raise NotImplementedError("Phase 1")


async def add_specialty_rule(tenant_id: UUID, kind: str, params: dict[str, Any], reasoning: str) -> dict:
    """Propose a specialty rule; every addition queues for human approval before activation (§6.3)."""
    raise NotImplementedError("Phase 1")
