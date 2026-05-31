"""Persona registry for the in-app /chat surface.

Each entry corresponds to one of the existing tax-team agents whose
``persona.md`` + ``system_prompt.md`` live on disk under ``agents/``.

The chat runner loads ``system_prompt.md`` for the selected persona and
prepends a small "conversational mode" preamble so the agent answers
follow-up questions interactively instead of producing a single end-to-end
deliverable. The MCP tool allow-list comes from this registry too — every
persona gets only the tools it needs, never more.
"""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal


Tier = Literal["strategic", "specialist", "operational"]


# Paths are resolved relative to the repo root (the agents/ package).
_AGENTS_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Conversational preamble prepended to every persona system prompt
# ---------------------------------------------------------------------------
#
# The on-disk system_prompt.md files are written for the standalone CLI
# runners that produce ONE deliverable per invocation (e.g. "DRAFT Form 1120
# markdown package"). In the chat surface the user expects a back-and-forth
# conversation: they may ask a single question, ask for an analysis on
# something narrower than a full form, or pivot mid-conversation.
#
# This preamble re-frames the agent's behavior without rewriting the persona.
# It is appended to the front of the system prompt; the rest of the agent's
# expertise + guardrails are preserved.

_CHAT_MODE_PREAMBLE = """\
You are operating in INTERACTIVE CHAT MODE inside the Accounting-Ops web app.
Aaron (or another workspace member) is chatting with you directly through a
Claude.ai-style interface.

Behavior in this mode:

- Answer the user's question conversationally. Do NOT produce a single
  end-to-end deliverable unless they explicitly ask for one. Short, focused
  answers are preferred.
- You can call MCP tools whenever real data would improve the answer.
  Always cite the workspace_id / entity_id / tax_year that the chat is
  bound to (passed below). NEVER access a different entity than the one
  scoped to this conversation.
- When you call a tool, the user will see a collapsed tool-call card in the
  UI showing the tool name, arguments, and result. Be precise about which
  tool you're calling and why.
- Markdown is rendered (headings, lists, tables, code blocks). Use it
  sparingly for clarity, not as decoration.
- Long artifacts (memos > 1K chars, draft form sections, calculation
  tables) will be opened in a side "canvas" pane automatically. You don't
  need to do anything special — just produce the content.
- DRAFT memos and DRAFT recommendations are allowed; you may call
  draft_memo and flag_for_cpa_review. You must NOT post anything to the
  approval queue's decide endpoint — that requires an explicit user click
  in the UI. There is no MCP tool for that and there should not be one.
- If the user asks for something outside your scope, hand off explicitly
  ("That's a question for the SC State Specialist — I'll let you switch
  personas in the top bar") rather than guessing.

Chat context:

- workspace_id:  {workspace_id}
- entity_id:     {entity_id}
- tax_year:      {tax_year}
- caller uc_uid: {user_uc_uid}

Below this preamble is your full agent persona and system prompt. Follow
the guardrails defined there. The conversational instructions above
override only the "produce one deliverable" framing — every other rule
(escalation categories, citation requirements, scope restrictions, what
you may and may not do) stays in force.

---
"""


@dataclass(frozen=True)
class Persona:
    """A single chat-selectable agent persona."""

    slug: str
    display_name: str
    one_line: str  # Shown next to the slug in the agent picker
    persona_path: pathlib.Path
    system_prompt_path: pathlib.Path
    allowed_mcp_tools: tuple[str, ...]
    # If True, the runner will pre-bind {workspace_id, entity_id, tax_year}
    # into the conversational preamble so the agent doesn't have to ask.
    needs_tax_year: bool = True
    # UI grouping in the persona picker. Default is "specialist" for backward
    # compatibility with all the existing tax-specialists; opt in to other
    # tiers when registering newer personas (CFO Agent = strategic).
    tier: Tier = "specialist"
    # Per-persona model override. None = let the chat runner pick (Sonnet
    # default). Set to "claude-opus-4-7" on personas that warrant the deeper
    # reasoning (CFO Agent strategic synthesis).
    default_model: str | None = None
    # True when this persona is allowed to consume Brigade-federated
    # cross-app tools (project.*, meeting.*, contact.*) when the caller has
    # the brigade:cross_app scope AND brigade_client.get_cross_app_catalog()
    # is online. Default False for all existing personas — opt in.
    consumes_brigade: bool = False

    def system_prompt(
        self,
        *,
        workspace_id: str,
        entity_id: str,
        tax_year: int | None,
        user_uc_uid: str,
    ) -> str:
        """Build the full system prompt: preamble + on-disk system_prompt.md."""
        base = self.system_prompt_path.read_text(encoding="utf-8")
        preamble = _CHAT_MODE_PREAMBLE.format(
            workspace_id=workspace_id,
            entity_id=entity_id,
            tax_year=tax_year if tax_year is not None else "(not bound for this session)",
            user_uc_uid=user_uc_uid,
        )
        return preamble + base


