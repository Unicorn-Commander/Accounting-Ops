"""In-app chat (Claude.ai-style) — persona registry + streaming runner.

The /chat web surface talks to the FastAPI ``/api/chat/sessions`` endpoints,
which delegate the actual LLM invocation to the runner here. Each chat
session is bound to (workspace, entity, agent_persona, optional tax_year)
and uses the existing OpenRouter / Anthropic SDK wiring from the
specialist runners.

Personas are loaded from on-disk ``persona.md`` + ``system_prompt.md`` files
inside ``agents/cpa_team/`` and ``agents/tax_specialists/`` — the same files
the standalone CLI runners read. The chat surface adds a thin "conversational
mode" prefix so the agent answers questions interactively rather than
producing a single end-to-end deliverable.
"""

from agents.chat.persona_registry import (
    PERSONAS,
    Persona,
    get_persona,
    list_personas,
)

__all__ = ["PERSONAS", "Persona", "get_persona", "list_personas"]
