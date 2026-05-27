def test_app_imports():
    from app.main import app

    assert app.title == "Accounting-Ops"


def test_tool_stubs_raise_phase_1():
    import uuid
    import pytest

    from app.tools.transactions import get_transaction

    with pytest.raises(NotImplementedError, match="Phase 1"):
        import asyncio

        # get_transaction(workspace_id, entity_id, id) — all three are required.
        asyncio.run(get_transaction(uuid.uuid4(), uuid.uuid4(), uuid.uuid4()))
