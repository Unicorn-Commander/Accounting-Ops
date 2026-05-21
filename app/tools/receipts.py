from uuid import UUID


async def link_receipt_to_transaction(tenant_id: UUID, receipt_id: UUID, tx_id: UUID) -> dict:
    """Close the purchase/payment/purpose triangle by linking a content-hash receipt to a transaction (§7.1)."""
    raise NotImplementedError("Phase 1")
