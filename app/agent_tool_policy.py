"""Server-side per-agent tool allow-list for the MCP surface.

WHY THIS EXISTS
---------------
Until now the only server-side gate on a tool call was ``authorize_workspace``:
workspace membership + a role floor. That means any bearer holding the
``accountant`` role could call ANY of the ~73 registered tools. The chat
personas do carry tool allow-lists, but those are enforced inside the chat
runner process (``agents/chat/runner.py``) — a client-side promise that a
compromised, modified, or simply different client never has to keep.

The exposure that matters is not the chat personas (a human is driving those,
and the human's own role is the real gate). It is the UNATTENDED service-token
agents: ``receipt-reconciler`` reads OCR text out of arbitrary uploaded PDFs,
``listing-ops-ao-sync`` reads marketplace-supplied text. Those are exactly the
callers whose input is attacker-influenced, and exactly the callers that run
with nobody watching. They also happen to be the callers the server CAN
identify: a service token carries an ``agent_id``, so the identity needed to
gate them already exists on every request.

Measured privilege gap that motivated this (dogfood ``decision_audit``, the
full recorded history): ``receipt-reconciler`` has ever called exactly ONE
tool, ``approval_decision``. Its token can reach all 73.

SEMANTICS — additive and fail-safe by construction
--------------------------------------------------
Policy comes from ``ACCTOPS_AGENT_TOOL_POLICY``, a JSON object mapping an
``agent_id`` to the list of tool names that agent may call::

    {"receipt-reconciler": ["list_transactions", "link_receipt_to_transaction"]}

- No ``agent_id`` on the request (a human's token) -> NOT restricted here. The
  role floor remains the gate; this layer is about non-human callers.
- ``agent_id`` present but absent from the policy -> READ-ONLY. It may call a
  tool whose ``app.api.discovery`` metadata says ``read_only: True`` and
  nothing else (a tool with no metadata is refused). An agent that must write
  is enrolled explicitly, with its list or ``"*"``. This used to be fail-open
  (an unknown agent reached all ~73 tools); AO-MCP-1 closed it.
- ``agent_id`` present AND in the policy -> the tool must be in its list, or
  the call is refused before the tool body runs.

An empty list is meaningful: it denies that agent everything. ``"*"`` in a
list allows everything (an explicit, greppable "characterised, unrestricted"
marker rather than the silence of being absent).

Restricting an agent is therefore always a deliberate act, and once taken it
is a real server-side constraint that no client can talk its way out of.
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

_log = logging.getLogger("accounting_ops.agent_tool_policy")

ENV_VAR = "ACCTOPS_AGENT_TOOL_POLICY"
ALLOW_ALL = "*"


@lru_cache(maxsize=1)
def _policy() -> dict[str, frozenset[str]]:
    """Parse ACCTOPS_AGENT_TOOL_POLICY once.

    A malformed policy is refused loudly at parse time rather than being
    silently treated as "no policy" — a security control that disables itself
    on a typo is worse than no control, because it still looks enabled.
    """
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ENV_VAR} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{ENV_VAR} must be a JSON object of agent_id -> [tool names]")

    policy: dict[str, frozenset[str]] = {}
    for agent_id, tools in parsed.items():
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise ValueError(
                f"{ENV_VAR}[{agent_id!r}] must be a list of tool-name strings"
            )
        policy[str(agent_id)] = frozenset(tools)
    _log.info(
        "agent tool policy loaded for %d agent(s): %s",
        len(policy),
        ", ".join(sorted(policy)),
    )
    return policy


def reset_policy_cache() -> None:
    """Drop the parsed-policy cache (tests, and any live config reload)."""
    _policy.cache_clear()
    _observed.clear()


def is_tool_allowed(agent_id: str | None, tool_name: str) -> bool:
    """Whether ``agent_id`` may call ``tool_name``. See module docstring."""
    if not agent_id:
        return True  # human caller: the role floor is the gate
    allowed = _policy().get(agent_id)
    if allowed is None:
        _observe(agent_id, tool_name)
        return _is_read_only_tool(tool_name)  # agent not enrolled: read-only
    return ALLOW_ALL in allowed or tool_name in allowed


def is_tool_visible(agent_id: str | None, tool_name: str) -> bool:
    """Whether ``tools/list`` shows ``tool_name`` to ``agent_id``.

    Same rule as :func:`is_tool_allowed` (an unenrolled agent sees only
    read-only tools, an enrolled one only its list) but without the
    observation log, which records calls, not listings.
    """
    if not agent_id:
        return True
    allowed = _policy().get(agent_id)
    if allowed is None:
        return _is_read_only_tool(tool_name)
    return ALLOW_ALL in allowed or tool_name in allowed


def _is_read_only_tool(tool_name: str) -> bool:
    """True only for a tool the discovery metadata marks read-only.

    Imported lazily: app.api.discovery is the one registry of read_only flags
    and pulls in FastAPI. A tool missing from it is treated as a write.
    """
    from app.api.discovery import _TOOL_METADATA

    meta = _TOOL_METADATA.get(tool_name)
    return bool(meta and meta.get("read_only") is True)


def _observe(agent_id: str, tool_name: str) -> None:
    """Record that an unenrolled agent asked for a tool, once per (agent, tool).

    Enrolment needs the agent's REAL tool set, and there is no existing source
    for it: ``decision_audit`` only captures write tools that call
    ``write_decision_audit``, so every read an agent depends on is invisible
    there. Guessing a list from that would confine an agent into breakage the
    first time it reads something.

    So the un-enrolled path is also the discovery path: run normally, then
    ``grep agent_tool_policy_observed`` the logs to get each agent's actual
    surface and enrol from evidence. De-duplicated in-process so a busy agent
    logs each tool once per restart rather than once per call.
    """
    key = (agent_id, tool_name)
    if key in _observed:
        return
    _observed.add(key)
    _log.info(
        "agent_tool_policy_observed agent_id=%r tool=%r — agent is NOT enrolled "
        "in %s; this line is the evidence for building its allow-list",
        agent_id,
        tool_name,
        ENV_VAR,
    )


_observed: set[tuple[str, str]] = set()


def enforce_tool_policy(agent_id: str | None, tool_name: str) -> None:
    """Raise ``PermissionError`` if ``agent_id`` may not call ``tool_name``.

    The denial is logged at WARNING with the agent and tool (never the token)
    so an attempt to reach outside an agent's remit is visible in the logs —
    that signal is most of the value of having the policy at all.
    """
    if is_tool_allowed(agent_id, tool_name):
        return
    _log.warning(
        "agent_tool_policy_denied agent_id=%r tool=%r — not in this agent's "
        "allow-list (%s)",
        agent_id,
        tool_name,
        ENV_VAR,
    )
    raise PermissionError(
        f"Agent {agent_id!r} is not permitted to call {tool_name!r}. This "
        "agent has a server-side tool allow-list; the call was refused before "
        "the tool ran."
    )
