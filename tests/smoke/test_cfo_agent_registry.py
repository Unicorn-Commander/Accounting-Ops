"""CFO Agent + persona registry — smoke tests (no live stack).

These tests verify:

1. The persona registry imports cleanly and contains the CFO Agent.
2. The CFO Agent's allow-list matches the brief (read + queue-draft +
   the 10 synthesis aliases).
3. The CFO Agent CANNOT call operational mutation tools.
4. CFO_AGENT_MODEL env var overrides the default Opus model.
5. The system_prompt.md + persona.md files load + contain the expected
   guardrail strings.
6. The Brigade client default state is offline / empty / safe.
7. The brigade_federation.md doc describes both producer + consumer.
8. The cross_app_strategy.md doc names the CFO Agent.
"""
from __future__ import annotations

import importlib
import os

import pytest


def _reload_registry():
    # The default_model resolution reads env at module load. To exercise
    # the env override path, we need to reload the module after mutating
    # os.environ.
    import agents.chat.persona_registry as mod

    return importlib.reload(mod)


def test_persona_registry_imports():
    from agents.chat.persona_registry import PERSONAS

    assert len(PERSONAS) >= 11, "Expected at least 11 personas"


def test_cfo_agent_in_registry():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("cfo-agent")
    assert spec is not None, "cfo-agent must be registered"
    assert spec.display_name == "CFO Agent"
    assert spec.tier == "strategic"
    assert spec.needs_tax_year is True
    assert spec.consumes_brigade is True


def test_cfo_agent_tool_allow_list_includes_all_brief_aliases():
    """The brief lists exactly these 10 aliases on top of read + draft."""
    from agents.chat.persona_registry import get_persona

    spec = get_persona("cfo-agent")
    assert spec is not None
    required = {
        "get_balance_sheet",
        "get_cash_flow",
        "get_trial_balance",
        "get_tax_status",
        "get_cash_position",
        "get_runway",
        "list_specialist_runs",
        "search_tax_knowledge",
        "list_pending_approvals",
        "get_tax_narrative",
    }
    missing = required - set(spec.allowed_mcp_tools)
    assert not missing, f"CFO Agent missing brief-mandated aliases: {missing}"


def test_cfo_agent_includes_read_and_draft_tools():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("cfo-agent")
    assert spec is not None
    tools = set(spec.allowed_mcp_tools)
    # Read tools
    assert "get_p_and_l" in tools
    assert "get_consolidated_p_and_l" in tools
    assert "get_runway" in tools
    assert "get_form_1120_data" in tools
    # Draft tools (queue, never auto-file)
    assert "draft_memo" in tools
    assert "flag_for_cpa_review" in tools


def test_cfo_agent_excludes_operational_mutation_tools():
    """Brief: CFO Agent uses tools to synthesize, not to do tactical work."""
    from agents.chat.persona_registry import get_persona

    spec = get_persona("cfo-agent")
    assert spec is not None
    forbidden = {
        "categorize_transaction",
        "propose_payment_tool",
        "link_receipt_to_transaction",
        "add_specialty_rule",
        "create_workspace",
        "create_entity_with_coa",
    }
    overlap = forbidden & set(spec.allowed_mcp_tools)
    assert not overlap, f"CFO Agent must NOT have these operational tools: {overlap}"


def test_cfo_agent_default_model_is_opus():
    os.environ.pop("CFO_AGENT_MODEL", None)
    mod = _reload_registry()
    spec = mod.get_persona("cfo-agent")
    assert spec is not None
    assert spec.default_model == "claude-opus-4-7"


def test_cfo_agent_model_env_override():
    os.environ["CFO_AGENT_MODEL"] = "claude-sonnet-4-6"
    try:
        mod = _reload_registry()
        spec = mod.get_persona("cfo-agent")
        assert spec is not None
        assert spec.default_model == "claude-sonnet-4-6"
    finally:
        os.environ.pop("CFO_AGENT_MODEL", None)
        _reload_registry()  # restore default


def test_cfo_persona_md_exists_and_has_guardrail_phrases():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("cfo-agent")
    assert spec is not None
    text = spec.persona_path.read_text(encoding="utf-8")
    # Brief: "I'll surface this to your CPA / attorney / board" when outside scope
    assert "surface this to your" in text
    # Brief: NOT focused on bookkeeping or filing minutiae
    assert "NOT" in text
    # Brigade producer + consumer
    assert "PRODUCER" in text or "Producer" in text
    assert "CONSUMER" in text or "Consumer" in text


