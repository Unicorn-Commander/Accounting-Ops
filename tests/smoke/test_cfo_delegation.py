"""Smoke tests for the CFO → specialist delegation orchestrator (workstream A).

Covers ``agents.chat.delegation`` (the agents-as-tools machinery) and its wiring
into ``agents.chat.runner.stream_assistant_turn``. The hard contracts:

  * **Tool schema** — consult_specialist exposes a CLOSED enum of specialist
    slugs and the right shape.
  * **Depth-1** — a consulted specialist is run WITHOUT a delegation context,
    and the consult tool is stripped from its allow-list, so it can NEVER
    recurse.
  * **Scope-safe** — the inner consult carries the caller's EXACT
    workspace/entity/tax_year/bearer; nothing is widened.
  * **Bounded** — ≤ MAX_CONSULTS_PER_TURN per turn; a 3rd consult is refused
    gracefully (no raise). Timeouts/errors degrade to a structured payload.
  * **No auto-posting** — the specialist's tool set is its normal read/draft set
    (no mutate/approve tool is added by delegation).
  * **Auditable** — every consult invokes the audit_sink with a structured
    record (which the chat layer persists to decision_audit).
  * **Runner dispatch** — consult_specialist is handled in-process and emits a
    UX-silent ``delegation`` event, NOT a visible tool_call/tool_result card.

Everything runs against fakes — no live LLM, no MCP server, no Postgres.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agents.chat import delegation as deleg


WID = "11111111-1111-1111-1111-111111111111"
EID = "22222222-2222-2222-2222-222222222222"
BEARER = "test-bearer-token-abc123"


# ---------------------------------------------------------------------------
# Helpers: a fake stream_assistant_turn the inner consult drives.
# ---------------------------------------------------------------------------


def _fake_stream(events):
    """Return an async-generator function yielding the given events."""

    async def _gen(**kwargs):
        # record the kwargs the inner consult was invoked with for assertions
        _gen.last_kwargs = kwargs
        for ev in events:
            yield ev

    _gen.last_kwargs = None
    return _gen


def _ok_specialist_events(text="The reasonable-comp figure is defensible at $120K."):
    return [
        {"type": "text_delta", "text": text},
        {
            "type": "stop",
            "stop_reason": "end_turn",
            "input_tokens": 100,
            "output_tokens": 30,
        },
    ]


def _make_ctx(monkeypatch, *, audit_records=None, stream_events=None,
              stream_fn=None, max_consults=None):
    """Build a DelegationContext with the inner runner mocked.

    ``audit_records`` (a list) collects every audit_sink payload.
    """
    records = audit_records if audit_records is not None else []

    async def _sink(rec):
        records.append(rec)

    ctx = deleg.DelegationContext(
        workspace_id=WID,
        entity_id=EID,
        user_uc_uid="jane",
        user_bearer_token=BEARER,
        tax_year=2023,
        cfo_agent_id="cfo-agent",
        audit_sink=_sink,
        max_consults=max_consults or deleg.MAX_CONSULTS_PER_TURN,
    )

    # Patch the runner's stream used by _collect_consult_turn.
    import agents.chat.runner as runner_mod

    fn = stream_fn or _fake_stream(stream_events or _ok_specialist_events())
    monkeypatch.setattr(runner_mod, "stream_assistant_turn", fn)
    return ctx, records, fn


# ---------------------------------------------------------------------------
# 1. Tool schema
# ---------------------------------------------------------------------------


def test_tool_schema_shape_and_closed_enum():
    schema = deleg.consult_specialist_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "consult_specialist"
    props = fn["parameters"]["properties"]
    assert set(fn["parameters"]["required"]) == {"specialist", "question"}
    # Closed enum == exactly the consultable slugs (no arbitrary persona).
    assert props["specialist"]["enum"] == list(deleg.CONSULTABLE_SPECIALIST_SLUGS)
    assert fn["parameters"]["additionalProperties"] is False


def test_consultable_slugs_all_resolve_to_personas():
    """Every consultable slug must be a real registered persona."""
    from agents.chat import get_persona

    for slug in deleg.CONSULTABLE_SPECIALIST_SLUGS:
        assert get_persona(slug) is not None, f"{slug} not in persona registry"


def test_cfo_and_strategic_personas_not_consultable():
    """No self-consult, no consulting the resolver/setup/research personas."""
    for slug in ("cfo-agent", "cpa-tax-resolver", "setup-assistant", "tax-research"):
        assert slug not in deleg.CONSULTABLE_SPECIALIST_SLUGS


# ---------------------------------------------------------------------------
# 2. Depth-1 guard
# ---------------------------------------------------------------------------


def test_inner_allowed_tools_strips_consult():
    tools = ("get_runway", "draft_memo", deleg.CONSULT_TOOL_NAME, "get_p_and_l")
    inner = deleg._inner_allowed_tools(tools)
    assert deleg.CONSULT_TOOL_NAME not in inner
    assert set(inner) == {"get_runway", "draft_memo", "get_p_and_l"}


async def test_inner_consult_runs_without_delegation_context(monkeypatch):
    """The PRIMARY depth-1 guard: the inner turn is invoked with NO delegation,
    so a consulted specialist is never given the consult tool at all."""
    ctx, _records, fn = _make_ctx(monkeypatch)
    out = await deleg.dispatch_consult(
        args={"specialist": "officer-compensation-specialist",
              "question": "Is $120K defensible?"},
        ctx=ctx,
    )
    assert out["ok"] is True
    # The runner was called WITHOUT a delegation kwarg (or with it None).
    kwargs = fn.last_kwargs
    assert kwargs is not None
    assert kwargs.get("delegation") in (None, ...) or "delegation" not in kwargs
    # And the consult tool was stripped from the specialist's allow-list.
    assert deleg.CONSULT_TOOL_NAME not in kwargs["allowed_mcp_tools"]


# ---------------------------------------------------------------------------
# 3. Scope-safe: caller scope carried verbatim
# ---------------------------------------------------------------------------


async def test_consult_carries_caller_scope(monkeypatch):
    ctx, _records, fn = _make_ctx(monkeypatch)
    await deleg.dispatch_consult(
        args={"specialist": "sc-state-specialist", "question": "SC1120 due?"},
        ctx=ctx,
    )
    kwargs = fn.last_kwargs
    # Bearer is carried verbatim (MCP middleware re-validates it per call).
    assert kwargs["user_bearer_token"] == BEARER
    # The specialist's system prompt is bound to the caller's workspace/entity.
    assert WID in kwargs["system_prompt"]
    assert EID in kwargs["system_prompt"]
    # The consultation framing + the question are present.
    assert "consulted privately by the CFO Agent" in kwargs["system_prompt"]
    assert kwargs["user_message"] == "SC1120 due?"
    # Bounded caps applied to the inner turn.
    assert kwargs["max_iterations"] == deleg.CONSULT_MAX_TOOL_ITERATIONS
    assert kwargs["max_tokens"] == deleg.CONSULT_MAX_TOKENS


# ---------------------------------------------------------------------------
# 4. Bounded: budget cap + graceful degradation
# ---------------------------------------------------------------------------


async def test_budget_cap_refuses_third_consult(monkeypatch):
    ctx, _records, _fn = _make_ctx(monkeypatch)
    assert ctx.budget_remaining() == 2
    r1 = await deleg.dispatch_consult(
        args={"specialist": "1099-specialist", "question": "q1"}, ctx=ctx
    )
    r2 = await deleg.dispatch_consult(
        args={"specialist": "sc-state-specialist", "question": "q2"}, ctx=ctx
    )
    r3 = await deleg.dispatch_consult(
        args={"specialist": "audit-defense-specialist", "question": "q3"}, ctx=ctx
    )
    assert r1["ok"] and r2["ok"]
    assert r3["ok"] is False
    assert "budget exhausted" in r3["error"]
    assert ctx.consults_used == 2  # the refused call did NOT increment further


async def test_unknown_specialist_refused(monkeypatch):
    ctx, records, _fn = _make_ctx(monkeypatch)
    out = await deleg.dispatch_consult(
        args={"specialist": "cfo-agent", "question": "recurse pls"}, ctx=ctx
    )
    assert out["ok"] is False
    assert "not a consultable specialist" in out["error"]
    # Refused before running → no budget spent, no audit row.
    assert ctx.consults_used == 0
    assert records == []


async def test_empty_question_refused(monkeypatch):
    ctx, _records, _fn = _make_ctx(monkeypatch)
    out = await deleg.dispatch_consult(
        args={"specialist": "1099-specialist", "question": "   "}, ctx=ctx
    )
    assert out["ok"] is False
    assert "question is required" in out["error"]
    assert ctx.consults_used == 0


async def test_consult_timeout_degrades_gracefully(monkeypatch):
    # A stream that never completes within the timeout window.
    async def _hang(**kwargs):
        await asyncio.sleep(5)
        yield {"type": "stop", "stop_reason": "end_turn"}

    ctx, records, _fn = _make_ctx(monkeypatch, stream_fn=_hang)
    # Shrink the timeout so the test is fast.
    monkeypatch.setattr(deleg, "CONSULT_TIMEOUT_SECONDS", 0.05)
    out = await deleg.dispatch_consult(
        args={"specialist": "nol-schedule-l-analyst", "question": "NOL c/f?"},
        ctx=ctx,
    )
    assert out["ok"] is False
    assert "timed out" in out["error"]
    # Budget WAS spent (reserved before running) — prevents retry storms.
    assert ctx.consults_used == 1
    # A degrade note tells the CFO to answer + flag the gap.
    assert "degrade_note" in out
    # The failure is still audited.
    assert len(records) == 1
    assert records[0]["ok"] is False


async def test_consult_inner_error_degrades(monkeypatch):
    err_events = [{"type": "error", "message": "LLM request failed: boom"}]
    ctx, records, _fn = _make_ctx(monkeypatch, stream_events=err_events)
    out = await deleg.dispatch_consult(
        args={"specialist": "federal-strategy-drafter", "question": "§168(k)?"},
        ctx=ctx,
    )
    assert out["ok"] is False
    assert "boom" in out["error"]
    assert records[0]["ok"] is False


async def test_consult_empty_analysis_is_soft_failure(monkeypatch):
    # Model streams nothing but a clean stop → no usable analysis.
    empty = [{"type": "stop", "stop_reason": "end_turn"}]
    ctx, _records, _fn = _make_ctx(monkeypatch, stream_events=empty)
    out = await deleg.dispatch_consult(
        args={"specialist": "form-1120-preparer", "question": "line 11?"},
        ctx=ctx,
    )
    assert out["ok"] is False
    assert "no analysis" in out["error"]


async def test_dispatch_never_raises_on_collect_failure(monkeypatch):
    # If the inner collector itself raises (not via an error event), the
    # public dispatch_consult must still return a structured payload.
    async def _raise(**kwargs):
        raise RuntimeError("catastrophic")
        yield  # pragma: no cover

    ctx, _records, _fn = _make_ctx(monkeypatch, stream_fn=_raise)
    out = await deleg.dispatch_consult(
        args={"specialist": "1099-specialist", "question": "q"}, ctx=ctx
    )
    assert out["ok"] is False
    assert "catastrophic" in out["error"]


# ---------------------------------------------------------------------------
# 5. Auditable
# ---------------------------------------------------------------------------


async def test_successful_consult_audited(monkeypatch):
    ctx, records, _fn = _make_ctx(monkeypatch)
    await deleg.dispatch_consult(
        args={"specialist": "officer-compensation-specialist",
              "question": "Is $120K reasonable?"},
        ctx=ctx,
    )
    assert len(records) == 1
    rec = records[0]
    assert rec["tool_name"] == "consult_specialist"
    assert rec["specialist"] == "officer-compensation-specialist"
    assert rec["question"] == "Is $120K reasonable?"
    assert rec["ok"] is True
    assert rec["result_summary"]  # a non-empty summary line
    assert rec["cfo_agent_id"] == "cfo-agent"
    assert rec["workspace_id"] == WID
    assert rec["entity_id"] == EID
    assert rec["consult_index"] == 1


async def test_audit_sink_failure_does_not_break_consult(monkeypatch):
    async def _boom_sink(rec):
        raise RuntimeError("db down")

    ctx = deleg.DelegationContext(
        workspace_id=WID, entity_id=EID, user_uc_uid="jane",
        user_bearer_token=BEARER, tax_year=2023, audit_sink=_boom_sink,
    )
    import agents.chat.runner as runner_mod
    monkeypatch.setattr(
        runner_mod, "stream_assistant_turn", _fake_stream(_ok_specialist_events())
    )
    out = await deleg.dispatch_consult(
        args={"specialist": "sc-state-specialist", "question": "q"}, ctx=ctx
    )
    # The consult still succeeds even though auditing blew up.
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# 6. Analysis truncation (runaway specialist can't blow CFO context)
# ---------------------------------------------------------------------------


async def test_long_analysis_truncated(monkeypatch):
    huge = "x" * (deleg._ANALYSIS_TRUNCATE_CHARS + 5000)
    ctx, _records, _fn = _make_ctx(
        monkeypatch, stream_events=_ok_specialist_events(text=huge)
    )
    out = await deleg.dispatch_consult(
        args={"specialist": "audit-defense-specialist", "question": "q"}, ctx=ctx
    )
    assert out["ok"] is True
    assert len(out["analysis"]) <= deleg._ANALYSIS_TRUNCATE_CHARS
    assert out["analysis"].endswith("…")


# ---------------------------------------------------------------------------
# 7. Runner-level dispatch: consult is UX-silent (delegation event, not card)
# ---------------------------------------------------------------------------


class _FakeMCPTool:
    def __init__(self, name):
        self.name = name
        self.description = f"desc for {name}"
        self.inputSchema = {"type": "object", "properties": {}}


class _FakeToolsResponse:
    def __init__(self, names):
        self.tools = [_FakeMCPTool(n) for n in names]


class _FakeMCPSession:
    """Records MCP call_tool invocations so we can prove the consult never
    reaches the MCP server."""

    def __init__(self, names):
        self._names = names
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self):
        return None

    async def list_tools(self):
        return _FakeToolsResponse(self._names)

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        class _R:
            content = []
        return _R()


def _install_fake_mcp(monkeypatch, session):
    """Patch the runner's MCP client + ClientSession context managers + openai
    client so stream_assistant_turn runs fully offline."""
    import agents.chat.runner as runner_mod

    class _CtxStreamable:
        async def __aenter__(self):
            return ("r", "w", None)

        async def __aexit__(self, *a):
            return False

    class _CtxSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    def _streamable(url, headers=None):
        return _CtxStreamable()

    # The runner imports these lazily INSIDE the function from `mcp...`.
    import sys
    import types

    mcp_mod = types.ModuleType("mcp")
    mcp_mod.ClientSession = _CtxSession
    client_mod = types.ModuleType("mcp.client")
    shttp_mod = types.ModuleType("mcp.client.streamable_http")
    shttp_mod.streamablehttp_client = _streamable
    monkeypatch.setitem(sys.modules, "mcp", mcp_mod)
    monkeypatch.setitem(sys.modules, "mcp.client", client_mod)
    monkeypatch.setitem(sys.modules, "mcp.client.streamable_http", shttp_mod)

    # Ensure the openai-not-installed guard doesn't short-circuit.
    if getattr(runner_mod, "openai", None) is None:
        monkeypatch.setattr(runner_mod, "openai", types.SimpleNamespace())


class _OneToolCallThenAnswer:
    """A fake OpenAI AsyncOpenAI whose first completion emits a tool_call for
    consult_specialist, and whose second completion emits a final answer."""

    def __init__(self):
        self._n = 0
        self.chat = self  # so client.chat.completions.create works
        self.completions = self

    async def create(self, **kwargs):
        self._n += 1
        call_index = self._n

        async def _stream():
            if call_index == 1:
                # Stream a tool_call for consult_specialist.
                yield _chunk_tool_call(
                    idx=0,
                    call_id="call_1",
                    name="consult_specialist",
                    args='{"specialist": "officer-compensation-specialist", '
                         '"question": "Is $120K defensible?"}',
                    finish="tool_calls",
                )
            else:
                # Stream the CFO's synthesized final answer.
                yield _chunk_text("Comp looks defensible at $120K.", finish="stop")

        return _stream()


def _chunk_text(text, finish=None):
    delta = type("D", (), {"content": text, "tool_calls": None})()
    choice = type("C", (), {"delta": delta, "finish_reason": finish})()
    return type("Ch", (), {"choices": [choice], "usage": None})()


def _chunk_tool_call(idx, call_id, name, args, finish=None):
    fn = type("F", (), {"name": name, "arguments": args})()
    tc = type("TC", (), {"index": idx, "id": call_id, "function": fn})()
    delta = type("D", (), {"content": None, "tool_calls": [tc]})()
    choice = type("C", (), {"delta": delta, "finish_reason": finish})()
    return type("Ch", (), {"choices": [choice], "usage": None})()


async def test_runner_consult_is_silent_and_dispatched_in_process(monkeypatch):
    """End-to-end through stream_assistant_turn:

      * the model's consult_specialist call is handled IN-PROCESS (never sent to
        MCP.call_tool),
      * it emits a UX-silent ``delegation`` event (NOT tool_call/tool_result),
      * the specialist analysis is fed back and the CFO produces a final answer.
    """
    import agents.chat.runner as runner_mod

    fake_session = _FakeMCPSession(names=["get_runway", "draft_memo"])
    _install_fake_mcp(monkeypatch, fake_session)
    monkeypatch.setattr(
        runner_mod.openai, "AsyncOpenAI",
        lambda *a, **k: _OneToolCallThenAnswer(), raising=False,
    )

    # The inner consult would call stream_assistant_turn again (the SAME
    # function under test). To keep the inner turn deterministic + offline we
    # patch dispatch's collector directly with a canned specialist answer.
    async def _fake_collect(*, specialist_slug, question, ctx):
        # Mirrors the real return shape.
        return {
            "ok": True,
            "specialist": specialist_slug,
            "analysis": "Defensible at $120K per §162.",
            "tool_calls": [],
            "stop_reason": "end_turn",
            "input_tokens": 10,
            "output_tokens": 5,
            "error": None,
        }

    monkeypatch.setattr(deleg, "_collect_consult_turn", _fake_collect)

    records: list[dict] = []

    async def _sink(rec):
        records.append(rec)

    ctx = deleg.DelegationContext(
        workspace_id=WID, entity_id=EID, user_uc_uid="jane",
        user_bearer_token=BEARER, tax_year=2023, audit_sink=_sink,
    )

    events = []
    async for ev in runner_mod.stream_assistant_turn(
        system_prompt="CFO system prompt",
        history=[],
        user_message="Is our officer comp defensible?",
        allowed_mcp_tools=("get_runway", "draft_memo"),
        user_bearer_token=BEARER,
        openrouter_api_key="test-key",
        delegation=ctx,
    ):
        events.append(ev)

    types_seen = [e["type"] for e in events]
    # A delegation event was emitted...
    assert "delegation" in types_seen
    deleg_ev = next(e for e in events if e["type"] == "delegation")
    assert deleg_ev["specialist"] == "officer-compensation-specialist"
    assert deleg_ev["ok"] is True
    # ...and it was NOT surfaced as a visible tool card.
    assert not any(
        e.get("name") == "consult_specialist"
        for e in events
        if e["type"] in ("tool_call", "tool_result")
    )
    # The consult never hit the MCP server.
    assert all(name != "consult_specialist" for name, _ in fake_session.calls)
    # The CFO produced a final synthesized answer.
    final_text = "".join(e.get("text", "") for e in events if e["type"] == "text_delta")
    assert "defensible" in final_text.lower()
    # The delegation was audited.
    assert len(records) == 1 and records[0]["specialist"] == "officer-compensation-specialist"


async def test_runner_without_delegation_has_no_consult_tool(monkeypatch):
    """A non-CFO turn (delegation=None) never exposes consult_specialist and a
    consult_specialist tool_call (if the model somehow emitted one) would be
    rejected as not-allowed rather than dispatched."""
    import agents.chat.runner as runner_mod

    fake_session = _FakeMCPSession(names=["get_runway"])
    _install_fake_mcp(monkeypatch, fake_session)

    captured_tools: dict[str, Any] = {}

    class _NoToolModel:
        def __init__(self):
            self.chat = self
            self.completions = self

        async def create(self, **kwargs):
            captured_tools["tools"] = kwargs.get("tools")

            async def _stream():
                yield _chunk_text("Plain answer, no tools.", finish="stop")

            return _stream()

    monkeypatch.setattr(
        runner_mod.openai, "AsyncOpenAI",
        lambda *a, **k: _NoToolModel(), raising=False,
    )

    events = []
    async for ev in runner_mod.stream_assistant_turn(
        system_prompt="some persona",
        history=[],
        user_message="hi",
        allowed_mcp_tools=("get_runway",),
        user_bearer_token=BEARER,
        openrouter_api_key="test-key",
        delegation=None,
    ):
        events.append(ev)

    tool_names = {
        t["function"]["name"] for t in (captured_tools.get("tools") or [])
    }
    assert "consult_specialist" not in tool_names
    assert "get_runway" in tool_names
