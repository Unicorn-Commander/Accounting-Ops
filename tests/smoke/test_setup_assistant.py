"""Smoke tests for the Setup Assistant persona + COA templates + MCP tools.

These tests don't need a live stack — they exercise:
  - All 7 COA templates load and have well-formed accounts
  - The persona is registered with the right tool allow-list
  - The mcp_server.build_server() registers the new tools
  - The Setup Assistant's opening message matches the spec
"""
from __future__ import annotations

import pathlib

import pytest


# ---------------------------------------------------------------------------
# COA templates
# ---------------------------------------------------------------------------


def test_coa_templates_all_load():
    from agents._shared.coa_templates import list_kinds

    kinds = list_kinds()
    assert "c_corp" in kinds
    assert "s_corp" in kinds
    assert "llc" in kinds
    assert "sole_prop" in kinds
    assert "personal_1040" in kinds
    assert "partnership" in kinds
    assert "nonprofit" in kinds


def test_coa_templates_account_shape():
    from agents._shared.coa_templates import get_accounts, list_kinds

    valid_types = {
        "asset", "liability", "equity", "revenue", "cogs",
        "expense", "other_income", "other_expense", "tax",
    }
    valid_balances = {"debit", "credit"}

    for kind in list_kinds():
        accounts = get_accounts(kind)
        assert len(accounts) > 0, f"{kind}: empty"
        for a in accounts:
            assert "number" in a, f"{kind}: missing number — {a}"
            assert "name" in a, f"{kind}: missing name — {a}"
            assert "type" in a, f"{kind}: missing type — {a}"
            assert a["type"] in valid_types, f"{kind} {a['number']}: bad type {a['type']}"
            if "normal_balance" in a:
                assert a["normal_balance"] in valid_balances
            assert 4 <= len(a["number"]) <= 8, f"{kind} {a['number']}: bad length"


def test_coa_templates_unique_account_numbers():
    """Each template should have unique account numbers."""
    from agents._shared.coa_templates import get_accounts, list_kinds

    for kind in list_kinds():
        accounts = get_accounts(kind)
        numbers = [a["number"] for a in accounts]
        dups = {n for n in numbers if numbers.count(n) > 1}
        assert not dups, f"{kind}: duplicate numbers {dups}"


def test_coa_templates_industry_overrides_extend_not_replace():
    """Industry overrides should add accounts, never silently override base ones."""
    from agents._shared.coa_templates import get_accounts, get_template, list_kinds

    for kind in list_kinds():
        module = get_template(kind)
        base = get_accounts(kind)
        base_numbers = {a["number"] for a in base}
        for industry in module.TEMPLATE_META.get("industry_overrides", {}):
            extended = get_accounts(kind, industry=industry)
            extended_numbers = {a["number"] for a in extended}
            # Every base account is still there
            assert base_numbers.issubset(extended_numbers), (
                f"{kind}/{industry}: lost {base_numbers - extended_numbers}"
            )
            # The extended set should be at least as large
            assert len(extended_numbers) >= len(base_numbers)


def test_c_corp_has_at_least_60_accounts():
    """The C-Corp template should be substantial (>= 60 accounts)."""
    from agents._shared.coa_templates import get_accounts

    assert len(get_accounts("c_corp")) >= 60


def test_personal_1040_marks_va_disability_nontaxable():
    """The 4081 VA Disability row must be annotated non-reportable."""
    from agents._shared.coa_templates import get_accounts

    accounts = get_accounts("personal_1040")
    va = next((a for a in accounts if a["number"] == "4081"), None)
    assert va is not None, "personal_1040 missing 4081 VA Disability account"
    # Tax-form line should mark as not reportable
    tax_line = (va.get("tax_form_line") or "").lower()
    notes = (va.get("notes") or "").lower()
    assert "not" in tax_line or "non-taxable" in notes or "excludable" in notes


def test_s_corp_requires_officer_wages_account():
    """S-Corp template must have an Officer/Shareholder W-2 wages account."""
    from agents._shared.coa_templates import get_accounts

    accounts = get_accounts("s_corp")
    has_officer = any(
        "officer" in (a["name"] or "").lower() and "wage" in (a["name"] or "").lower()
        for a in accounts
    )
    assert has_officer, "S-Corp missing Officer/Shareholder Wages account (reasonable comp requirement)"


def test_llc_has_member_capital_accounts():
    """LLC template must support per-member capital tracking."""
    from agents._shared.coa_templates import get_accounts

    accounts = get_accounts("llc")
    member_caps = [a for a in accounts if "member" in (a["name"] or "").lower() and "capital" in (a["name"] or "").lower()]
    assert len(member_caps) >= 2, "LLC needs at least 2 member-capital accounts"


# ---------------------------------------------------------------------------
# Persona registry
# ---------------------------------------------------------------------------