def test_cfo_system_prompt_loads():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("cfo-agent")
    assert spec is not None
    # Use the persona's system_prompt() builder — it prepends the chat-mode
    # preamble + injects context. Pass placeholders.
    sp = spec.system_prompt(
        workspace_id="00000000-0000-0000-0000-000000000001",
        entity_id="00000000-0000-0000-0000-000000000002",
        tax_year=2026,
        user_uc_uid="aaron",
    )
    assert len(sp) > 500, "system_prompt should be non-trivial"
    assert "CFO Agent" in sp
    # Guardrails the LLM will see
    assert "Mandatory Surface-To-Human List" in sp
    # Preamble injection — the chat runner prepends this on top of the persona's prompt.
    assert "INTERACTIVE CHAT MODE" in sp


def test_persona_tier_distribution():
    """Registry must include all three tiers per the brief."""
    from agents.chat.persona_registry import PERSONAS

    tiers = {p.tier for p in PERSONAS}
    assert tiers == {"strategic", "specialist", "operational"}


def test_list_personas_includes_brigade_status():
    """list_personas(brigade_online=True) must mark consume_brigade personas."""
    from agents.chat.persona_registry import list_personas

    rows = list_personas(brigade_online=True)
    cfo = next((r for r in rows if r["slug"] == "cfo-agent"), None)
    assert cfo is not None
    assert cfo["tier"] == "strategic"
    assert cfo["consumes_brigade"] is True
    assert cfo["brigade_active"] is True
    assert cfo["default_model"] == "claude-opus-4-7"

    # When Brigade is offline, brigade_active is False.
    rows_off = list_personas(brigade_online=False)
    cfo_off = next((r for r in rows_off if r["slug"] == "cfo-agent"), None)
    assert cfo_off is not None
    assert cfo_off["brigade_active"] is False


def test_brigade_client_starts_offline_when_env_unset():
    """No BRIGADE_BASE_URL → catalog snapshot must be online=False, tools=()."""
    os.environ.pop("BRIGADE_BASE_URL", None)
    os.environ.pop("BRIGADE_TRUSTED_ISSUER", None)
    from app.security import brigade_client

    snap = brigade_client.get_cross_app_catalog()
    # The init hasn't been called in this test path, but the default
    # snapshot must still be a valid offline-stub.
    assert snap.online is False
    assert snap.tools == ()
    # Either "not_initialized" or "feature_off" — both are safe.
    assert snap.reason == "not_initialized" or snap.reason.startswith(("feature_off", "fetch"))


def test_brigade_invoke_raises_when_offline():
    """Without Brigade live, invoke_cross_app_tool must raise BrigadeUnavailableError."""
    import asyncio

    os.environ.pop("BRIGADE_BASE_URL", None)
    os.environ.pop("BRIGADE_TRUSTED_ISSUER", None)
    from app.security.brigade_client import (
        BrigadeUnavailableError,
        invoke_cross_app_tool,
    )

    with pytest.raises(BrigadeUnavailableError):
        asyncio.run(
            invoke_cross_app_tool(
                "project.list_budgets",
                {"workspace_id": "x"},
                bearer_token="fake",
                scopes=("brigade:cross_app",),
            )
        )


def test_brigade_client_has_cross_app_scope_check():
    """The scope helper must recognize brigade:cross_app."""
    from app.security.brigade_client import has_cross_app_scope

    assert has_cross_app_scope(("brigade:cross_app",)) is True
    assert has_cross_app_scope(("brigade:cross_app", "accounting:read")) is True
    assert has_cross_app_scope(("accounting:read",)) is False
    assert has_cross_app_scope(()) is False


def test_cross_app_strategy_doc_exists():
    """The cross-app strategy doc must exist and name the CFO Agent."""
    from pathlib import Path

    doc = Path(__file__).resolve().parents[2] / "agents" / "_shared" / "cross_app_strategy.md"
    assert doc.exists(), "cross_app_strategy.md must exist"
    text = doc.read_text(encoding="utf-8")
    assert "CFO Agent" in text
    assert "brigade:cross_app" in text


def test_brigade_federation_doc_describes_both_sides():
    from pathlib import Path

    doc = Path(__file__).resolve().parents[2] / "agents" / "_shared" / "brigade_federation.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    # The brief: must describe BOTH producer AND consumer
    assert "PRODUCER" in text or "Producer" in text or "producer" in text
    assert "CONSUMER" in text or "Consumer" in text or "consumer" in text
    # Specifically the consumer section we added.
    assert "Consumer side" in text


def test_cfo_readme_exists():
    """The CFO Agent README must exist and describe scope + tools."""
    from pathlib import Path

    doc = Path(__file__).resolve().parents[2] / "agents" / "cpa_team" / "cfo_agent" / "README.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "CFO Agent" in text
    # Scope statements per the brief
    assert "what the CFO Agent does NOT do" in text or "does NOT do" in text
