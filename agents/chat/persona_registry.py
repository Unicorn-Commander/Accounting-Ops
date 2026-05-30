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

import pathlib
from dataclasses import dataclass
from functools import lru_cache


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
    )


# ---------------------------------------------------------------------------
# Registry — order matters: the UI shows personas in this order, and the
# default (CPA Tax Resolver) is the first entry.
# ---------------------------------------------------------------------------

PERSONAS: tuple[Persona, ...] = (
    _persona(
        "cpa-tax-resolver",
        "CPA Tax Resolver",
        "Resolves queue items + cites rules; default conversational entry point.",
        "cpa_team/tax_resolver",
        tools=_READ_TOOLS + _QUEUE_DRAFT_TOOLS,
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
)


@lru_cache(maxsize=1)
def _index() -> dict[str, Persona]:
    """Slug → Persona lookup. Cached because PERSONAS is frozen."""
    return {p.slug: p for p in PERSONAS}


def get_persona(slug: str) -> Persona | None:
    """Return the persona for ``slug`` or None when not registered."""
    return _index().get(slug)


def list_personas() -> list[dict[str, str]]:
    """Lightweight list for the agent-picker UI (no file I/O)."""
    return [
        {
            "slug": p.slug,
            "display_name": p.display_name,
            "one_line": p.one_line,
        }
        for p in PERSONAS
    ]


# Default persona selected when /chat is opened fresh (no session id).
DEFAULT_PERSONA_SLUG = PERSONAS[0].slug
