"""Setup Assistant prompt loader: individual vs business from client_kind.

No database. Fixtures are fictional workspace ids and profile documents.
"""
from __future__ import annotations

from agents.chat.persona_registry import get_persona
from agents.cpa_team.setup_assistant import (
    client_kind_from_workspace_profile,
    resolve_client_kind,
    resolve_system_prompt,
)

_INDIVIDUAL_OPENING = "You're setting up an individual's books (1040)"
_BUSINESS_QUESTION = "what kind of business are you setting up?"
_PROMPT_IDS = dict(
    workspace_id="00000000-0000-0000-0000-000000000001",
    entity_id="00000000-0000-0000-0000-000000000002",
    tax_year=None,
    user_uc_uid="jane",
)


def _persona():
    p = get_persona("setup-assistant")
    assert p is not None
    return p


def test_profile_reader_accepts_dict_and_json():
    assert client_kind_from_workspace_profile(None) is None
    assert client_kind_from_workspace_profile({}) is None
    assert client_kind_from_workspace_profile({"version": 1}) is None
    assert (
        client_kind_from_workspace_profile({"client_kind": "individual"})
        == "individual"
    )
    assert (
        client_kind_from_workspace_profile({"client_kind": "business"}) == "business"
    )
    assert (
        client_kind_from_workspace_profile('{"client_kind": "individual"}')
        == "individual"
    )
    assert client_kind_from_workspace_profile("not-json") is None
    assert client_kind_from_workspace_profile({"client_kind": "personal"}) is None
    assert resolve_client_kind("individual", {"client_kind": "business"}) == "individual"
    assert resolve_client_kind(None, {"client_kind": "individual"}) == "individual"


def test_loader_individual_branch_from_profile():
    text = resolve_system_prompt(
        workspace_profile={"client_kind": "individual", "version": 1}
    )
    assert _INDIVIDUAL_OPENING in text
    assert _BUSINESS_QUESTION not in text
    assert "what kind of business" not in text
    assert "{{#if" not in text
    assert "{{else}}" not in text
    assert "{{/if}}" not in text
    assert "Kind is `personal`" in text
    assert "entity type, EIN, payroll, and sales tax" in text


def test_loader_business_branch_unchanged():
    raw = _persona().system_prompt_path.read_text(encoding="utf-8")
    business = resolve_system_prompt(client_kind="business")
    unknown = resolve_system_prompt()
    from_empty_profile = resolve_system_prompt(workspace_profile={"version": 1})
    assert business == unknown == from_empty_profile
    assert _BUSINESS_QUESTION in business
    assert _INDIVIDUAL_OPENING not in business
    assert "{{#if" not in business
    # Business/unknown keeps the original opening and the six kind options.
    for kind in ("C-Corp", "S-Corp", "LLC", "Sole Proprietorship", "Personal household"):
        assert kind in business
    assert "Hi — I'm the Accounting-Ops **Setup Assistant**" in business
    # The unresolved file still holds both branches; the business resolve
    # drops only the individual block.
    assert _INDIVIDUAL_OPENING in raw
    assert _BUSINESS_QUESTION in raw


def test_persona_system_prompt_injects_client_kind():
    p = _persona()
    individual = p.system_prompt(
        **_PROMPT_IDS, workspace_profile={"client_kind": "individual"}
    )
    business = p.system_prompt(**_PROMPT_IDS, client_kind="business")
    unknown = p.system_prompt(**_PROMPT_IDS)

    assert "- client_kind:   individual" in individual
    assert _INDIVIDUAL_OPENING in individual
    assert _BUSINESS_QUESTION not in individual
    assert "what kind of business" not in individual
    assert "INTERACTIVE CHAT MODE" in individual

    assert "- client_kind:   business" in business
    assert _BUSINESS_QUESTION in business
    assert _INDIVIDUAL_OPENING not in business

    assert "- client_kind:   unknown" in unknown
    assert _BUSINESS_QUESTION in unknown
    assert _INDIVIDUAL_OPENING not in unknown
