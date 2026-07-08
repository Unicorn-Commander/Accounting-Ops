from decimal import Decimal
from typing import Any
from uuid import UUID


async def propose_payment(
    workspace_id: UUID,
    entity_id: UUID,
    amount: Decimal,
    recipient: dict[str, Any],
    reasoning: str,
) -> dict:
    """Prepare a non-custodial Stripe ACP payment proposal for human approval.

    The entity_id determines which set of payment methods are usable
    (a payment from a business entity uses that entity's connected accounts;
    a payment from a personal entity uses personal accounts).

    Always queues for approval. Never auto-executes regardless of materiality.
    Custody stays with Stripe — we model AP2 mandate shape but don't move money.
    """
    raise NotImplementedError("Phase 1")
