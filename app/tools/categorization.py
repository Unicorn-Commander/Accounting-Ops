from uuid import UUID


async def categorize_transaction(
    workspace_id: UUID,
    entity_id: UUID,
    id: UUID,
    category: str,
    reasoning: str,
) -> dict:
    """Categorize an entity-scoped transaction.

    Routes through the materiality gate: under-threshold auto-posts;
    over-threshold queues to approval_queue for human attestation.
    Always writes a decision_audit row.
    """
    raise NotImplementedError("Phase 1")
