"""Streaming chat runner.

Drives the OpenRouter / Anthropic SDK on behalf of a single user turn and
yields **chat events** as small dicts the FastAPI layer turns into SSE
frames. Mirrors the existing CLI specialist runners (form_1120_preparer
etc.) but with three differences:

1. Streams the model output token-by-token instead of buffering.
2. Uses ``anthropic/claude-sonnet-4.5`` via OpenRouter by default (per the
   in-app chat spec — 4.6 was flaky earlier this session).
3. Is cancellable: if the caller stops consuming the generator, the
   underlying SDK / MCP session is torn down via the async-context-manager
   exit path.

Tool calls are dispatched against the existing MCP server at
``http://localhost:8000/mcp/`` using the user's own Keycloak bearer token
(threaded through from the FastAPI request), so the MCP auth middleware
sees exactly the same caller as any direct REST API request would.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# Soft import — both backends are installed in the production app container
# (Dockerfile installs ``.[tax-agents]``). Tests + local dev may not have
# either; defer the ImportError to the actual call site.
try:
    import openai  # type: ignore
except ImportError:  # pragma: no cover
    openai = None  # type: ignore[assignment]


_log = logging.getLogger("acctops.chat.runner")


# ---------------------------------------------------------------------------
# Defaults — overridable via env vars in app/main.py's container.
# ---------------------------------------------------------------------------

# Per the in-app chat spec: 4.6 was flaky in earlier sessions, so the chat
# defaults to 4.5. Override per session if needed.
DEFAULT_MODEL = os.environ.get(
    "CHAT_DEFAULT_MODEL", "anthropic/claude-sonnet-4.5"
)
DEFAULT_BACKEND = os.environ.get("CHAT_DEFAULT_BACKEND", "openrouter")
DEFAULT_OPENROUTER_URL = os.environ.get(
    "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
)

# MCP server — defaults to the in-container address. The auth middleware
# validates the user's bearer token, so the model sees only data the user
# is allowed to see (defense in depth on top of the per-persona tool
# allow-list).
DEFAULT_MCP_URL = os.environ.get(
    "ACCTOPS_MCP_URL", "http://localhost:8000/mcp/"
)

# Hard cap on tool-call iterations per user turn. Mirrors the CLI runners'
# loop cap so a misbehaving model can't burn unbounded tokens.
MAX_TOOL_ITERATIONS = int(os.environ.get("CHAT_MAX_TOOL_ITERATIONS", "12"))

# Max tokens per assistant turn. Conservative — chat answers should be focused.
MAX_TOKENS_PER_TURN = int(os.environ.get("CHAT_MAX_TOKENS_PER_TURN", "4096"))


# ---------------------------------------------------------------------------
# Chat events — emitted from ``stream_assistant_turn``. Each event is a
# small JSON-serializable dict; the FastAPI layer encodes them as SSE.
# ---------------------------------------------------------------------------

#   {"type": "text_delta", "text": "..."}              token-level append
#   {"type": "tool_call",  "id": "...", "name": "...", "args": {...}}
#   {"type": "tool_result","id": "...", "name": "...", "result": <json>,
#                          "artifact_kind": "markdown" | "json" | "text" | null,
#                          "artifact_size": <int>}
#   {"type": "delegation","id": "...", "specialist": "...", "ok": bool,
#                          "summary": "..."}   <-- UX-silent CFO→specialist
#                          consult; the chat layer persists it for the audit
#                          trail but does NOT render an inline tool card.
#   {"type": "stop", "stop_reason": "end_turn"|"max_tokens"|"tool_iteration_cap",
#                    "input_tokens": <int>, "output_tokens": <int>,
#                    "duration_ms": <int>, "model": "...", "backend": "..."}
#   {"type": "error", "message": "..."}

# Artifact detection — any tool result that returns a long markdown / json
# blob is flagged for the canvas pane.
_ARTIFACT_TEXT_THRESHOLD = 1024  # bytes


# ---------------------------------------------------------------------------
# Internal mutable state for an in-flight assistant turn
# ---------------------------------------------------------------------------


@dataclass
class _TurnState:
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cancelled: bool = False


# ---------------------------------------------------------------------------
# OpenAI tool-schema conversion (same as the CLI runners) and artifact sniffing
# ---------------------------------------------------------------------------


def _mcp_tool_to_openai_function(tool: Any) -> dict[str, Any]:
    """Convert an MCP Tool descriptor to OpenAI function-calling schema."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": (
                tool.inputSchema
                if tool.inputSchema
                else {"type": "object", "properties": {}}
            ),
        },
    }


