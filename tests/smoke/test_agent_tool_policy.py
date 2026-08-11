"""Server-side per-agent tool allow-list.

Two things are under test, and the second is the scary one:

  1. The policy semantics themselves (who may call what, and that a malformed
     policy fails loudly instead of silently disabling the control).
  2. That wrapping ``server.tool()`` to install the policy did not corrupt the
     JSON schema FastMCP derives from each tool's signature. A wrapper that
     loses ``__annotations__`` would degrade all ~73 tools to (*args,
     **kwargs) — every tool still "works" from the server's point of view, but
     every client loses its parameter contract. That is exactly the kind of
     silent, wide-blast-radius degradation this codebase has been bitten by
     before, so it gets pinned against an unwrapped build.
"""
from __future__ import annotations

import json

import pytest

from app import agent_tool_policy
from app.agent_tool_policy import (
    ENV_VAR,
    enforce_tool_policy,
    is_tool_allowed,
    reset_policy_cache,
)


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    reset_policy_cache()
    yield
    reset_policy_cache()


def _set_policy(monkeypatch, policy: dict | str) -> None:
    raw = policy if isinstance(policy, str) else json.dumps(policy)
    monkeypatch.setenv(ENV_VAR, raw)
    reset_policy_cache()


# ---------------------------------------------------------------------------
# Semantics
# ---------------------------------------------------------------------------