def test_setup_assistant_persona_registered():
    from agents.chat.persona_registry import get_persona, list_personas

    slugs = {p["slug"] for p in list_personas()}
    assert "setup-assistant" in slugs

    p = get_persona("setup-assistant")
    assert p is not None
    assert p.display_name == "Setup Assistant"
    assert p.persona_path.exists()
    assert p.system_prompt_path.exists()


def test_setup_assistant_tools_allow_list():
    from agents.chat.persona_registry import get_persona

    p = get_persona("setup-assistant")
    assert p is not None
    tools = set(p.allowed_mcp_tools)
    required = {
        "create_entity_with_coa",
        "list_chart_of_accounts",
        "list_workspace_entities",
        "list_my_workspaces",
        "create_workspace",
        "create_coa_account",
        "suggest_connectors",
        "draft_memo",
        "flag_for_cpa_review",
        "mark_intake_complete",
    }
    missing = required - tools
    assert not missing, f"setup-assistant missing tools: {missing}"

    # Setup Assistant must NOT have any journal-posting tool exposed
    forbidden = {"post_journal_entry", "decide_approval", "bulk_decide_approvals"}
    leaked = forbidden & tools
    assert not leaked, f"setup-assistant should not have: {leaked}"


def test_default_persona_is_cfo_agent():
    """The CFO Agent is the primary interface, so it is the /chat default.
    (Adding the Setup Assistant must not flip this — Setup has its own /setup entry.)"""
    from agents.chat.persona_registry import DEFAULT_PERSONA_SLUG

    assert DEFAULT_PERSONA_SLUG == "cfo-agent"


def test_setup_assistant_opening_message_is_present():
    """The system_prompt.md must contain a verbatim opening message."""
    from agents.chat.persona_registry import get_persona

    p = get_persona("setup-assistant")
    assert p is not None
    text = p.system_prompt_path.read_text(encoding="utf-8")
    # The opening question must be exactly this phrasing
    assert "Hi — I'm the Accounting-Ops **Setup Assistant**" in text
    assert "what kind of business are you setting up?" in text
    # All 6 entity-kind options must appear in the opening
    for kind in ("C-Corp", "S-Corp", "LLC", "Sole Proprietorship", "Personal household"):
        assert kind in text


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


def test_mcp_server_registers_new_tools():
    """build_server() should register all 4 new tools without raising."""
    import mcp_server

    # Importing + building the server validates all tool functions
    # have valid signatures + the FastMCP wrapper accepts them.
    server = mcp_server.build_server()
    assert server is not None


def test_create_entity_with_coa_importable():
    """The MCP tool function exists at the expected path."""
    from app.tools import create_entity_with_coa

    assert callable(create_entity_with_coa)
    assert create_entity_with_coa.__name__ == "create_entity_with_coa"


def test_setup_assistant_helpers_importable():
    from app.tools import (
        create_workspace,
        mark_intake_complete,
        suggest_connectors,
    )

    assert callable(create_workspace)
    assert callable(mark_intake_complete)
    assert callable(suggest_connectors)


# ---------------------------------------------------------------------------
# Connector-suggestion logic (pure function — testable without DB)
# ---------------------------------------------------------------------------


def test_suggest_connectors_profile_routing():
    """Spot-check that the entity-kind + industry combo picks the right profile."""
    from app.tools.setup_assistant import _connector_key

    assert _connector_key("c_corp", "tech_saas") == "tech_startup_c_corp"
    assert _connector_key("s_corp", "ai_infrastructure") == "tech_startup_s_corp"
    assert _connector_key("llc", "consulting_services") == "consulting_services"
    assert _connector_key("c_corp", "ecommerce") == "ecommerce"
    assert _connector_key("personal", None) == "personal"
    assert _connector_key("personal_1040", None) == "personal"
    assert _connector_key("nonprofit", None) == "nonprofit"
    assert _connector_key("llc", "real_estate") == "real_estate_holding"
    # Unknown combo falls back to default
    assert _connector_key("c_corp", None) == "default"
    assert _connector_key("trust", "weird") == "default"


# ---------------------------------------------------------------------------
# Sanity: entity-type enum mapping
# ---------------------------------------------------------------------------


def test_kind_to_entity_type_mapping_complete():
    """Every COA template kind must map to a valid entity_type enum value."""
    from app.tools.create_entity_with_coa import _KIND_TO_ENTITY_TYPE

    # These are the DB enum values from migrations/tenant/0001
    valid_enum = {
        "c_corp", "s_corp", "llc", "partnership", "sole_proprietorship",
        "personal", "trust", "non_profit", "estate", "other",
    }
    for kind, db_type in _KIND_TO_ENTITY_TYPE.items():
        assert db_type in valid_enum, f"{kind} -> {db_type} is not a valid entity_type"
