"""In-app chat (Claude.ai-style) endpoints.

Backs the ``/chat`` web surface. Each conversation is one row in
``agent_session`` (extended with chat columns in migration 0011); every
turn is one row in ``chat_message``.

Routes (all gated by uchub bearer, accountant floor for create/append,
viewer floor for read):

  GET   /api/chat/personas
        — registered chat personas (slug + display name + 1-line description).
          No workspace context required.

  POST  /api/workspaces/{wid}/chat/sessions
        — create a new chat session bound to (workspace, entity,
          agent_persona, optional tax_year).
          Body: {entity_id, agent_persona, tax_year?, title?}
          Returns the session row.

  GET   /api/workspaces/{wid}/chat/sessions?entity_id=&limit=
        — list the calling user's chat sessions (sidebar surface).

  GET   /api/workspaces/{wid}/chat/sessions/{sid}
        — load session header + full message history.

  POST  /api/workspaces/{wid}/chat/sessions/{sid}/messages
        — append a user message and stream the assistant response as SSE.
          Body: {text, cancel_token?}
          Response: text/event-stream with chat events (see agents/chat/runner.py).

  POST  /api/workspaces/{wid}/chat/sessions/{sid}/cancel
        — request the in-flight assistant turn for this session be cancelled.
          Idempotent; safe to call when no stream is running.

  POST  /api/workspaces/{wid}/chat/sessions/{sid}/save-as-memo
        — promote a specific assistant message to a tax_review_queue memo.
          Body: {message_id, topic?}
          Returns the new memo id; closes the loop with the existing CPA
          review surface.

Entity-scope (BOLA): every session row carries entity_id; reads and writes
both enforce assert_entity_in_scope.

SSE protocol:
  - Each chat event is JSON, encoded as one SSE frame:
        event: text_delta | tool_call | tool_result | stop | error | session
        data:  <json>
  - The first frame is always ``event: session`` carrying the message id
    being streamed (so the client can correlate cancel calls and persist
    the assistant message on stream end).
  - The last frame is always ``event: stop`` or ``event: error``.
  - Heartbeat: a comment line (``: keepalive\n\n``) is emitted every 15s
    to keep proxies (Traefik) from dropping idle SSE connections.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text

from agents.chat import get_persona, list_personas
from agents.chat.persona_registry import DEFAULT_PERSONA_SLUG
from agents.chat.runner import (
    DEFAULT_BACKEND,
    DEFAULT_MODEL,
    stream_assistant_turn,
)
from app.auth import AuthContext, validate_bearer_token
from app.authz import assert_entity_in_scope
from app.db import SessionLocal
from app.tenancy import WorkspaceContext, require_role, resolve_workspace
from app.workspace_session import workspace_session


router = APIRouter(tags=["chat"])
_log = logging.getLogger("acctops.api.chat")


# ---------------------------------------------------------------------------
# Cancellation registry — a session-id-keyed flag the streaming runner polls.
# Lives in-process: a chat in the middle of streaming on box A can be
# cancelled by a request that lands on the same box. The web surface always
# targets the same backend behind Traefik so this is fine.
# ---------------------------------------------------------------------------

_CANCELLED: set[str] = set()


def _request_cancel(session_id: str) -> None:
    _CANCELLED.add(session_id)


def _clear_cancel(session_id: str) -> None:
    _CANCELLED.discard(session_id)


def _is_cancelled(session_id: str) -> bool:
    return session_id in _CANCELLED


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    entity_id: UUID
    agent_persona: str | None = None  # defaults to CPA Tax Resolver
    tax_year: int | None = None
    title: str | None = None


class SendMessageRequest(BaseModel):
    text: str
    # Optional model override per session (advanced users only). None →
    # use the runner default (anthropic/claude-sonnet-4.5).
    model: str | None = None


class SaveAsMemoRequest(BaseModel):
    message_id: UUID
    topic: str | None = None


# ---------------------------------------------------------------------------
# GET /personas — no workspace context required
# ---------------------------------------------------------------------------


@router.get("/api/chat/personas")
async def get_chat_personas(
    _auth: AuthContext = Depends(validate_bearer_token),
) -> dict:
    """List registered chat personas for the agent-picker UI."""
    return {
        "default": DEFAULT_PERSONA_SLUG,
        "personas": list_personas(),
    }


# ---------------------------------------------------------------------------
# Helpers — load + serialize a chat session
# ---------------------------------------------------------------------------


async def _load_session_row(session, session_id: UUID) -> dict | None:
    row = (
        await session.execute(
            text(
                """
                SELECT
                    a.id::text          AS id,
                    a.workspace_id::text AS workspace_id,
                    a.entity_id::text   AS entity_id,
                    a.agent_id          AS agent_id,
                    a.user_uc_uid       AS user_uc_uid,
                    a.title             AS title,
                    a.agent_persona     AS agent_persona,
                    a.tax_year          AS tax_year,
                    a.started_at        AS started_at,
                    a.last_activity_at  AS last_activity_at,
                    a.ended_at          AS ended_at,
                    a.cancelled_at      AS cancelled_at,
                    e.display_name      AS entity_display_name,
                    e.slug              AS entity_slug
                FROM agent_session a
                LEFT JOIN entity e ON e.id = a.entity_id
                WHERE a.id = :sid
                LIMIT 1
                """
            ),
            {"sid": str(session_id)},
        )
    ).mappings().first()
    return dict(row) if row else None


def _serialize_message_row(row: Any) -> dict:
    d = dict(row)
    # JSONB columns come back as already-parsed dicts via asyncpg/sqlalchemy;
    # double-encode-safe by checking type.
    if isinstance(d.get("content"), (bytes, bytearray)):
        d["content"] = json.loads(d["content"].decode("utf-8"))
    elif isinstance(d.get("content"), str):
        try:
            d["content"] = json.loads(d["content"])
        except json.JSONDecodeError:
            pass
    return d


# ---------------------------------------------------------------------------
# POST /sessions — create a new chat session
# ---------------------------------------------------------------------------


@router.post(
    "/api/workspaces/{workspace_id}/chat/sessions",
    status_code=201,
)
async def create_chat_session(
    workspace_id: UUID,
    body: CreateSessionRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Create a new chat session.

    Role floor: viewer (anyone in the workspace can chat — write tools the
    chat exposes still enforce their own per-tool role floor).

    Entity-scope: enforced via assert_entity_in_scope.
    """
    require_role(workspace, "viewer")
    assert_entity_in_scope(workspace, body.entity_id)

    persona_slug = (body.agent_persona or DEFAULT_PERSONA_SLUG).strip()
    persona = get_persona(persona_slug)
    if persona is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown agent_persona {persona_slug!r}. "
                f"Valid slugs: {', '.join(p['slug'] for p in list_personas())}"
            ),
        )

    title = (body.title or persona.display_name).strip()[:240]

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        # Verify the entity exists in this workspace + the user can see it.
        ent_row = (
            await session.execute(
                text("SELECT id FROM entity WHERE id = :eid"),
                {"eid": str(body.entity_id)},
            )
        ).first()
        if ent_row is None:
            raise HTTPException(status_code=404, detail="Entity not found")

        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO agent_session (
                        id, workspace_id, entity_id, agent_id, user_uc_uid,
                        scope, started_at, last_activity_at,
                        title, agent_persona, tax_year
                    ) VALUES (
                        gen_random_uuid(), :wid, :eid, :agent_id, :uid,
                        CAST(:scope AS jsonb), now(), now(),
                        :title, :persona, :tax_year
                    )
                    RETURNING id::text AS id
                    """
                ),
                {
                    "wid": str(workspace.workspace_id),
                    "eid": str(body.entity_id),
                    "agent_id": persona_slug,
                    "uid": workspace.user_uc_uid,
                    "scope": json.dumps({"kind": "chat"}),
                    "title": title,
                    "persona": persona_slug,
                    "tax_year": body.tax_year,
                },
            )
        ).mappings().one()

        loaded = await _load_session_row(session, UUID(row["id"]))

    return {"session": loaded, "messages": []}


# ---------------------------------------------------------------------------
# GET /sessions — list (sidebar)
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/chat/sessions")
async def list_chat_sessions(
    workspace_id: UUID,
    entity_id: UUID | None = None,
    limit: int = 50,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """List the calling user's chat sessions in this workspace.

    Sorted by last_activity_at DESC. Entity-scope enforced. The sidebar
    typically shows the current user's own sessions; we filter to that.
    """
    require_role(workspace, "viewer")
    scope_restriction = assert_entity_in_scope(workspace, entity_id)

    limit = max(1, min(limit, 200))

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        where = ["a.user_uc_uid = :uid"]
        params: dict[str, Any] = {
            "uid": workspace.user_uc_uid,
            "limit": limit,
        }
        if entity_id:
            where.append("a.entity_id = :eid")
            params["eid"] = str(entity_id)
        if scope_restriction is not None:
            where.append("a.entity_id = ANY(:entity_scope)")
            params["entity_scope"] = [str(e) for e in scope_restriction]

        where_sql = " AND ".join(where)
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT
                        a.id::text          AS id,
                        a.entity_id::text   AS entity_id,
                        e.display_name      AS entity_display_name,
                        e.slug              AS entity_slug,
                        a.title             AS title,
                        a.agent_persona     AS agent_persona,
                        a.tax_year          AS tax_year,
                        a.started_at        AS started_at,
                        a.last_activity_at  AS last_activity_at
                    FROM agent_session a
                    LEFT JOIN entity e ON e.id = a.entity_id
                    WHERE {where_sql}
                      AND a.agent_persona IS NOT NULL
                    ORDER BY a.last_activity_at DESC NULLS LAST, a.started_at DESC
                    LIMIT :limit
                    """
                ),
                params,
            )
        ).mappings().all()

    return {"sessions": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# GET /sessions/{sid} — load + history
# ---------------------------------------------------------------------------


@router.get("/api/workspaces/{workspace_id}/chat/sessions/{session_id}")
async def get_chat_session(
    workspace_id: UUID,
    session_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Load one chat session including its full message history."""
    require_role(workspace, "viewer")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        loaded = await _load_session_row(session, session_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Chat session not found")

        assert_entity_in_scope(workspace, UUID(loaded["entity_id"]))

        # Owner / admin can see anyone's session; everyone else only their own.
        if (
            loaded["user_uc_uid"]
            and loaded["user_uc_uid"] != workspace.user_uc_uid
            and workspace.role not in ("owner", "admin")
        ):
            raise HTTPException(status_code=403, detail="Not your session")

        msgs = (
            await session.execute(
                text(
                    """
                    SELECT
                        id::text          AS id,
                        session_id::text  AS session_id,
                        role,
                        content,
                        submitter_uc_uid,
                        model,
                        backend,
                        input_tokens,
                        output_tokens,
                        duration_ms,
                        created_at
                    FROM chat_message
                    WHERE session_id = :sid
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"sid": str(session_id)},
            )
        ).mappings().all()

    return {
        "session": loaded,
        "messages": [_serialize_message_row(m) for m in msgs],
    }


