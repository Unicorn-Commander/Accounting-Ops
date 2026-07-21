"""Smoke tests for the entity-owner write surface (the Schedule K-1 input).

``books.entity_owner`` (tenant migration 0029) had readers — the cpa-export
1120-S / 1065 K-1 derivation — but NO write path anywhere, so every
flow-through return refused to issue K-1s forever. These tests pin the new
surface shut against regression:

  1. Tool registration — importable from ``app.tools`` and registered on the
     live FastMCP server.
  2. Discovery metadata — explicit ``_TOOL_METADATA`` rows with the right
     role floors (the manifest gate in test_discovery_manifest.py needs them).
  3. HTTP routes — GET + POST ``.../entities/{entity_id}/owners`` on app.main.
  4. Validation logic — pct bounds, shares sign, owner_type /
     allocation_basis / tax_id_last4 vocabulary, period ordering. Pure python:
     ``authorize_workspace`` and ``workspace_session`` are mocked at the seams
     (same approach as test_ai_categorize.py), and validation failures must
     surface BEFORE any session opens.
  5. Role gating shape — upsert authorizes at admin, list at viewer.
"""
from __future__ import annotations

import contextlib
import types
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

import app.tools.entity_owners as mod
from app.models.ledger import EntityOwner

_OWNERS_PATH = "/api/workspaces/{workspace_id}/entities/{entity_id}/owners"


