"""Setup Assistant persona package.

The Setup Assistant is a conversational chat agent that walks a new
workspace owner through provisioning their first entity + COA + initial
connector recommendations.

Lives under cpa_team/ rather than tax_specialists/ because it is NOT a tax
filer — it is a hospitality / on-boarding agent. The Tax Resolver (also in
cpa_team/) is the closest cousin: both are coordinator-style agents rather
than form-line specialists.

This package has no CLI runner — the assistant is only ever invoked via the
in-app /chat surface (web/app/setup/page.tsx pre-binds the persona slug
``setup-assistant``). The functions below are the prompt loader: they read
``setup_progress.client_kind`` and resolve ``{{#if individual}}`` blocks in
system_prompt.md.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
SYSTEM_PROMPT_PATH = _DIR / "system_prompt.md"
PERSONA_PATH = _DIR / "persona.md"

CLIENT_KINDS = ("business", "individual")

# Paired if/else only. Nested blocks are not supported.
_IF_INDIVIDUAL = re.compile(
    r"\{\{#if\s+individual\s*\}\}(.*?)\{\{else\}\}(.*?)\{\{/if\}\}",
    re.DOTALL,
)


def client_kind_from_workspace_profile(profile: Any) -> str | None:
    """Read ``client_kind`` off a workspace ``setup_progress`` profile.

    Returns ``business`` or ``individual`` when the profile carries a known
    value. Returns ``None`` when the profile is missing or the key is
    unknown — the prompt then uses the business/unknown text.
    """
    if isinstance(profile, str):
        try:
            profile = json.loads(profile)
        except json.JSONDecodeError:
            return None
    if not isinstance(profile, dict):
        return None
    raw = profile.get("client_kind")
    if raw in CLIENT_KINDS:
        return raw
    return None


def resolve_client_kind(
    client_kind: str | None = None,
    workspace_profile: Any = None,
) -> str | None:
    """Prefer an explicit kind; otherwise read it from the workspace profile."""
    if client_kind in CLIENT_KINDS:
        return client_kind
    return client_kind_from_workspace_profile(workspace_profile)


def resolve_system_prompt(
    text: str | None = None,
    *,
    client_kind: str | None = None,
    workspace_profile: Any = None,
) -> str:
    """Resolve ``{{#if individual}}`` blocks in the Setup Assistant prompt.

    Individual → 1040 opening, no "what kind of business". Business or
    unknown (``client_kind`` missing) → the original business opening.
    """
    source = (
        text if text is not None else SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    )
    individual = resolve_client_kind(client_kind, workspace_profile) == "individual"

    def _repl(match: re.Match[str]) -> str:
        chosen = match.group(1) if individual else match.group(2)
        return chosen.strip("\n")

    return _IF_INDIVIDUAL.sub(_repl, source)
