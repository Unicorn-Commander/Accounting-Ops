from uuid import UUID


async def categorize_transaction(tenant_id: UUID, id: UUID, category: str, reasoning: str) -> dict:
    """Categorize a transaction, queueing for human approval when materiality policy requires it (§4.3)."""
    raise NotImplementedError("Phase 1")