# ---------------------------------------------------------------------------
# Tool allow-lists — defense in depth. The chat runner intersects these with
# the MCP server's actual tool list so unknown / new tools are auto-rejected.
# Read-only tools are safe to expose to all personas; write tools are
# allow-listed per persona based on what each agent actually needs.
# ---------------------------------------------------------------------------

# Read-only tools every persona can use to pull live workspace data.
_READ_TOOLS: tuple[str, ...] = (
    "list_transactions",
    "get_transaction",
    "get_p_and_l",
    "get_runway",
    "get_consolidated_p_and_l",
    "query_specialty_rules",
    "get_current_rule",
    "get_form_1120_data",
    "get_period_journal",
    "get_1099_payees",
)

# Write tools that draft into the tax_review_queue (still requires human review
# before any tax position is established). These are the only writes the chat
# surface enables; payment-proposing / approval-deciding tools are NOT exposed.
_QUEUE_DRAFT_TOOLS: tuple[str, ...] = (
    "draft_memo",
    "flag_for_cpa_review",
)


# Builder helper — keeps the per-persona definitions readable.
def _persona(
    slug: str,
    display_name: str,
    one_line: str,
    rel_dir: str,
    *,
    tools: tuple[str, ...],
    needs_tax_year: bool = True,
    tier: Tier = "specialist",
    default_model: str | None = None,
    consumes_brigade: bool = False,
) -> Persona:
    base = _AGENTS_ROOT / rel_dir
    return Persona(
        slug=slug,
        display_name=display_name,
        one_line=one_line,
        persona_path=base / "persona.md",
        system_prompt_path=base / "system_prompt.md",
        allowed_mcp_tools=tools,
        needs_tax_year=needs_tax_year,
        tier=tier,
        default_model=default_model,
        consumes_brigade=consumes_brigade,
    )


# ---------------------------------------------------------------------------
# CFO Agent — strategic synthesis layer (new 2026-05-30)
# ---------------------------------------------------------------------------
#
# The CFO Agent is a tier-up synthesis persona: it answers board-room-level
# questions (cash runway, dilution, year-end strategy, board narrative) by
# READING the data the operational + specialist agents produce, and SYNTHE-
# SIZING across them. It explicitly does not do tactical work — it surfaces
# to CPA / attorney / 409A provider / board for anything outside its scope.
#
# Model: Claude Opus 4.7 by default for deeper reasoning. Override via the
# CFO_AGENT_MODEL env var. The runner falls back to Sonnet 4.5 when Opus
# returns "model not found" (e.g. account doesn't have Opus access yet).
#
# Brigade: consumes_brigade=True — when the caller's bearer carries
# brigade:cross_app AND brigade_client.get_cross_app_catalog().online, the
# runner injects project.* / meeting.* / contact.* tools alongside the
# local 17. Degrades to local-only when Brigade is offline.
#
# Tools: read-only + draft set, PLUS a set of "synthesis aliases" the brief
# enumerates (get_balance_sheet, get_cash_flow, get_trial_balance, etc.).
# Those aliases are resolved to the underlying registered MCP tool names at
# dispatch time (see app/api/chat.py).

CFO_AGENT_SYNTHESIS_ALIASES: tuple[str, ...] = (
    "get_balance_sheet",       # alias: /api/.../reports/balance-sheet
    "get_cash_flow",           # alias: /api/.../reports/cash-flow
    "get_trial_balance",       # alias: /api/.../reports/trial-balance
    "get_tax_status",          # alias: /api/.../tax-status
    "get_cash_position",       # alias: /api/.../cash-position
    "get_runway",              # already in _READ_TOOLS
    "list_specialist_runs",    # alias: /api/.../specialist-runs
    "search_tax_knowledge",    # alias of get_current_rule (RAG)
    "list_pending_approvals",  # alias of analyze_pending_approvals
    "get_tax_narrative",       # alias of get_form_1120_data narrative
)


def _cfo_default_model() -> str:
    """CFO Agent model: Opus 4.7 by default; CFO_AGENT_MODEL env overrides."""
    return os.getenv("CFO_AGENT_MODEL", "claude-opus-4-7")


# ---------------------------------------------------------------------------
# Registry — order matters: the UI shows personas in this order, and the
# default (CPA Tax Resolver) is the first entry.
# ---------------------------------------------------------------------------