# ---------------------------------------------------------------------------
# Seam fakes (no DB) — mirrors tests/smoke/test_ai_categorize.py
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Records adds + decision_audit inserts; serves SELECTs its seeded rows."""

    def __init__(self, rows=None, existing=None):
        self._rows = rows or []
        self.existing = existing
        self.added = []
        self.audits = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def get(self, model, pk):
        return self.existing

    async def execute(self, stmt, params=None):
        if "INSERT INTO decision_audit" in str(stmt):
            self.audits.append(params)
            return _FakeResult([])
        return _FakeResult(self._rows)


def _install(monkeypatch, session):
    """Wire the auth + session seams; return the recorded min_role list."""
    recorded: list[str] = []

    async def _fake_authorize(workspace_id, entity_id=None, *, min_role="viewer", **kw):
        recorded.append(min_role)
        return types.SimpleNamespace(
            agent_id=None,
            user_uc_uid="u1",
            workspace_id=workspace_id,
            role="admin",
            entity_scope=None,
        )

    @contextlib.asynccontextmanager
    async def _fake_ws(_sessionmaker, _wid):
        yield session

    monkeypatch.setattr(mod, "authorize_workspace", _fake_authorize)
    monkeypatch.setattr(mod, "workspace_session", _fake_ws)
    return recorded


def _install_no_session(monkeypatch):
    """Auth passes, but opening a workspace session is a test failure —
    proves validation raises BEFORE any DB work."""

    @contextlib.asynccontextmanager
    async def _must_not_open(_sessionmaker, _wid):
        raise AssertionError("workspace_session must not open on validation failure")
        yield  # pragma: no cover

    recorded = _install(monkeypatch, session=None)
    monkeypatch.setattr(mod, "workspace_session", _must_not_open)
    return recorded


def _valid_kwargs(**over):
    kw = dict(
        owner_name="Aaron Stransky",
        effective_from="2025-01-01",
        reasoning="initial cap table entry from the operating agreement",
    )
    kw.update(over)
    return kw


# ---------------------------------------------------------------------------
# 1. Tool registration
# ---------------------------------------------------------------------------


def test_tools_importable_from_app_tools():
    from app.tools import list_entity_owners, upsert_entity_owner

    assert callable(upsert_entity_owner)
    assert upsert_entity_owner.__name__ == "upsert_entity_owner"
    assert callable(list_entity_owners)
    assert list_entity_owners.__name__ == "list_entity_owners"


@pytest.mark.asyncio
async def test_mcp_server_registers_entity_owner_tools():
    """Both tools must be live FastMCP registrations, under their own names."""
    from mcp_server import build_server

    server = build_server()
    names = {t.name for t in await server.list_tools()}
    assert "upsert_entity_owner" in names
    assert "list_entity_owners" in names


# ---------------------------------------------------------------------------
# 2. Discovery metadata (the manifest gate)
# ---------------------------------------------------------------------------


def test_discovery_metadata_present_with_correct_floors():
    from app.api.discovery import _SCOPE_FOR_ROLE, _TOOL_METADATA

    up = _TOOL_METADATA.get("upsert_entity_owner")
    assert up is not None, "upsert_entity_owner missing from _TOOL_METADATA"
    assert up["min_role"] == "admin"
    assert up["read_only"] is False

    ls = _TOOL_METADATA.get("list_entity_owners")
    assert ls is not None, "list_entity_owners missing from _TOOL_METADATA"
    assert ls["min_role"] == "viewer"
    assert ls["read_only"] is True

    # Roles must be routable by the scope map (test_discovery_manifest gates
    # this globally; pinned here so a rename fails THIS file too).
    assert up["min_role"] in _SCOPE_FOR_ROLE
    assert ls["min_role"] in _SCOPE_FOR_ROLE


# ---------------------------------------------------------------------------
# 3. HTTP routes
# ---------------------------------------------------------------------------


def test_http_owner_routes_registered_get_and_post():
    import app.main as m

    methods: set[str] = set()
    for r in m.app.routes:
        if getattr(r, "path", None) == _OWNERS_PATH:
            methods |= set(getattr(r, "methods", set()) or set())
    assert "GET" in methods, f"GET {_OWNERS_PATH} not registered"
    assert "POST" in methods, f"POST {_OWNERS_PATH} not registered"


# ---------------------------------------------------------------------------
# 4. Validation logic (pure python — session must never open)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("profit_pct", "150"),
        ("profit_pct", "-0.01"),
        ("loss_pct", "101"),
        ("capital_pct", "100.000001"),
        ("profit_pct", "not-a-number"),
    ],
)
async def test_upsert_rejects_out_of_range_percentages(monkeypatch, field, value):
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match=field):
        await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs(**{field: value}))


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["-5", "abc"])
async def test_upsert_rejects_bad_shares(monkeypatch, value):
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match="shares"):
        await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs(shares=value))


@pytest.mark.asyncio
async def test_upsert_rejects_unknown_owner_type(monkeypatch):
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match="owner_type"):
        await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs(owner_type="alien"))


@pytest.mark.asyncio
async def test_upsert_rejects_unknown_allocation_basis(monkeypatch):
    """The allocation interlock: only the three attested values (or omitted)."""
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match="allocation_basis"):
        await mod.upsert_entity_owner(
            uuid4(), uuid4(), **_valid_kwargs(allocation_basis="pro_rata_probably")
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["123", "12345", "12a4"])
async def test_upsert_rejects_malformed_tax_id_last4(monkeypatch, value):
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match="tax_id_last4"):
        await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs(tax_id_last4=value))


@pytest.mark.asyncio
async def test_upsert_rejects_inverted_period(monkeypatch):
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match="effective_to"):
        await mod.upsert_entity_owner(
            uuid4(),
            uuid4(),
            **_valid_kwargs(effective_from="2025-06-01", effective_to="2025-01-01"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "01/02/2025"])
async def test_upsert_rejects_missing_or_malformed_effective_from(monkeypatch, value):
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match="effective_from"):
        await mod.upsert_entity_owner(
            uuid4(), uuid4(), **_valid_kwargs(effective_from=value)
        )


@pytest.mark.asyncio
async def test_upsert_rejects_blank_owner_name_and_reasoning(monkeypatch):
    _install_no_session(monkeypatch)
    with pytest.raises(ValueError, match="owner_name"):
        await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs(owner_name="   "))
    with pytest.raises(ValueError, match="reasoning"):
        await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs(reasoning=""))


# ---------------------------------------------------------------------------
# 4b. Write behavior (fake session — asserts row shape + decision audit)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_creates_row_and_writes_decision_audit(monkeypatch):
    session = _FakeSession()
    _install(monkeypatch, session)
    wid, eid = uuid4(), uuid4()

    out = await mod.upsert_entity_owner(
        wid,
        eid,
        **_valid_kwargs(
            shares="100",
            profit_pct="100",   # boundary values are legal
            loss_pct="0",
            tax_id_last4="1234",
            allocation_basis="per_share_per_day",
        ),
    )

    assert out["created"] is True
    assert out["owner"]["owner_name"] == "Aaron Stransky"
    assert out["owner"]["shares"] == "100"
    [row] = session.added
    assert row.workspace_id == wid and row.entity_id == eid
    assert row.effective_from == date(2025, 1, 1)
    assert row.shares == Decimal("100")
    assert row.allocation_basis == "per_share_per_day"
    # Exactly one decision_audit row, attributed to this tool.
    [audit] = session.audits
    assert audit["tool_name"] == "upsert_entity_owner"
    assert audit["outcome"] == "applied"
    assert str(wid) == audit["wid"] and str(eid) == audit["eid"]


@pytest.mark.asyncio
async def test_upsert_never_defaults_allocation_basis(monkeypatch):
    """NULL basis = refusal to allocate. The tool must store None as None —
    never silently substitute pro-rata (migration 0029's interlock)."""
    session = _FakeSession()
    _install(monkeypatch, session)

    out = await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs(shares="50"))

    assert out["owner"]["allocation_basis"] is None
    assert session.added[0].allocation_basis is None


