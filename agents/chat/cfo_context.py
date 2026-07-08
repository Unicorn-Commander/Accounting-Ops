"""Grounding context for the CFO chat persona.

Assembles a compact, read-only "Agent Memory & Sentinel" block that is appended
to the CFO Agent's system prompt at chat time so the CFO speaks with the SAME
voice to everything the rest of the platform has already learned / flagged:

  1. FalkorDB agent-memory (workspace + entity scoped subgraph):
     - recent ``Decision`` history (verdict / reasoning) + OVERRIDES lineage,
       so the CFO can say "what we decided and why / what changed".
     - active ``ComplianceRule`` nodes for the entity.
     - notable learned vendor→category patterns (ranked by human confirmations).
  2. Sentinel findings (Postgres ``sentinel_finding``, RLS-scoped), so the CFO
     is the single voice that surfaces "what Sentinel flagged" to the user.

EVERY read here is BEST-EFFORT. The graph is a non-authoritative projection of
Postgres and may be unreachable; Sentinel may have no findings. On ANY failure
(graph down, falkordb-py absent, query error, empty result) this returns an
empty string and the chat behaves EXACTLY as it does today — no error is
surfaced to the user, nothing blocks the stream. Failures are logged at DEBUG.

Workspace + entity scoped always. No PII leaves the workspace: only data the
graph already holds (verdict/reasoning text written by the platform's own
agents, vendor names already on bank descriptors, category strings) and the
Sentinel finding title/summary the user can already see in the Sentinel UI.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import text

_log = logging.getLogger("acctops.chat.cfo_context")


# Caps — keep the injected block small so it never crowds the persona prompt or
# the user's actual question. These are deliberately conservative.
MAX_DECISIONS = 8
MAX_COMPLIANCE_RULES = 8
MAX_LEARNED_PATTERNS = 8
MAX_SENTINEL_FINDINGS = 8

# Truncate any single free-text field (reasoning, summary) so a runaway memo
# can't blow up the prompt.
_FIELD_TRUNCATE = 300

# Sentinel severities, most→least urgent, for ordering the findings surface.
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _truncate(value: Any, limit: int = _FIELD_TRUNCATE) -> str:
    s = str(value or "").strip().replace("\n", " ")
    if len(s) > limit:
        return s[: limit - 1].rstrip() + "…"
    return s


def _ms_to_date(value: Any) -> str:
    """Epoch-milliseconds (how the graph stores ``created_at``) → YYYY-MM-DD.

    Falls back to a best-effort string for anything non-numeric, and to empty
    string when there's nothing usable. Never raises.
    """
    if value is None:
        return ""
    try:
        return dt.datetime.fromtimestamp(
            int(value) / 1000, tz=dt.timezone.utc
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return _truncate(value, 32)


# ---------------------------------------------------------------------------
# Sentinel findings — Postgres read, reuses the caller's RLS-scoped session.
# ---------------------------------------------------------------------------


async def _recent_sentinel_findings(
    session: Any,
    entity_id: str,
    *,
    days: int = 30,
    limit: int = MAX_SENTINEL_FINDINGS,
) -> list[dict]:
    """Open findings for this entity (+ workspace-wide) in the last ``days``.

    Reuses the caller's already-open ``workspace_session`` so RLS
    (``app.current_workspace``) is in force — no cross-tenant leakage. Entity
    filter mirrors the Sentinel API: rows for this entity OR workspace-wide
    (``entity_id IS NULL``). Best-effort: returns [] on any error.
    """
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT check_type, severity, title, summary, created_at
                    FROM sentinel_finding
                    WHERE status = 'open'
                      AND created_at >= now() - make_interval(days => :days)
                      AND (entity_id = :eid OR entity_id IS NULL)
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {"eid": str(entity_id), "days": days, "limit": int(limit)},
            )
        ).mappings().all()
    except Exception as exc:  # noqa: BLE001 - best-effort; degrade silently
        _log.debug("sentinel findings read failed (ignored): %s", exc)
        return []

    findings = [dict(r) for r in rows]
    # Surface the most urgent first within the recent window.
    findings.sort(
        key=lambda f: (
            _SEVERITY_RANK.get(str(f.get("severity", "info")).lower(), 9),
            # created_at desc as the tiebreak (negate via reverse-friendly key).
        )
    )
    return findings


# ---------------------------------------------------------------------------
# FalkorDB agent-memory — best-effort graph reads.
# ---------------------------------------------------------------------------


async def _agent_memory(workspace_id: str, entity_id: str) -> dict[str, list[dict]]:
    """Pull decisions + compliance rules + learned patterns from the graph.

    Each sub-read is independently guarded so one empty/failing read doesn't
    drop the others. Returns a dict with possibly-empty lists; never raises.
    """
    decisions: list[dict] = []
    rules: list[dict] = []
    patterns: list[dict] = []

    # Import lazily so a missing falkordb-py (tests / minimal envs) doesn't even
    # import-error here — the module-level import in app.falkordb already guards
    # the driver, but keep this defensive.
    try:
        from app import falkordb
    except Exception as exc:  # noqa: BLE001
        _log.debug("falkordb import failed (ignored): %s", exc)
        return {"decisions": [], "rules": [], "patterns": []}

    try:
        decisions = await falkordb.recent_decisions(
            workspace_id, entity_id, limit=MAX_DECISIONS
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("recent_decisions read failed (ignored): %s", exc)

    try:
        rules = await falkordb.get_active_compliance_rules(workspace_id, entity_id)
    except Exception as exc:  # noqa: BLE001
        _log.debug("compliance rules read failed (ignored): %s", exc)

    try:
        patterns = await falkordb.notable_learned_patterns(
            workspace_id, entity_id, limit=MAX_LEARNED_PATTERNS
        )
    except Exception as exc:  # noqa: BLE001
        _log.debug("learned patterns read failed (ignored): %s", exc)

    return {"decisions": decisions, "rules": rules, "patterns": patterns}


# ---------------------------------------------------------------------------
# Markdown rendering of the assembled context.
# ---------------------------------------------------------------------------


def _render_decisions(decisions: list[dict]) -> list[str]:
    lines: list[str] = []
    for d in decisions[:MAX_DECISIONS]:
        verdict = _truncate(d.get("verdict"), 80) or "(verdict n/a)"
        agent = _truncate(d.get("agent_id"), 60)
        when = _ms_to_date(d.get("created_at"))
        reasoning = _truncate(d.get("reasoning"))
        head = f"- **{verdict}**"
        meta = " · ".join(p for p in (agent, when) if p)
        if meta:
            head += f" ({meta})"
        if reasoning:
            head += f" — {reasoning}"
        lines.append(head)
        overrides = d.get("overrides")
        if isinstance(overrides, dict) and overrides.get("verdict"):
            lines.append(
                f"    ↳ supersedes a prior decision: "
                f"{_truncate(overrides.get('verdict'), 80)}"
                + (
                    f" — {_truncate(overrides.get('reasoning'), 160)}"
                    if overrides.get("reasoning")
                    else ""
                )
            )
        if d.get("superseded_by_id"):
            lines.append(
                "    ⚠ this decision was later overridden — treat as historical, "
                "not the current position."
            )
    return lines


def _render_rules(rules: list[dict]) -> list[str]:
    lines: list[str] = []
    for r in rules[:MAX_COMPLIANCE_RULES]:
        kind = _truncate(r.get("kind"), 60) or "(rule)"
        trigger = _truncate(r.get("trigger"), 120)
        action = _truncate(r.get("action"), 120)
        bits = [f"- **{kind}**"]
        if trigger:
            bits.append(f"when {trigger}")
        if action:
            bits.append(f"→ {action}")
        lines.append(" ".join(bits))
    return lines


def _render_patterns(patterns: list[dict]) -> list[str]:
    lines: list[str] = []
    for p in patterns[:MAX_LEARNED_PATTERNS]:
        vendor = _truncate(p.get("vendor"), 60)
        category = _truncate(p.get("category"), 60)
        conf = p.get("confirmations", 1)
        if not vendor or not category:
            continue
        lines.append(
            f"- {vendor} → **{category}** "
            f"({conf} human confirmation{'s' if conf != 1 else ''})"
        )
    return lines


def _render_findings(findings: list[dict]) -> list[str]:
    lines: list[str] = []
    for f in findings[:MAX_SENTINEL_FINDINGS]:
        sev = _truncate(f.get("severity"), 12).lower() or "info"
        title = _truncate(f.get("title"), 140) or "(finding)"
        summary = _truncate(f.get("summary"), 200)
        check = _truncate(f.get("check_type"), 40)
        head = f"- [{sev.upper()}] **{title}**"
        if check:
            head += f" ({check})"
        if summary:
            head += f" — {summary}"
        lines.append(head)
    return lines


async def build_cfo_memory_context(
    *,
    session: Any,
    workspace_id: str,
    entity_id: str,
) -> str:
    """Assemble the CFO's grounding block, or "" when there's nothing / on error.

    Args:
        session: an already-open ``workspace_session`` (RLS-scoped to the
                 workspace) used for the Sentinel read. FalkorDB opens its own
                 workspace-scoped graph session internally.
        workspace_id / entity_id: the chat's bound scope.

    Returns a markdown string to append to the system prompt, or "" when no
    memory/findings are available (or any read failed). NEVER raises.
    """
    try:
        memory = await _agent_memory(workspace_id, entity_id)
        findings = await _recent_sentinel_findings(session, entity_id)
    except Exception as exc:  # noqa: BLE001 - hard guarantee: never raise to caller
        _log.debug("CFO memory context build failed (ignored): %s", exc)
        return ""

    decision_lines = _render_decisions(memory.get("decisions", []))
    rule_lines = _render_rules(memory.get("rules", []))
    pattern_lines = _render_patterns(memory.get("patterns", []))
    finding_lines = _render_findings(findings)

    if not (decision_lines or rule_lines or pattern_lines or finding_lines):
        # Nothing to ground on — behave exactly as today.
        return ""

    sections: list[str] = [
        "",
        "---",
        "",
        "## Agent Memory & Sentinel Context (read-only, best-effort)",
        "",
        "The following is grounding context the platform has accumulated for "
        "THIS entity — recent agent decisions (and what changed), active "
        "compliance rules, learned categorization habits, and what the Sentinel "
        "daemon has flagged. You are the single voice that speaks to the user "
        "about this. Use it to answer 'what did we decide and why', 'what "
        "changed', and to proactively surface Sentinel's open findings in your "
        "framing. It is BEST-EFFORT and may be incomplete; never present it as "
        "authoritative, and defer to live tool reads + the human for anything "
        "with a financial, legal, or filing consequence.",
    ]

    if decision_lines:
        sections += ["", "### Recent decisions (newest first)", *decision_lines]
    if rule_lines:
        sections += ["", "### Active compliance rules", *rule_lines]
    if pattern_lines:
        sections += ["", "### Learned vendor → category patterns", *pattern_lines]
    if finding_lines:
        sections += [
            "",
            "### Open Sentinel findings (you surface these)",
            *finding_lines,
        ]

    sections.append("")
    return "\n".join(sections)