def test_no_policy_configured_restricts_nobody(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    reset_policy_cache()
    assert is_tool_allowed("receipt-reconciler", "post_journal_entry") is True
    assert is_tool_allowed(None, "post_journal_entry") is True


def test_human_callers_are_never_restricted(monkeypatch):
    # A human's token carries no agent_id; the role floor is their gate.
    _set_policy(monkeypatch, {"receipt-reconciler": []})
    assert is_tool_allowed(None, "post_journal_entry") is True
    assert is_tool_allowed("", "post_journal_entry") is True


def test_unenrolled_agent_is_not_restricted(monkeypatch):
    # Enrolment is opt-in, so shipping the feature cannot break an agent that
    # nobody has characterised yet.
    _set_policy(monkeypatch, {"receipt-reconciler": ["list_transactions"]})
    assert is_tool_allowed("listing-ops-ao-sync", "post_journal_entry") is True


def test_unenrolled_agent_usage_is_logged_for_enrolment(monkeypatch, caplog):
    # decision_audit only records write tools, so it cannot tell us an agent's
    # read surface. The un-enrolled path is the discovery path instead.
    _set_policy(monkeypatch, {"someone-else": []})
    with caplog.at_level("INFO"):
        assert is_tool_allowed("receipt-reconciler", "list_transactions") is True
        assert is_tool_allowed("receipt-reconciler", "list_transactions") is True
        assert is_tool_allowed("receipt-reconciler", "get_transaction") is True
    observed = [ln for ln in caplog.text.splitlines() if "agent_tool_policy_observed" in ln]
    assert len(observed) == 2, "should log once per (agent, tool), not per call"
    assert any("list_transactions" in ln for ln in observed)
    assert any("get_transaction" in ln for ln in observed)


def test_enrolled_agents_are_not_observation_logged(monkeypatch, caplog):
    _set_policy(monkeypatch, {"receipt-reconciler": ["list_transactions"]})
    with caplog.at_level("INFO"):
        is_tool_allowed("receipt-reconciler", "list_transactions")
    assert "agent_tool_policy_observed" not in caplog.text


def test_enrolled_agent_is_confined_to_its_list(monkeypatch):
    _set_policy(monkeypatch, {"receipt-reconciler": ["link_receipt_to_transaction"]})
    assert is_tool_allowed("receipt-reconciler", "link_receipt_to_transaction") is True
    assert is_tool_allowed("receipt-reconciler", "post_journal_entry") is False
    assert is_tool_allowed("receipt-reconciler", "close_period") is False


def test_empty_list_denies_everything(monkeypatch):
    _set_policy(monkeypatch, {"quarantined-agent": []})
    assert is_tool_allowed("quarantined-agent", "list_transactions") is False


def test_wildcard_allows_everything(monkeypatch):
    _set_policy(monkeypatch, {"trusted-agent": ["*"]})
    assert is_tool_allowed("trusted-agent", "close_period") is True


def test_enforce_raises_with_an_actionable_message(monkeypatch):
    _set_policy(monkeypatch, {"receipt-reconciler": ["list_transactions"]})
    enforce_tool_policy("receipt-reconciler", "list_transactions")  # allowed: no raise
    with pytest.raises(PermissionError, match="not permitted to call"):
        enforce_tool_policy("receipt-reconciler", "post_journal_entry")


def test_denial_is_logged_with_agent_and_tool_but_never_a_token(monkeypatch, caplog):
    _set_policy(monkeypatch, {"receipt-reconciler": []})
    with caplog.at_level("WARNING"):
        with pytest.raises(PermissionError):
            enforce_tool_policy("receipt-reconciler", "post_journal_entry")
    logged = caplog.text
    assert "agent_tool_policy_denied" in logged
    assert "receipt-reconciler" in logged and "post_journal_entry" in logged


@pytest.mark.parametrize(
    "bad",
    ["{not json", '["a","list"]', '{"agent": "not-a-list"}', '{"agent": [1, 2]}'],
)
def test_malformed_policy_fails_loudly(monkeypatch, bad):
    # A security control that silently disables itself on a typo is worse than
    # no control: it still looks enabled.
    _set_policy(monkeypatch, bad)
    with pytest.raises(ValueError, match=ENV_VAR):
        is_tool_allowed("receipt-reconciler", "post_journal_entry")


# ---------------------------------------------------------------------------
# The wrapper must not disturb the tool contract
# ---------------------------------------------------------------------------


def _schemas_for(install_policy: bool) -> dict[str, dict]:
    """Build a server with/without the policy wrapper; return {tool: schema}."""
    pytest.importorskip("mcp.server.fastmcp")
    import mcp_server

    real_install = mcp_server._install_agent_tool_policy
    try:
        if not install_policy:
            mcp_server._install_agent_tool_policy = lambda server: None
        server = mcp_server.build_server()
    finally:
        mcp_server._install_agent_tool_policy = real_install

    import asyncio

    tools = asyncio.run(server.list_tools())
    return {t.name: t.inputSchema for t in tools}


def test_policy_wrapper_preserves_every_tool_schema():
    wrapped = _schemas_for(install_policy=True)
    plain = _schemas_for(install_policy=False)

    assert set(wrapped) == set(plain), (
        "policy wrapper changed the set of registered tools: "
        f"missing={set(plain) - set(wrapped)} extra={set(wrapped) - set(plain)}"
    )
    assert len(wrapped) > 50, f"expected the full tool surface, got {len(wrapped)}"

    degraded = [
        name
        for name, schema in wrapped.items()
        if schema != plain[name]
    ]
    assert not degraded, (
        "policy wrapper altered the input schema of these tools (a lost "
        f"__annotations__ would do exactly this): {degraded[:10]}"
    )


def test_denied_call_is_refused_before_the_tool_body_runs(monkeypatch):
    """The whole point: refusal happens at dispatch, not inside the tool.

    ``post_journal_entry`` would authorize and hit the database. With a denying
    policy the call must fail with PermissionError having touched neither — so
    a confined agent cannot even reach the authorization layer, let alone the
    ledger.
    """
    pytest.importorskip("mcp.server.fastmcp")
    import asyncio

    import mcp_server

    _set_policy(monkeypatch, {"receipt-reconciler": ["list_transactions"]})

    class _Auth:
        agent_id = "receipt-reconciler"

    monkeypatch.setattr(mcp_server, "current_auth", lambda: _Auth())

    # Fail the test loudly if authorization is ever reached.
    import app.tools.journal_tools as jt

    async def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("authorize_workspace reached despite policy denial")

    monkeypatch.setattr(jt, "authorize_workspace", _boom)

    server = mcp_server.build_server()
    with pytest.raises(Exception) as exc:
        asyncio.run(
            server.call_tool(
                "post_journal_entry",
                {
                    "workspace_id": "00000000-0000-0000-0000-000000000001",
                    "entity_id": "00000000-0000-0000-0000-000000000002",
                    "source": "manual",
                    "source_external_id": "x",
                    "effective_date": "2026-08-11",
                    "amount_cents": -100,
                    "description": "should never post",
                    "category": "SaaS · AI",
                },
            )
        )
    assert "not permitted to call" in str(exc.value)


def test_wrapped_tools_keep_their_docstrings():
    # FastMCP surfaces the docstring as the tool description; losing it would
    # strip every tool's usage guidance from the client.
    pytest.importorskip("mcp.server.fastmcp")
    import asyncio

    import mcp_server

    server = mcp_server.build_server()
    tools = asyncio.run(server.list_tools())
    undocumented = [t.name for t in tools if not (t.description or "").strip()]
    assert not undocumented, f"tools lost their descriptions: {undocumented[:10]}"