def _flatten_mcp_result(mcp_result: Any) -> dict[str, Any]:
    """Turn an MCP CallToolResult into a JSON-serializable dict.

    Each content item in mcp_result.content is one of TextContent /
    ImageContent / EmbeddedResource. We call .model_dump() when available
    and fall back to str(item) so the wire format is always valid JSON.
    """
    items: list[Any] = []
    for item in getattr(mcp_result, "content", []) or []:
        if hasattr(item, "model_dump"):
            items.append(item.model_dump())
        else:
            items.append(str(item))
    return {"content": items}


def _classify_artifact(result_payload: dict[str, Any]) -> tuple[str | None, int]:
    """Detect a substantial artifact in a tool result for the canvas pane.

    Returns ``(artifact_kind, artifact_size_bytes)``. ``artifact_kind`` is
    one of ``markdown``, ``json``, ``text``, or None for "no notable
    artifact". We unwrap the standard MCP text-content shape and look at
    the longest text item.
    """
    longest_text = ""
    for item in result_payload.get("content", []):
        text = None
        if isinstance(item, dict):
            text = item.get("text")
        elif isinstance(item, str):
            text = item
        if isinstance(text, str) and len(text) > len(longest_text):
            longest_text = text
    if not longest_text:
        return (None, 0)
    size = len(longest_text)
    if size < _ARTIFACT_TEXT_THRESHOLD:
        return (None, size)
    # Crude format sniff — first non-blank chars decide.
    stripped = longest_text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        return ("json", size)
    # Markdown signal: starts with # heading, has multiple ## sections, or
    # has fenced code blocks / pipe tables. Otherwise fall back to plain text.
    head = stripped[:512]
    if (
        head.startswith("#")
        or "\n## " in stripped[:2048]
        or "\n```" in stripped[:2048]
        or "\n|" in stripped[:2048]
    ):
        return ("markdown", size)
    return ("text", size)


# ---------------------------------------------------------------------------
# Main entry point: stream one assistant turn (user prompt → assistant reply)
# ---------------------------------------------------------------------------


