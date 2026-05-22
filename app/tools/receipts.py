from uuid import UUID


async def link_receipt_to_transaction(
    workspace_id: UUID,
    entity_id: UUID,
    receipt_id: UUID,
    tx_id: UUID,
) -> dict:
    """Link a content-hash receipt to an entity-scoped transaction.

    Closes the IRS purchase/payment/purpose triangle. The receipt and
    transaction MUST belong to the same entity_id, or this raises 422.
    """
    raise NotImplementedError("Phase 1")
