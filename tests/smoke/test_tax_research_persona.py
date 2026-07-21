"""Tax-Research persona — registry + file smoke tests (no live stack).

Verifies the new conversational ``tax-research`` persona:

  1. Is registered, specialist tier, no tax-year binding.
  2. Exposes ONLY the read-only knowledge/RAG tools (no write/queue/mutation).
  3. persona.md + system_prompt.md load and carry the expected guardrails
     (citation-first, no-position, no-write, retrieve-don't-recall).
  4. Its system_prompt() builds with the chat-mode preamble prepended.
"""
from __future__ import annotations


def test_tax_research_in_registry():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("tax-research")
    assert spec is not None, "tax-research must be registered"
    assert spec.display_name == "Tax Research"
    assert spec.tier == "specialist"
    # The user names the year in their question; persona isn't entity-bound.
    assert spec.needs_tax_year is False
    # Not a Brigade cross-app consumer.
    assert spec.consumes_brigade is False


def test_tax_research_tools_are_knowledge_only():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("tax-research")
    assert spec is not None
    tools = set(spec.allowed_mcp_tools)
    # The four RAG/knowledge tools, exactly.
    assert tools == {
        "search_tax_knowledge",
        "list_tax_knowledge_topics",
        "get_tax_knowledge_doc",
        "get_current_rule",
    }


def test_tax_research_excludes_write_and_mutation_tools():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("tax-research")
    assert spec is not None
    forbidden = {
        "draft_memo",
        "flag_for_cpa_review",
        "categorize_transaction",
        "propose_payment",
        "resolve_tax_review_item",
        "add_specialty_rule",
        "create_entity_with_coa",
    }
    overlap = forbidden & set(spec.allowed_mcp_tools)
    assert not overlap, f"Tax Research must NOT have these tools: {overlap}"


def test_tax_research_persona_md_has_guardrails():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("tax-research")
    assert spec is not None
    text = spec.persona_path.read_text(encoding="utf-8")
    # Citation-first + no-position + no-write + retrieve-don't-recall.
    assert "Cite everything" in text or "citation" in text.lower()
    assert "take no tax position" in text.lower() or "no tax position" in text.lower() or "take a tax position" in text.lower()
    assert "no write tools" in text.lower()
    assert "Retrieve, don't recall" in text or "retrieve" in text.lower()
    # Hand-off to the CPA Tax Resolver.
    assert "CPA Tax Resolver" in text


def test_tax_research_system_prompt_builds_with_preamble():
    from agents.chat.persona_registry import get_persona

    spec = get_persona("tax-research")
    assert spec is not None
    sp = spec.system_prompt(
        workspace_id="00000000-0000-0000-0000-000000000001",
        entity_id="00000000-0000-0000-0000-000000000002",
        tax_year=None,  # needs_tax_year=False
        user_uc_uid="jane",
    )
    assert len(sp) > 500
    assert "Tax Research" in sp
    # Chat-mode preamble is prepended by the persona builder.
    assert "INTERACTIVE CHAT MODE" in sp
    # Core guardrail visible to the LLM.
    assert "search_tax_knowledge" in sp


def test_registry_still_has_all_three_tiers():
    from agents.chat.persona_registry import PERSONAS

    tiers = {p.tier for p in PERSONAS}
    assert tiers == {"strategic", "specialist", "operational"}
    # Adding tax-research grows the registry.
    assert len(PERSONAS) >= 12