async def stream_assistant_turn(
    *,
    system_prompt: str,
    history: list[dict[str, Any]],
    user_message: str,
    allowed_mcp_tools: tuple[str, ...],
    user_bearer_token: str,
    model: str = DEFAULT_MODEL,
    backend: str = DEFAULT_BACKEND,
    openrouter_base_url: str = DEFAULT_OPENROUTER_URL,
    openrouter_api_key: str | None = None,
    mcp_url: str = DEFAULT_MCP_URL,
    max_iterations: int = MAX_TOOL_ITERATIONS,
    max_tokens: int = MAX_TOKENS_PER_TURN,
    cancel_check: Any = None,  # callable[[], bool] | None — polled between deltas
    delegation: Any = None,  # DelegationContext | None — enables consult_specialist
) -> AsyncIterator[dict[str, Any]]:
    """Stream chat events for one assistant turn.

    Args:
        system_prompt:      Full system prompt (persona + chat preamble).
        history:            Prior turns. Each item: {"role": "user"|"assistant"|"tool",
                            "content": str | list[dict]}. The assistant items
                            may include ``tool_calls`` if a previous turn used
                            tools (we re-hydrate them into the OpenAI format).
        user_message:       The new user prompt to append.
        allowed_mcp_tools:  Per-persona allow-list intersected with the MCP
                            server's tool list — defense in depth.
        user_bearer_token:  The caller's Keycloak access token. Forwarded as
                            the MCP server's Authorization header so the MCP
                            sees the SAME caller as a direct API request.
        model:              Model ID — default ``anthropic/claude-sonnet-4.5``.
        backend:            "openrouter" (default) | "openai". Both speak the
                            OpenAI Chat Completions API.
        openrouter_base_url: Base URL for the OpenAI-compatible endpoint.
        openrouter_api_key: API key. Falls back to env OPENROUTER_API_KEY.
        mcp_url:            MCP endpoint URL.
        max_iterations:     Hard cap on tool-call iterations per turn.
        max_tokens:         Max output tokens per LLM call.
        cancel_check:       Optional callable; when it returns True, the loop
                            stops cleanly and emits a {"type": "stop",
                            "stop_reason": "cancelled"} event.
        delegation:         Optional ``agents.chat.delegation.DelegationContext``.
                            When supplied, the CFO persona is given a LOCAL
                            ``consult_specialist`` tool (NOT an MCP tool) that
                            silently consults a specialist's chat persona and
                            folds the analysis back in. Depth-1 only: the
                            consult is run WITHOUT a delegation context, so a
                            consulted specialist can never delegate further.
                            Pass None (the default) for every persona except the
                            CFO and for every inner consult turn.

    Yields:
        Chat events (dicts). Always ends with one ``{"type": "stop"|"error"}``.
    """
    if openai is None:
        yield {"type": "error", "message": "openai SDK not installed in this container"}
        return

    api_key = openrouter_api_key or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key and backend == "openrouter":
        yield {
            "type": "error",
            "message": (
                "OPENROUTER_API_KEY is not set. Set it in the app container's "
                "environment to enable the in-app chat."
            ),
        }
        return

    # Late imports — only needed when chat is actually used.
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError:
        yield {
            "type": "error",
            "message": "mcp[cli] SDK not installed in this container",
        }
        return

    state = _TurnState()
    state.messages.append({"role": "system", "content": system_prompt})

    # Re-hydrate prior history. Assistant turns with tool calls are stored
    # as content=[{type:'text'|'tool_call'|...}]; flatten back to OpenAI
    # message shape for the next call. We only need the most-recent assistant
    # text + matching tool_call/result pairs; older turns can be plain text.
    for h in history:
        role = h.get("role")
        content = h.get("content")
        if role == "user" and isinstance(content, str):
            state.messages.append({"role": "user", "content": content})
        elif role == "assistant" and isinstance(content, str):
            state.messages.append({"role": "assistant", "content": content})
        # For assistant turns with tool calls we drop the tool_call/result
        # detail and keep only the final text — the next turn re-builds tool
        # calls from scratch if needed. This keeps the prompt small + avoids
        # tool_call_id mismatches across SDK versions.
        elif role == "assistant" and isinstance(content, dict):
            text = content.get("text")
            if text:
                state.messages.append({"role": "assistant", "content": text})

    state.messages.append({"role": "user", "content": user_message})

    start_ts = time.monotonic()

    # Open MCP client + LLM client concurrently.
    headers = {"Authorization": f"Bearer {user_bearer_token}"}
    stop_event: dict[str, Any] = {
        "type": "stop",
        "stop_reason": "end_turn",
        "model": model,
        "backend": backend,
    }

    try:
        async with streamablehttp_client(mcp_url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools_response = await session.list_tools()
                allowed = set(allowed_mcp_tools)
                tools = [t for t in tools_response.tools if t.name in allowed]
                openai_tools = [_mcp_tool_to_openai_function(t) for t in tools]

                # ---- Delegation (agents-as-tools) ----
                # When a DelegationContext is supplied (CFO turn only), expose
                # the LOCAL consult_specialist tool alongside the MCP tools.
                # It is dispatched in-process (see the tool loop below), NOT to
                # the MCP server. Guard: only enable it when there's budget AND
                # the context isn't itself a depth-1 inner consult (callers pass
                # delegation=None for inner turns, so this is belt-and-suspenders).
                delegation_enabled = False
                _CONSULT_TOOL_NAME = None
                _dispatch_consult = None
                if delegation is not None:
                    try:
                        from agents.chat.delegation import (
                            CONSULT_TOOL_NAME as _CONSULT_TOOL_NAME,
                            consult_specialist_tool_schema,
                            dispatch_consult as _dispatch_consult,
                        )

                        if delegation.budget_remaining() > 0:
                            openai_tools.append(consult_specialist_tool_schema())
                            allowed.add(_CONSULT_TOOL_NAME)
                            delegation_enabled = True
                    except Exception as exc:  # noqa: BLE001 - never break chat
                        _log.warning(
                            "delegation tool unavailable (ignored): %s", exc
                        )

                client = openai.AsyncOpenAI(
                    base_url=openrouter_base_url,
                    api_key=api_key,
                    # OpenRouter recommends a referrer / app title for analytics.
                    default_headers={
                        "HTTP-Referer": os.environ.get(
                            "PUBLIC_BASE_URL",
                            "https://accounting.magicunicorn.dev",
                        ),
                        "X-Title": "Accounting-Ops Chat",
                    },
                )

                for iteration in range(max_iterations):
                    if cancel_check is not None and cancel_check():
                        state.cancelled = True
                        stop_event["stop_reason"] = "cancelled"
                        break

                    kwargs: dict[str, Any] = {
                        "model": model,
                        "messages": state.messages,
                        "max_tokens": max_tokens,
                        "stream": True,
                    }
                    if openai_tools:
                        kwargs["tools"] = openai_tools
                        kwargs["tool_choice"] = "auto"

                    # ----- Streamed chat completion -----
                    text_buf: list[str] = []
                    tool_calls_buf: dict[int, dict[str, Any]] = {}
                    finish_reason: str | None = None
                    usage_snapshot: Any = None

                    try:
                        stream = await client.chat.completions.create(**kwargs)
                    except Exception as exc:
                        _log.warning("LLM request failed: %s", exc)
                        yield {"type": "error", "message": f"LLM request failed: {exc}"}
                        return

                    async for chunk in stream:
                        if cancel_check is not None and cancel_check():
                            state.cancelled = True
                            stop_event["stop_reason"] = "cancelled"
                            break
                        if not chunk.choices:
                            # OpenRouter sometimes emits usage-only frames at end.
                            usage = getattr(chunk, "usage", None)
                            if usage:
                                usage_snapshot = usage
                            continue
                        choice = chunk.choices[0]
                        delta = choice.delta
                        if delta is None:
                            continue

                        # Text content delta.
                        if delta.content:
                            text_buf.append(delta.content)
                            yield {"type": "text_delta", "text": delta.content}

                        # Tool-call delta (OpenAI streams partial argument JSON).
                        tc_list = getattr(delta, "tool_calls", None) or []
                        for tc_delta in tc_list:
                            idx = getattr(tc_delta, "index", 0)
                            slot = tool_calls_buf.setdefault(
                                idx,
                                {"id": None, "name": None, "args_buf": ""},
                            )
                            if getattr(tc_delta, "id", None):
                                slot["id"] = tc_delta.id
                            fn = getattr(tc_delta, "function", None)
                            if fn is not None:
                                if getattr(fn, "name", None):
                                    slot["name"] = fn.name
                                if getattr(fn, "arguments", None):
                                    slot["args_buf"] += fn.arguments

                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
                        # Capture usage where available (some OpenRouter providers stamp it on the final chunk).
                        usage = getattr(chunk, "usage", None)
                        if usage:
                            usage_snapshot = usage

                    if state.cancelled:
                        break

                    if usage_snapshot is not None:
                        try:
                            state.input_tokens = int(
                                getattr(usage_snapshot, "prompt_tokens", 0) or 0
                            )
                            state.output_tokens = int(
                                getattr(usage_snapshot, "completion_tokens", 0) or 0
                            )
                        except Exception:
                            pass

                    text_response = "".join(text_buf)

                    # ----- No tool calls → final assistant turn -----
                    if not tool_calls_buf or finish_reason != "tool_calls":
                        if finish_reason == "length":
                            stop_event["stop_reason"] = "max_tokens"
                        break

                    # ----- Dispatch tool calls -----
                    # Append the assistant turn (with structured tool_calls) so
                    # the model sees the same structure on the next call.
                    rebuilt_tool_calls = []
                    for idx in sorted(tool_calls_buf.keys()):
                        slot = tool_calls_buf[idx]
                        if not slot["name"]:
                            continue
                        rebuilt_tool_calls.append({
                            "id": slot["id"] or f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": slot["name"],
                                "arguments": slot["args_buf"] or "{}",
                            },
                        })
                    state.messages.append({
                        "role": "assistant",
                        "content": text_response or None,
                        "tool_calls": rebuilt_tool_calls,
                    })

                    for tc in rebuilt_tool_calls:
                        if cancel_check is not None and cancel_check():
                            state.cancelled = True
                            stop_event["stop_reason"] = "cancelled"
                            break
                        name = tc["function"]["name"]
                        try:
                            args = json.loads(tc["function"]["arguments"] or "{}")
                        except json.JSONDecodeError:
                            args = {}

                        # ---- Local delegation tool (UX-silent) ----
                        # consult_specialist is handled in-process, never sent
                        # to the MCP server. It does NOT emit a visible
                        # tool_call/tool_result card; instead a single
                        # `delegation` event is yielded so the chat layer can
                        # persist it for the audit trail without showing the
                        # user an inline "routing to X".
                        if delegation_enabled and name == _CONSULT_TOOL_NAME:
                            state.tool_call_count += 1
                            try:
                                consult_result = await _dispatch_consult(
                                    args=args, ctx=delegation
                                )
                            except Exception as exc:  # noqa: BLE001
                                # dispatch_consult should never raise, but guard
                                # so the chat stream is never broken.
                                _log.warning("consult dispatch failed: %s", exc)
                                consult_result = {
                                    "ok": False,
                                    "specialist": args.get("specialist"),
                                    "error": f"consult failed: {exc}",
                                }
                            # Feed the specialist analysis back to the CFO as a
                            # normal tool result so it can synthesize from it.
                            state.messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": json.dumps(consult_result, default=str),
                            })
                            # Quiet, audit-only event (no inline card).
                            yield {
                                "type": "delegation",
                                "id": tc["id"],
                                "specialist": consult_result.get("specialist"),
                                "ok": bool(consult_result.get("ok")),
                                "question": args.get("question"),
                                "summary": (
                                    consult_result.get("analysis")
                                    or consult_result.get("error")
                                    or ""
                                ),
                            }
                            continue

                        # Surface the call to the UI (collapsible card).
                        yield {
                            "type": "tool_call",
                            "id": tc["id"],
                            "name": name,
                            "args": args,
                        }

                        if name not in allowed:
                            # Should be impossible (tool list was pre-filtered)
                            # but defense in depth.
                            err = {
                                "error": (
                                    f"tool {name!r} not allowed for this persona"
                                )
                            }
                            state.messages.append({
                                "role": "tool",
                                "tool_call_id": tc["id"],
                                "content": json.dumps(err),
                            })
                            yield {
                                "type": "tool_result",
                                "id": tc["id"],
                                "name": name,
                                "result": err,
                                "artifact_kind": None,
                                "artifact_size": 0,
                            }
                            state.tool_call_count += 1
                            continue

                        state.tool_call_count += 1
                        try:
                            mcp_result = await session.call_tool(name, args)
                            result_payload = _flatten_mcp_result(mcp_result)
                        except Exception as exc:
                            result_payload = {"error": str(exc)}

                        artifact_kind, artifact_size = _classify_artifact(
                            result_payload
                        )

                        state.messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": json.dumps(result_payload),
                        })

                        yield {
                            "type": "tool_result",
                            "id": tc["id"],
                            "name": name,
                            "result": result_payload,
                            "artifact_kind": artifact_kind,
                            "artifact_size": artifact_size,
                        }

                    if state.cancelled:
                        break

                    # Loop: feed the tool results back into the LLM.

                else:
                    # Hit the iteration cap.
                    stop_event["stop_reason"] = "tool_iteration_cap"

    except Exception as exc:  # pragma: no cover
        _log.exception("chat runner failed")
        yield {"type": "error", "message": f"chat runner failed: {exc}"}
        return

    stop_event["input_tokens"] = state.input_tokens
    stop_event["output_tokens"] = state.output_tokens
    stop_event["tool_call_count"] = state.tool_call_count
    stop_event["duration_ms"] = int((time.monotonic() - start_ts) * 1000)
    yield stop_event