@pytest.mark.asyncio
async def test_upsert_updates_existing_row_in_place(monkeypatch):
    wid, eid, oid = uuid4(), uuid4(), uuid4()
    existing = EntityOwner(
        id=oid,
        workspace_id=wid,
        entity_id=eid,
        owner_name="Old Name",
        owner_type="individual",
        effective_from=date(2024, 1, 1),
    )
    session = _FakeSession(existing=existing)
    _install(monkeypatch, session)

    out = await mod.upsert_entity_owner(
        wid, eid, **_valid_kwargs(owner_id=oid, profit_pct="33.34")
    )

    assert out["created"] is False
    assert out["owner_id"] == str(oid)
    assert session.added == []  # corrected, not duplicated
    assert existing.owner_name == "Aaron Stransky"
    assert existing.profit_pct == Decimal("33.34")
    assert len(session.audits) == 1


@pytest.mark.asyncio
async def test_upsert_rejects_owner_id_belonging_to_another_entity(monkeypatch):
    wid, eid, oid = uuid4(), uuid4(), uuid4()
    foreign = EntityOwner(
        id=oid,
        workspace_id=wid,
        entity_id=uuid4(),  # different entity in the same workspace
        owner_name="Someone Else",
        owner_type="individual",
        effective_from=date(2024, 1, 1),
    )
    session = _FakeSession(existing=foreign)
    _install(monkeypatch, session)

    with pytest.raises(ValueError, match="owner_id"):
        await mod.upsert_entity_owner(wid, eid, **_valid_kwargs(owner_id=oid))
    assert session.audits == []  # nothing written for a refused update


@pytest.mark.asyncio
async def test_list_serializes_rows(monkeypatch):
    row = EntityOwner(
        id=uuid4(),
        workspace_id=uuid4(),
        entity_id=uuid4(),
        owner_name="Jordan Stransky",
        owner_type="individual",
        effective_from=date(2025, 1, 1),
        shares=Decimal("49.5"),
    )
    session = _FakeSession(rows=[row])
    _install(monkeypatch, session)

    out = await mod.list_entity_owners(uuid4(), uuid4())

    assert out == [
        {
            "owner_id": str(row.id),
            "owner_name": "Jordan Stransky",
            "owner_type": "individual",
            "tax_id_last4": None,
            "effective_from": "2025-01-01",
            "effective_to": None,
            "shares": "49.5",
            "profit_pct": None,
            "loss_pct": None,
            "capital_pct": None,
            "is_general_partner": None,
            "is_officer": None,
            "allocation_basis": None,
            "notes": None,
        }
    ]


# ---------------------------------------------------------------------------
# 5. Role gating shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_authorizes_at_admin_floor(monkeypatch):
    session = _FakeSession()
    recorded = _install(monkeypatch, session)

    await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs())

    assert recorded == ["admin"]


@pytest.mark.asyncio
async def test_list_authorizes_at_viewer_floor(monkeypatch):
    session = _FakeSession(rows=[])
    recorded = _install(monkeypatch, session)

    out = await mod.list_entity_owners(uuid4(), uuid4())

    assert out == []
    assert recorded == ["viewer"]


@pytest.mark.asyncio
async def test_upsert_denial_short_circuits_before_any_write(monkeypatch):
    """authorize_workspace raising PermissionError must abort the tool before
    validation or session work — the auth check is FIRST in the body."""

    async def _deny(*a, **kw):
        raise PermissionError("insufficient role")

    @contextlib.asynccontextmanager
    async def _must_not_open(_sessionmaker, _wid):
        raise AssertionError("session must not open for a denied caller")
        yield  # pragma: no cover

    monkeypatch.setattr(mod, "authorize_workspace", _deny)
    monkeypatch.setattr(mod, "workspace_session", _must_not_open)

    with pytest.raises(PermissionError):
        await mod.upsert_entity_owner(uuid4(), uuid4(), **_valid_kwargs())
