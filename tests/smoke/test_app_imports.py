def test_app_imports():
    from app.main import app

    assert app.title == "Accounting-Ops"


def test_payment_tool_stays_phase_1():
    """propose_payment stays NotImplementedError until the approval flow is built
    and tested (hard guardrail). It must not silently start moving money."""
    import asyncio
    import uuid
    from decimal import Decimal

    import pytest

    from app.tools.payments import propose_payment

    with pytest.raises(NotImplementedError, match="Phase 1"):
        asyncio.run(
            propose_payment(uuid.uuid4(), uuid.uuid4(), Decimal("1.00"), {}, "x")
        )


def test_read_tool_enforces_auth_not_phase_1():
    """The read tools are now implemented (no longer Phase-1 stubs) and gate on
    the MCP auth context: called with NO bound caller, get_transaction must DENY
    (PermissionError) — not return data and not raise the old NotImplementedError."""
    import asyncio
    import uuid

    import pytest

    from app.tools.transactions import get_transaction

    with pytest.raises(PermissionError):
        asyncio.run(get_transaction(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()))
