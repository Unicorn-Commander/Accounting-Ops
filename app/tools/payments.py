from decimal import Decimal
from typing import Any
from uuid import UUID


async def propose_payment(tenant_id: UUID, amount: Decimal, recipient: dict[str, Any], reasoning: str) -> dict:
    """Prepare a non-custodial Stripe ACP payment proposal for human approval (§8.1, §11.1)."""
    raise NotImplementedError("Phase 1")