# ---------------------------------------------------------------------------
# POST /sessions/{sid}/messages — append + stream
# ---------------------------------------------------------------------------


def _sse_frame(event: str, data: dict | str) -> bytes:
    """Encode one SSE frame. Supports multi-line data via newline split."""
    if isinstance(data, dict):
        payload = json.dumps(data, separators=(",", ":"), default=str)
    else:
        payload = data
    # SSE: every newline in the data needs a fresh "data:" prefix.
    data_lines = "\n".join(f"data: {line}" for line in payload.splitlines() or [""])
    return f"event: {event}\n{data_lines}\n\n".encode("utf-8")


@router.post("/api/workspaces/{workspace_id}/chat/sessions/{session_id}/messages")
async def send_chat_message(
    workspace_id: UUID,
    session_id: UUID,
    body: SendMessageRequest,
    request: Request,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> StreamingResponse:
    """Append the user message and stream the assistant response as SSE.

    Role floor: viewer (write tools still gate per-tool).

    The SSE stream:
      - First frame: ``event: session`` with {session_id, user_message_id,
        assistant_message_id} so the client can correlate.
      - Followed by ``text_delta`` / ``tool_call`` / ``tool_result`` frames.
      - Always terminated by ``stop`` or ``error``.
      - Heartbeat: ``: keepalive\\n\\n`` every 15s.
    """
    require_role(workspace, "viewer")

    # Load + authorize session.
    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        loaded = await _load_session_row(session, session_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Chat session not found")

        assert_entity_in_scope(workspace, UUID(loaded["entity_id"]))

        if (
            loaded["user_uc_uid"]
            and loaded["user_uc_uid"] != workspace.user_uc_uid
            and workspace.role not in ("owner", "admin")
        ):
            raise HTTPException(status_code=403, detail="Not your session")

        persona_slug = loaded.get("agent_persona") or DEFAULT_PERSONA_SLUG
        persona = get_persona(persona_slug)
        if persona is None:
            raise HTTPException(
                status_code=409,
                detail=f"Session bound to unknown persona {persona_slug!r}",
            )

        entity_id = loaded["entity_id"]
        tax_year = loaded.get("tax_year")

        # Load prior history (compact: only the keys the runner cares about).
        history_rows = (
            await session.execute(
                text(
                    """
                    SELECT role, content
                    FROM chat_message
                    WHERE session_id = :sid
                    ORDER BY created_at ASC, id ASC
                    """
                ),
                {"sid": str(session_id)},
            )
        ).mappings().all()

        # Persist the user turn FIRST so that even if the model never streams
        # anything, the conversation reflects what the user said.
        user_msg = (
            await session.execute(
                text(
                    """
                    INSERT INTO chat_message (
                        id, workspace_id, entity_id, session_id, role,
                        content, submitter_uc_uid, created_at
                    ) VALUES (
                        gen_random_uuid(), :wid, :eid, :sid, 'user',
                        CAST(:content AS jsonb), :uid, now()
                    )
                    RETURNING id::text AS id, created_at
                    """
                ),
                {
                    "wid": str(workspace.workspace_id),
                    "eid": entity_id,
                    "sid": str(session_id),
                    "content": json.dumps({"type": "text", "text": body.text}),
                    "uid": workspace.user_uc_uid,
                },
            )
        ).mappings().one()

        # If the session is fresh and untitled, derive a title from the prompt.
        if not loaded.get("title") or loaded["title"] == persona.display_name:
            derived = (body.text or "").strip().splitlines()[0][:80] or persona.display_name
            await session.execute(
                text(
                    """
                    UPDATE agent_session
                    SET title = :title, last_activity_at = now()
                    WHERE id = :sid
                    """
                ),
                {"title": derived, "sid": str(session_id)},
            )
        else:
            await session.execute(
                text(
                    """
                    UPDATE agent_session
                    SET last_activity_at = now()
                    WHERE id = :sid
                    """
                ),
                {"sid": str(session_id)},
            )

        # Reserve the assistant message id up front so the SSE first-frame
        # can carry it back to the client. We'll UPDATE its content when the
        # stream completes. created_at is now()+1ms so the assistant always
        # sorts AFTER the user message inserted above (both are in the same
        # transaction so plain now() would give identical timestamps and the
        # message list would render the assistant placeholder first).
        assistant_msg = (
            await session.execute(
                text(
                    """
                    INSERT INTO chat_message (
                        id, workspace_id, entity_id, session_id, role,
                        content, created_at,
                        model, backend
                    ) VALUES (
                        gen_random_uuid(), :wid, :eid, :sid, 'assistant',
                        CAST(:content AS jsonb), now() + interval '1 ms',
                        :model, :backend
                    )
                    RETURNING id::text AS id
                    """
                ),
                {
                    "wid": str(workspace.workspace_id),
                    "eid": entity_id,
                    "sid": str(session_id),
                    "content": json.dumps({"type": "text", "text": ""}),
                    "model": (body.model or DEFAULT_MODEL),
                    "backend": DEFAULT_BACKEND,
                },
            )
        ).mappings().one()

    # ---- Build the runner's history input from what's in the DB.
    runner_history: list[dict[str, Any]] = []
    for r in history_rows:
        c = r["content"]
        if isinstance(c, (bytes, bytearray)):
            c = json.loads(c.decode("utf-8"))
        elif isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                c = {"text": c}
        if r["role"] == "user":
            text_val = c.get("text") if isinstance(c, dict) else str(c)
            runner_history.append({"role": "user", "content": text_val or ""})
        elif r["role"] == "assistant":
            text_val = c.get("text") if isinstance(c, dict) else str(c)
            if text_val:
                runner_history.append({"role": "assistant", "content": text_val})

    # ---- Extract the caller's bearer for MCP-side auth.
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer required for chat stream")
    user_bearer = auth_header.split(None, 1)[1].strip()

    # Build the system prompt for the persona.
    system_prompt = persona.system_prompt(
        workspace_id=str(workspace.workspace_id),
        entity_id=entity_id,
        tax_year=tax_year,
        user_uc_uid=workspace.user_uc_uid,
    )

    _clear_cancel(str(session_id))

    user_message_id = user_msg["id"]
    assistant_message_id = assistant_msg["id"]
    selected_model = (body.model or DEFAULT_MODEL)

    async def _generator() -> AsyncIterator[bytes]:
        # First frame: correlation ids for the client.
        yield _sse_frame("session", {
            "session_id": str(session_id),
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
            "persona": persona_slug,
            "model": selected_model,
        })

        # Buffers to persist on completion. The X-Accel-Buffering header
        # below tells the proxy to flush each frame immediately, so we
        # don't need a separate heartbeat task as long as text deltas keep
        # arriving (they will — Sonnet streams every few tokens).
        text_chunks: list[str] = []
        tool_calls_seen: list[dict[str, Any]] = []
        tool_results_seen: list[dict[str, Any]] = []
        stop_payload: dict[str, Any] = {}

        try:
            async for event in stream_assistant_turn(
                system_prompt=system_prompt,
                history=runner_history,
                user_message=body.text,
                allowed_mcp_tools=persona.allowed_mcp_tools,
                user_bearer_token=user_bearer,
                model=selected_model,
                cancel_check=lambda: _is_cancelled(str(session_id)),
            ):
                etype = event.get("type", "text_delta")
                if etype == "text_delta":
                    text_chunks.append(event.get("text", ""))
                elif etype == "tool_call":
                    tool_calls_seen.append({
                        "id": event.get("id"),
                        "name": event.get("name"),
                        "args": event.get("args"),
                    })
                elif etype == "tool_result":
                    # Keep a stripped copy in DB (no canvas-sized payload).
                    tool_results_seen.append({
                        "id": event.get("id"),
                        "name": event.get("name"),
                        "artifact_kind": event.get("artifact_kind"),
                        "artifact_size": event.get("artifact_size"),
                        # The full result is in the SSE event already; we
                        # also store it in chat_message.content so a reload
                        # can replay it in the canvas pane.
                        "result": event.get("result"),
                    })
                elif etype == "stop":
                    stop_payload = event
                elif etype == "error":
                    stop_payload = event

                yield _sse_frame(etype, event)
        finally:
            _clear_cancel(str(session_id))

            # Persist the assistant turn — even on error / cancel, save
            # what we got. The conversation history is the audit trail.
            cancelled = stop_payload.get("stop_reason") == "cancelled" if stop_payload else False
            final_text = "".join(text_chunks)
            persisted = {
                "type": "mixed" if (tool_calls_seen or tool_results_seen) else "text",
                "text": final_text,
                "tool_calls": tool_calls_seen,
                "tool_results": tool_results_seen,
                "stop_reason": stop_payload.get("stop_reason") if stop_payload else None,
                "cancelled": cancelled,
            }
            try:
                async with workspace_session(
                    SessionLocal, workspace.workspace_id
                ) as session:
                    await session.execute(
                        text(
                            """
                            UPDATE chat_message
                            SET content        = CAST(:content AS jsonb),
                                input_tokens   = :input_tokens,
                                output_tokens  = :output_tokens,
                                duration_ms    = :duration_ms,
                                model          = :model,
                                backend        = :backend
                            WHERE id = :id
                            """
                        ),
                        {
                            "id": assistant_message_id,
                            "content": json.dumps(persisted, default=str),
                            "input_tokens": (
                                stop_payload.get("input_tokens") if stop_payload else None
                            ),
                            "output_tokens": (
                                stop_payload.get("output_tokens") if stop_payload else None
                            ),
                            "duration_ms": (
                                stop_payload.get("duration_ms") if stop_payload else None
                            ),
                            "model": selected_model,
                            "backend": DEFAULT_BACKEND,
                        },
                    )
                    await session.execute(
                        text(
                            """
                            UPDATE agent_session
                            SET last_activity_at = now(),
                                cancelled_at     = CASE
                                  WHEN :cancelled THEN now() ELSE cancelled_at
                                END
                            WHERE id = :sid
                            """
                        ),
                        {"sid": str(session_id), "cancelled": cancelled},
                    )
            except Exception:  # pragma: no cover
                _log.exception("Failed to persist assistant message")

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# POST /sessions/{sid}/cancel
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/chat/sessions/{session_id}/cancel")
async def cancel_chat_stream(
    workspace_id: UUID,
    session_id: UUID,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Request the in-flight assistant turn for this session to stop.

    Idempotent; safe to call even when nothing is streaming. The streaming
    handler polls the cancellation flag between deltas and emits a
    ``stop_reason: cancelled`` frame.
    """
    require_role(workspace, "viewer")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        loaded = await _load_session_row(session, session_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Chat session not found")
        assert_entity_in_scope(workspace, UUID(loaded["entity_id"]))

    _request_cancel(str(session_id))
    return {"ok": True, "session_id": str(session_id), "cancelled": True}


# ---------------------------------------------------------------------------
# POST /sessions/{sid}/save-as-memo
# ---------------------------------------------------------------------------


@router.post("/api/workspaces/{workspace_id}/chat/sessions/{session_id}/save-as-memo")
async def save_chat_message_as_memo(
    workspace_id: UUID,
    session_id: UUID,
    body: SaveAsMemoRequest,
    workspace: WorkspaceContext = Depends(resolve_workspace),
) -> dict:
    """Promote an assistant message to a tax_review_queue memo.

    Closes the loop with the existing CPA review surface — the memo shows
    up in /taxes/{year} → Review queue just like a specialist-generated
    one, except submitter_kind='human' and submitter_id=uc_uid (the user
    who saved it).

    Role floor: accountant (the queue itself is gated at accountant).
    """
    require_role(workspace, "accountant")

    async with workspace_session(SessionLocal, workspace.workspace_id) as session:
        loaded = await _load_session_row(session, session_id)
        if loaded is None:
            raise HTTPException(status_code=404, detail="Chat session not found")
        assert_entity_in_scope(workspace, UUID(loaded["entity_id"]))

        msg_row = (
            await session.execute(
                text(
                    """
                    SELECT id::text AS id, role, content
                    FROM chat_message
                    WHERE id = :id AND session_id = :sid
                    """
                ),
                {"id": str(body.message_id), "sid": str(session_id)},
            )
        ).mappings().first()
        if msg_row is None:
            raise HTTPException(status_code=404, detail="Message not found in this session")
        if msg_row["role"] != "assistant":
            raise HTTPException(
                status_code=400,
                detail="Only assistant messages can be promoted to a memo",
            )

        c = msg_row["content"]
        if isinstance(c, (bytes, bytearray)):
            c = json.loads(c.decode("utf-8"))
        elif isinstance(c, str):
            try:
                c = json.loads(c)
            except json.JSONDecodeError:
                c = {"text": str(c)}
        memo_text = (c.get("text") if isinstance(c, dict) else None) or ""
        if not memo_text.strip():
            raise HTTPException(
                status_code=400,
                detail="Selected message has no text to memo",
            )

        topic = (body.topic or loaded.get("title") or "Chat conversation memo")[:240]
        tax_year = loaded.get("tax_year") or dt.date.today().year

        # Categorize the memo so the /open-items aggregator routes it to the
        # right section. The aggregator's _bucket_for_item inspects
        # form_reference prefixes — leveraging the same convention here means
        # chat-saved memos flow into the dashboard the same way as any other
        # tax_review_queue entry.
        #
        # Heuristic on the memo text (lowercase substring match):
        #   * "decide", "deduct currently", "elect" -> Decisions awaiting Aaron
        #   * "send", "provide", "upload", "pull",
        #     "obtain", "need to get"               -> Documents (informational)
        #   * "critical", "urgent", "deadline"      -> Time-sensitive
        #   * else                                   -> Mosin review (default)
        lower = memo_text.lower()
        if any(w in lower for w in ("critical", "urgent", "deadline", "immediately")):
            bucket = "time_sensitive"
            form_reference = f"CRITICAL: {topic[:160]}"
        elif any(w in lower for w in ("decide", "election", "deduct currently",
                                       "skip", "should we", "should i", "yes or no")):
            bucket = "decision"
            form_reference = f"Decision: {topic[:160]}"
        elif any(w in lower for w in ("need to get", "upload", "obtain ", "send the",
                                       "provide ", "physically pull")):
            bucket = "document_ask"
            form_reference = f"Document Ask: {topic[:160]}"
        else:
            bucket = "informational"
            form_reference = f"Chat memo: {topic[:160]}"

        content_payload = {
            "topic": topic,
            "text": memo_text.strip(),
            "bucket": bucket,
            "source": {
                "kind": "chat",
                "session_id": str(session_id),
                "message_id": str(body.message_id),
                "agent_persona": loaded.get("agent_persona"),
                "chat_url": f"/chat?session={session_id}",
            },
        }

        row = (
            await session.execute(
                text(
                    """
                    INSERT INTO tax_review_queue (
                        workspace_id, entity_id, tax_year,
                        item_type, form_reference,
                        content, confidence, citations,
                        status, submitter_kind, submitter_id
                    ) VALUES (
                        :wid, :eid, :tax_year,
                        'memo', :form_ref,
                        CAST(:content AS jsonb), 'medium',
                        CAST('[]' AS jsonb),
                        'pending', 'human', :uid
                    )
                    RETURNING id::text AS id
                    """
                ),
                {
                    "wid": str(workspace.workspace_id),
                    "eid": loaded["entity_id"],
                    "tax_year": tax_year,
                    "form_ref": form_reference,
                    "content": json.dumps(content_payload),
                    "uid": workspace.user_uc_uid,
                },
            )
        ).mappings().one()

    return {
        "ok": True,
        "memo_id": row["id"],
        "tax_year": tax_year,
        "entity_id": loaded["entity_id"],
        "bucket": bucket,
        "form_reference": form_reference,
        "open_items_url": "/open-items",
    }