PERSONAS: tuple[Persona, ...] = (
    # ----- Strategic tier — synthesis across operational + specialist work --
    _persona(
        "cfo-agent",
        "CFO Agent",
        "Strategic financial view — cash runway, unit economics, board reporting, financing strategy.",
        "cpa_team/cfo_agent",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS + CFO_AGENT_SYNTHESIS_ALIASES,
        needs_tax_year=True,
        tier="strategic",
        default_model=_cfo_default_model(),
        consumes_brigade=True,
    ),
    _persona(
        "cpa-tax-resolver",
        "CPA Tax Resolver",
        "Resolves queue items + cites rules; default conversational entry point.",
        "cpa_team/tax_resolver",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS,
        tier="strategic",
    ),
    # ----- Operational tier — setup, intake, runtime ops --------------------
    _persona(
        "setup-assistant",
        "Setup Assistant",
        "Conversationally provisions a new workspace, entity, COA, and connectors. Run this first.",
        "cpa_team/setup_assistant",
        tools=(
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
        ),
        needs_tax_year=False,
        tier="operational",
    ),
    _persona(
        "form-1120-preparer",
        "Form 1120 Preparer",
        "Drafts federal C-corp returns line by line.",
        "tax_specialists/form_1120_preparer",
        tools=(
            "get_form_1120_data",
            "get_period_journal",
            "get_1099_payees",
            "draft_memo",
            "flag_for_cpa_review",
            "get_current_rule",
        ),
    ),
    _persona(
        "sc-state-specialist",
        "SC State Specialist",
        "South Carolina SC1120 + state-specific compliance.",
        "tax_specialists/sc_state_specialist",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS,
    ),
    _persona(
        "state-strategy-drafter",
        "State Strategy Drafter",
        "Multi-state nexus + apportionment strategy memos.",
        "tax_specialists/state_strategy_drafter",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS,
    ),
    _persona(
        "federal-strategy-drafter",
        "Federal Strategy Drafter",
        "Year-end federal tax-planning analysis (R&D, NOL, §199A, etc.).",
        "tax_specialists/federal_strategy_drafter",
        tools=(
            "get_form_1120_data",
            "get_period_journal",
            "draft_memo",
            "flag_for_cpa_review",
            "get_current_rule",
        ),
    ),
    _persona(
        "1099-specialist",
        "1099-NEC Specialist",
        "Identifies 1099 payees + drafts year-end information returns.",
        "tax_specialists/1099_specialist",
        tools=(
            "get_1099_payees",
            "get_period_journal",
            "draft_memo",
            "flag_for_cpa_review",
            "get_current_rule",
        ),
    ),
    _persona(
        "officer-compensation-specialist",
        "Officer Compensation Specialist",
        "Form 1125-E + reasonable-comp analysis for C-corp officers.",
        "tax_specialists/officer_compensation_specialist",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS,
    ),
    _persona(
        "nol-schedule-l-analyst",
        "NOL / Schedule L Analyst",
        "Net operating loss carryforwards + balance-sheet reconciliation.",
        "tax_specialists/nol_schedule_l_analyst",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS,
    ),
    _persona(
        "audit-defense-specialist",
        "Audit Defense Specialist",
        "Builds defensible positions + audit-readiness packages.",
        "tax_specialists/audit_defense_specialist",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS,
    ),
    _persona(
        "personal-1040-specialist",
        "Personal 1040 Specialist",
        "Individual returns with military retirement / VA disability / SC veteran provisions.",
        "tax_specialists/personal_1040_specialist",
        tools=(
            "get_period_journal",
            "get_tax_review_queue",
            "draft_memo",
            "flag_for_cpa_review",
            "get_current_rule",
        ),
    ),
)


@lru_cache(maxsize=1)
def _index() -> dict[str, Persona]:
    """Slug → Persona lookup. Cached because PERSONAS is frozen."""
    return {p.slug: p for p in PERSONAS}


def get_persona(slug: str) -> Persona | None:
    """Return the persona for ``slug`` or None when not registered."""
    return _index().get(slug)


def list_personas(brigade_online: bool = False) -> list[dict]:
    """Lightweight list for the agent-picker UI (no file I/O).

    When ``brigade_online`` is True, the per-persona payload includes a
    ``brigade_active`` flag the picker uses to show the "+N cross-app tools
    available" badge on personas that consume Brigade.
    """
    return [
        {
            "slug": p.slug,
            "display_name": p.display_name,
            "one_line": p.one_line,
            "tier": p.tier,
            "needs_tax_year": p.needs_tax_year,
            "consumes_brigade": p.consumes_brigade,
            "brigade_active": p.consumes_brigade and brigade_online,
            "default_model": p.default_model,
            "tool_count": len(p.allowed_mcp_tools),
        }
        for p in PERSONAS
    ]


# Default persona selected when /chat is opened fresh (no session id).
# Explicitly set to the CPA Tax Resolver so the prepended Setup Assistant
# does not become the implicit default for an established workspace's chat.
# Setup Assistant has its own entry point: /setup (and is opened explicitly).
DEFAULT_PERSONA_SLUG = "cpa-tax-resolver"
