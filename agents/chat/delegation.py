"""CFO → specialist delegation (agents-as-tools).

The CFO Agent chat persona answers board-room questions by SYNTHESIZING across
the platform's data. Some questions need a specialist's read (e.g. "is this
officer comp defensible?" → Officer Compensation Specialist; "do we owe SC
estimated tax?" → SC State Specialist). Rather than make the user switch
personas, the CFO **silently consults** the relevant specialist mid-turn and
folds the analysis into ONE synthesized answer.

This is implemented as **agents-as-tools**: the CFO persona is given a single
extra local tool — ``consult_specialist(specialist, question)`` — that the
runner dispatches HERE instead of to the MCP server. Each consult runs a
BOUNDED, SINGLE-TURN consultation of the chosen specialist's chat persona,
reusing the exact same machinery (``runner.stream_assistant_turn``) the chat
already uses, with the SAME workspace / entity / tax-year / caller bearer.

Hard guarantees (verified by tests in tests/smoke/test_cfo_delegation.py):

  * **Depth-1 only.** A specialist consulted this way is NEVER given the
    ``consult_specialist`` tool, so it cannot recurse / fan out. The runner is
    invoked with ``delegation=None`` for the inner turn and we additionally
    strip the tool name from the inner allow-list as belt-and-suspenders.
  * **Scope-safe, no escalation.** The consult carries the caller's exact
    workspace_id / entity_id / tax_year / bearer token. The MCP auth middleware
    re-validates the bearer on every inner tool call, so the specialist sees
    EXACTLY the data the caller can see — never more. There is no path to widen
    scope.
  * **No auto-posting.** A specialist's chat-persona tool set is read/draft only
    (same allow-list the persona has in the picker); the consult does not add
    any mutate/approve tool. Drafts still flow through the human approval +
    materiality gate. The CFO synthesizes + recommends; humans approve.
  * **Bounded.** ≤ ``MAX_CONSULTS_PER_TURN`` consults per CFO turn (enforced by
    a per-turn budget), conservative token + iteration caps, and a wall-clock
    timeout. On ANY failure (timeout, specialist error, unknown slug, budget
    exhausted) the consult returns a structured error string the CFO folds in
    gracefully ("I couldn't reach the X specialist; based on the data I have…").
    It NEVER raises into the chat stream.
  * **Auditable.** "Silent" means UX-silent, not unaudited. Each consult is
    recorded via an injected ``audit_sink`` callback (the chat layer writes a
    ``decision_audit`` row with tool_name='consult_specialist' + model_version=
    the CFO agent id), so the delegation surfaces in the AI-activity timeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

_log = logging.getLogger("acctops.chat.delegation")


# The local tool name. Deliberately distinct from any MCP tool so the runner
# can recognize it and dispatch HERE rather than to the MCP server.
CONSULT_TOOL_NAME = "consult_specialist"


# ---------------------------------------------------------------------------
# Which personas the CFO may consult.
# ---------------------------------------------------------------------------
#
# The 9 focused tax specialists. We deliberately EXCLUDE:
#   * cfo-agent itself (no self-consult — and the depth guard would strip the
#     tool anyway),
#   * cpa-tax-resolver (a strategic peer, not a specialist),
#   * setup-assistant (operational / provisioning),
#   * tax-research (read-only RAG; the CFO already has search_tax_knowledge).
#
# These slugs MUST exist in agents.chat.persona_registry. Resolution is done
# lazily at dispatch via get_persona so this list stays a pure data constant
# (no import cycle with the registry at module load).
CONSULTABLE_SPECIALIST_SLUGS: tuple[str, ...] = (
    "form-1120-preparer",
    "sc-state-specialist",
    "state-strategy-drafter",
    "federal-strategy-drafter",
    "1099-specialist",
    "officer-compensation-specialist",
    "nol-schedule-l-analyst",
    "audit-defense-specialist",
    "personal-1040-specialist",
)


# ---------------------------------------------------------------------------
# Bounds — conservative so a chat turn can't fan out into a slow/costly job.
# ---------------------------------------------------------------------------

# Max specialist consults the CFO may make in a single user turn.
MAX_CONSULTS_PER_TURN = 2

# The inner consultation is a focused single answer; cap tool iterations low so
# a specialist can pull a couple of reads + answer, but not run a long loop.
CONSULT_MAX_TOOL_ITERATIONS = 4

# Output token cap for the inner consult — analysis, not a full deliverable.
CONSULT_MAX_TOKENS = 1500

# Wall-clock timeout for one consult. The CFO degrades gracefully past this.
CONSULT_TIMEOUT_SECONDS = 45.0

# Truncate the specialist's returned analysis before handing it back to the CFO
# so a runaway specialist can't blow the CFO's context window.
_ANALYSIS_TRUNCATE_CHARS = 6000

# Truncate the question the CFO supplies (defense against a giant prompt).
_QUESTION_TRUNCATE_CHARS = 2000


# A callback the chat layer supplies to persist the audit record. Async,
# best-effort: the runner awaits it but swallows any error (audit must never
# break the chat stream). Signature: (record: dict) -> Awaitable[None].
AuditSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class DelegationContext:
    """Everything one CFO turn needs to consult specialists, plus its budget.

    Built fresh per CFO turn in app/api/chat.py and handed to the runner. It
    carries the caller's EXACT scope (never widened) and a mutable counter that
    enforces the per-turn consult cap across multiple tool calls in one turn.
    """

    # ---- Caller scope (carried verbatim into the inner consult) ----
    workspace_id: str
    entity_id: str
    user_uc_uid: str
    user_bearer_token: str
    tax_year: int | None = None
    # Caller's verified token claims — read by the smart-inference entitlement
    # gate on the consult (itself a paid cloud call). None ⇒ the gate treats it
    # as "no entitlement claim" and (absent BYOK) fails closed.
    claims: dict[str, Any] | None = None

    # ---- LLM wiring (mirrors the outer chat turn) ----
    backend: str | None = None
    openrouter_base_url: str | None = None
    openrouter_api_key: str | None = None
    mcp_url: str | None = None
    # Model used for the inner consult. None → the specialist persona's
    # default_model, else the chat runner default. Kept separate from the CFO's
    # own (possibly Opus) model so consults stay cheap.
    consult_model: str | None = None

    # ---- Identity for the audit trail ----
    # The CFO agent id (e.g. "cfo-agent") recorded as decision_audit.model_version
    # so the delegation surfaces as an AI action in the activity timeline.
    cfo_agent_id: str = "cfo-agent"

    # ---- Audit + bounds ----
    audit_sink: AuditSink | None = None
    max_consults: int = MAX_CONSULTS_PER_TURN

    # Mutable per-turn state.
    consults_used: int = field(default=0, init=False)

    def budget_remaining(self) -> int:
        return max(0, self.max_consults - self.consults_used)


# ---------------------------------------------------------------------------
# Tool schema — the OpenAI function-calling descriptor for consult_specialist.
# ---------------------------------------------------------------------------


def consult_specialist_tool_schema() -> dict[str, Any]:
    """Return the OpenAI function schema the CFO sees for the delegation tool.

    The ``specialist`` parameter is a closed enum of the consultable slugs so
    the model can't name an arbitrary persona. ``question`` is the focused
    sub-question to put to that specialist.
    """
    return {
        "type": "function",
        "function": {
            "name": CONSULT_TOOL_NAME,
            "description": (
                "Privately consult ONE tax specialist to inform your synthesis, "
                "then fold their analysis into your single answer. The user does "
                "NOT see this consultation — do not announce 'routing to X'. Use "
                "it only when a question genuinely needs a specialist's read "
                "(e.g. officer-comp defensibility, SC state filing, 1099 "
                "obligations, NOL carryforward, audit posture). You may consult "
                f"at most {MAX_CONSULTS_PER_TURN} specialists per turn. The "
                "specialist sees the SAME entity/workspace and only read/draft "
                "tools — it cannot post anything or consult further. If you "
                "already have what you need, just answer."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "specialist": {
                        "type": "string",
                        "enum": list(CONSULTABLE_SPECIALIST_SLUGS),
                        "description": "Slug of the specialist persona to consult.",
                    },
                    "question": {
                        "type": "string",
                        "description": (
                            "The focused question to ask the specialist. Be "
                            "specific; include the relevant numbers/context so "
                            "they don't have to re-derive everything."
                        ),
                    },
                },
                "required": ["specialist", "question"],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Inner consult — bounded single-turn run of the specialist's chat persona.
# ---------------------------------------------------------------------------


def _inner_allowed_tools(persona_tools: tuple[str, ...]) -> tuple[str, ...]:
    """Belt-and-suspenders depth-1 guard.

    Strip ``consult_specialist`` from the specialist's allow-list so even if a
    future change accidentally adds it to a persona, a consulted specialist can
    never delegate. (The runner is ALSO invoked with delegation=None for the
    inner turn, which is the primary guard; this is the redundant one.)
    """
    return tuple(t for t in persona_tools if t != CONSULT_TOOL_NAME)


async def _collect_consult_turn(
    *,
    specialist_slug: str,
    question: str,
    ctx: DelegationContext,
) -> dict[str, Any]:
    """Run ONE specialist turn via the shared runner; collect into a result.

    Returns a structured dict:
      {"ok": bool, "specialist": slug, "analysis": str,
       "tool_calls": [name, ...], "stop_reason": str|None,
       "input_tokens": int, "output_tokens": int, "error": str|None}

    Reuses ``runner.stream_assistant_turn`` exactly as the chat surface does,
    but DOES NOT pass a delegation context (depth-1) and buffers the streamed
    text into one analysis string instead of forwarding SSE frames.
    """
    # Late import to avoid an import cycle (runner imports nothing from here,
    # but the registry + runner are heavier modules we don't want at import).
    from agents.chat import get_persona
    from agents.chat.runner import (
        DEFAULT_BACKEND,
        stream_assistant_turn,
    )

    persona = get_persona(specialist_slug)
    if persona is None:
        return {
            "ok": False,
            "specialist": specialist_slug,
            "analysis": "",
            "tool_calls": [],
            "stop_reason": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "error": f"unknown specialist {specialist_slug!r}",
        }

    # Build the specialist's normal chat system prompt with the CALLER's scope.
    system_prompt = persona.system_prompt(
        workspace_id=ctx.workspace_id,
        entity_id=ctx.entity_id,
        tax_year=ctx.tax_year,
        user_uc_uid=ctx.user_uc_uid,
    )
    # Prepend a short consultation framing so the specialist answers the CFO's
    # sub-question tightly rather than producing a full deliverable. This does
    # NOT relax any of the persona's guardrails — it only narrows the ask.
    consult_preamble = (
        "You are being consulted privately by the CFO Agent to inform a single "
        "synthesized answer to the user. Answer ONLY the specific question "
        "below, concisely, from your expertise + the live tools available to "
        "you. Cite the rule/section where it matters. Do NOT produce a full "
        "form or end-to-end deliverable. Draft tools (draft_memo / "
        "flag_for_cpa_review) remain available but use them only if a written "
        "artifact is genuinely warranted; you may NOT post or approve anything. "
        "Your answer is handed back to the CFO, not shown verbatim to the user.\n\n"
        f"CFO's question to you: {question}\n\n---\n"
    )
    system_prompt = consult_preamble + system_prompt

    allowed = _inner_allowed_tools(persona.allowed_mcp_tools)

    # Per-persona model override (e.g. a specialist may pin a model), else the
    # consult_model from the context, else the runner default (Sonnet).
    model = ctx.consult_model or persona.default_model

    text_chunks: list[str] = []
    tool_calls: list[str] = []
    input_tokens = 0
    output_tokens = 0
    stop_reason: str | None = None
    error: str | None = None

    # The runner kwargs mirror the outer chat turn (same backend/url/key/mcp),
    # but with the consult caps and NO delegation (depth-1).
    runner_kwargs: dict[str, Any] = dict(
        system_prompt=system_prompt,
        history=[],  # a consult is a fresh single-turn ask, no prior history
        user_message=question,
        allowed_mcp_tools=allowed,
        user_bearer_token=ctx.user_bearer_token,
        max_iterations=CONSULT_MAX_TOOL_ITERATIONS,
        max_tokens=CONSULT_MAX_TOKENS,
        # Same workspace attribution as the outer CFO turn (gateway/Lago user=).
        llm_user=ctx.workspace_id,
    )
    if model:
        runner_kwargs["model"] = model
    if ctx.backend:
        runner_kwargs["backend"] = ctx.backend
    if ctx.openrouter_base_url:
        runner_kwargs["openrouter_base_url"] = ctx.openrouter_base_url
    if ctx.openrouter_api_key:
        runner_kwargs["openrouter_api_key"] = ctx.openrouter_api_key
    if ctx.mcp_url:
        runner_kwargs["mcp_url"] = ctx.mcp_url

    async def _drive() -> None:
        nonlocal input_tokens, output_tokens, stop_reason, error
        async for event in stream_assistant_turn(**runner_kwargs):
            etype = event.get("type")
            if etype == "text_delta":
                text_chunks.append(event.get("text", ""))
            elif etype == "tool_call":
                name = event.get("name")
                if name:
                    tool_calls.append(name)
            elif etype == "stop":
                stop_reason = event.get("stop_reason")
                input_tokens = int(event.get("input_tokens", 0) or 0)
                output_tokens = int(event.get("output_tokens", 0) or 0)
            elif etype == "error":
                error = str(event.get("message", "consult error"))

    try:
        await asyncio.wait_for(_drive(), timeout=CONSULT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        error = f"consult timed out after {CONSULT_TIMEOUT_SECONDS:.0f}s"
        stop_reason = "timeout"
    except Exception as exc:  # noqa: BLE001 - never propagate into the chat stream
        _log.warning("specialist consult (%s) failed: %s", specialist_slug, exc)
        error = f"consult failed: {exc}"

    analysis = "".join(text_chunks).strip()
    if len(analysis) > _ANALYSIS_TRUNCATE_CHARS:
        analysis = analysis[: _ANALYSIS_TRUNCATE_CHARS - 1].rstrip() + "…"

    ok = error is None and bool(analysis)
    # A non-error empty answer (model returned nothing) is treated as a soft
    # failure so the CFO knows it got nothing usable.
    if error is None and not analysis:
        error = "specialist returned no analysis"
        ok = False

    return {
        "ok": ok,
        "specialist": specialist_slug,
        "analysis": analysis,
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "error": error,
    }


def _summarize_for_audit(analysis: str, *, limit: int = 400) -> str:
    """A short, single-line result summary for the audit trail."""
    s = " ".join(str(analysis or "").split())
    if len(s) > limit:
        return s[: limit - 1].rstrip() + "…"
    return s


async def dispatch_consult(
    *,
    args: dict[str, Any],
    ctx: DelegationContext,
) -> dict[str, Any]:
    """Handle one ``consult_specialist`` tool call from the CFO.

    This is what the runner calls when it sees the local delegation tool. It:
      1. validates the requested specialist against the closed enum,
      2. enforces the per-turn consult budget,
      3. runs the bounded single-turn consult (depth-1, scope-safe),
      4. records the delegation via ``ctx.audit_sink`` (best-effort),
      5. returns a tool-result payload the CFO synthesizes from.

    NEVER raises — every failure mode returns a structured payload the CFO can
    fold into its answer.
    """
    specialist = str(args.get("specialist", "")).strip()
    question = str(args.get("question", "")).strip()
    if len(question) > _QUESTION_TRUNCATE_CHARS:
        question = question[:_QUESTION_TRUNCATE_CHARS].rstrip()

    # ---- Closed-enum guard: only the registered specialist slugs. ----
    if specialist not in CONSULTABLE_SPECIALIST_SLUGS:
        return {
            "ok": False,
            "error": (
                f"{specialist!r} is not a consultable specialist. Choose one of: "
                + ", ".join(CONSULTABLE_SPECIALIST_SLUGS)
            ),
            "specialist": specialist,
        }

    if not question:
        return {
            "ok": False,
            "error": "question is required to consult a specialist",
            "specialist": specialist,
        }

    # ---- Budget guard: at most MAX_CONSULTS_PER_TURN per CFO turn. ----
    if ctx.budget_remaining() <= 0:
        return {
            "ok": False,
            "error": (
                f"consult budget exhausted ({ctx.max_consults} per turn). "
                "Synthesize from the consultations already gathered + your tools."
            ),
            "specialist": specialist,
        }

    # ---- Smart-inference gate: the consult is itself a paid cloud call. ----
    # Defense-in-depth — the CFO turn was already gated pre-stream in
    # app/api/chat.py, but gate the consult too so any direct/future caller of
    # this path is covered. Contract: NEVER raise — return a structured payload.
    # Self-inerting + edition-guarded import (community never imports app.billing).
    from app.config import get_settings

    if not get_settings().is_community:
        from app.billing.inference_gate import (
            SmartInferenceGateError,
            check_smart_inference_access,
        )

        try:
            await check_smart_inference_access(
                workspace_id=ctx.workspace_id,
                claims=ctx.claims,
                what=f"Consulting the {specialist} specialist",
            )
        except SmartInferenceGateError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "specialist": specialist,
                "upgrade_url": exc.upgrade_url,
            }
        except Exception:  # noqa: BLE001 — dispatch_consult must never raise; fail CLOSED
            _log.warning(
                "smart-inference gate errored during consult; blocking (fail-closed)",
                exc_info=True,
            )
            return {
                "ok": False,
                "error": "Specialist consultation is temporarily unavailable.",
                "specialist": specialist,
            }

    # Reserve the slot BEFORE running so a slow/erroring consult still counts
    # against the budget (prevents an unbounded retry loop in one turn).
    ctx.consults_used += 1

    result = await _collect_consult_turn(
        specialist_slug=specialist,
        question=question,
        ctx=ctx,
    )

    # ---- Audit (best-effort; never breaks the turn). ----
    if ctx.audit_sink is not None:
        record = {
            "tool_name": CONSULT_TOOL_NAME,
            "specialist": specialist,
            "question": question,
            "result_summary": _summarize_for_audit(result.get("analysis", "")),
            "ok": result.get("ok", False),
            "error": result.get("error"),
            "specialist_tool_calls": result.get("tool_calls", []),
            "input_tokens": result.get("input_tokens", 0),
            "output_tokens": result.get("output_tokens", 0),
            "consult_index": ctx.consults_used,
            "cfo_agent_id": ctx.cfo_agent_id,
            "workspace_id": ctx.workspace_id,
            "entity_id": ctx.entity_id,
        }
        try:
            await ctx.audit_sink(record)
        except Exception as exc:  # noqa: BLE001 - audit must never break chat
            _log.warning("delegation audit_sink failed (ignored): %s", exc)

    # ---- Tool-result payload the CFO synthesizes from. ----
    if result.get("ok"):
        return {
            "ok": True,
            "specialist": specialist,
            "analysis": result["analysis"],
            "consults_remaining": ctx.budget_remaining(),
        }
    return {
        "ok": False,
        "specialist": specialist,
        "error": result.get("error") or "consult failed",
        # Hand back any partial analysis so the CFO can still use it if present.
        "partial_analysis": result.get("analysis") or "",
        "consults_remaining": ctx.budget_remaining(),
        "degrade_note": (
            "Could not get a usable answer from this specialist. Answer the user "
            "from the data you have and explicitly note the gap (e.g. "
            "'pending the X specialist's review')."
        ),
    }


# ---------------------------------------------------------------------------
# Audit sink — persist each delegation as a decision_audit row.
# ---------------------------------------------------------------------------
#
# "Silent" is UX-silent, not unaudited. Each consult writes one decision_audit
# row with tool_name='consult_specialist' and model_version=<cfo agent id> so
# it surfaces in the AI-activity timeline (app/api/ai_activity.py already treats
# any row with a non-empty model_version as an AI action; consult_specialist is
# classified challenge-only / non-reversible there).


async def _write_consult_audit(
    *,
    sessionmaker: Any,
    workspace_id: str,
    agent_session_id: str | None,
    record: dict[str, Any],
) -> None:
    """Append one decision_audit row for a delegation. Best-effort.

    Runs in its own RLS-pinned ``workspace_session`` so it commits independently
    of the streaming response (which holds no DB transaction). Any failure is
    logged and swallowed — auditing must never break the chat stream.
    """
    # Local imports keep this module importable in minimal/test envs that don't
    # have the DB layer wired (the chat layer passes a real sessionmaker).
    from sqlalchemy import text

    from app.workspace_session import workspace_session

    request_payload = {
        "specialist": record.get("specialist"),
        "question": record.get("question"),
        "consult_index": record.get("consult_index"),
        "actor_role": "agent",
    }
    response_payload = {
        "ok": record.get("ok"),
        "result_summary": record.get("result_summary"),
        "error": record.get("error"),
        "specialist_tool_calls": record.get("specialist_tool_calls", []),
        "input_tokens": record.get("input_tokens", 0),
        "output_tokens": record.get("output_tokens", 0),
    }
    reasoning = (
        f"CFO consulted {record.get('specialist')} — "
        f"{record.get('result_summary') or record.get('error') or ''}"
    )
    outcome = "consulted" if record.get("ok") else "consult_failed"

    async with workspace_session(sessionmaker, workspace_id) as session:
        await session.execute(
            text(
                """
                INSERT INTO decision_audit (
                    id, workspace_id, entity_id, agent_session_id,
                    tool_name, request_payload, response_payload,
                    reasoning_trace, source_document_id,
                    model_version, outcome
                ) VALUES (
                    :id, :wid, :eid, :sid,
                    :tool_name, CAST(:req AS jsonb), CAST(:resp AS jsonb),
                    :reasoning, NULL,
                    :model_version, :outcome
                )
                """
            ),
            {
                "id": str(uuid4()),
                "wid": workspace_id,
                "eid": record.get("entity_id"),
                "sid": agent_session_id,
                "tool_name": CONSULT_TOOL_NAME,
                "req": json.dumps(request_payload),
                "resp": json.dumps(response_payload),
                "reasoning": reasoning,
                "model_version": record.get("cfo_agent_id") or "cfo-agent",
                "outcome": outcome,
            },
        )


def build_cfo_delegation(
    *,
    sessionmaker: Any,
    workspace_id: str,
    entity_id: str,
    user_uc_uid: str,
    user_bearer_token: str,
    tax_year: int | None,
    claims: dict[str, Any] | None = None,
    cfo_agent_id: str = "cfo-agent",
    agent_session_id: str | None = None,
    backend: str | None = None,
    openrouter_base_url: str | None = None,
    openrouter_api_key: str | None = None,
    mcp_url: str | None = None,
    consult_model: str | None = None,
) -> DelegationContext:
    """Construct a per-turn ``DelegationContext`` for the CFO chat turn.

    Wires an ``audit_sink`` that persists each delegation to ``decision_audit``
    via its own RLS-pinned ``workspace_session`` (committed independently of the
    SSE stream). Scope is carried VERBATIM — this factory never widens it.
    """

    async def _sink(rec: dict[str, Any]) -> None:
        try:
            await _write_consult_audit(
                sessionmaker=sessionmaker,
                workspace_id=workspace_id,
                agent_session_id=agent_session_id,
                record=rec,
            )
        except Exception as exc:  # noqa: BLE001 - audit must never break chat
            _log.warning("consult audit write failed (ignored): %s", exc)

    return DelegationContext(
        workspace_id=workspace_id,
        entity_id=entity_id,
        user_uc_uid=user_uc_uid,
        user_bearer_token=user_bearer_token,
        tax_year=tax_year,
        claims=claims,
        backend=backend,
        openrouter_base_url=openrouter_base_url,
        openrouter_api_key=openrouter_api_key,
        mcp_url=mcp_url,
        consult_model=consult_model,
        cfo_agent_id=cfo_agent_id,
        audit_sink=_sink,
    )
