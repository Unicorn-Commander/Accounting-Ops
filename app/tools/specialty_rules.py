from typing import Any
from uuid import UUID


async def query_specialty_rules(
    workspace_id: UUID,
    entity_id: UUID,
) -> list[dict[str, Any]]:
    """Return active compliance + specialty rules for this entity.

    Filters by temporal validity (effective_from <= now < effective_to or
    effective_to IS NULL). Rules cross-reference the FalkorDB ComplianceRule
    nodes for richer relationship context.
    """
    raise NotImplementedError("Phase 1")


async def add_specialty_rule(
    workspace_id: UUID,
    entity_id: UUID,
    kind: str,
    params: dict[str, Any],
    reasoning: str,
) -> dict:
    """Propose a specialty rule for this entity.

    Every rule addition queues for human approval before activation.
    The agent's reasoning is captured for the audit trail.
    """
    raise NotImplementedError("Phase 1")
