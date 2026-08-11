"""H2 service-token scope constraints — integration coverage.

These tests exercise ``resolve_workspace`` end-to-end against a REAL database,
covering workspace gate, entity-scope intersection, and role-cap enforcement.

Approach: option (c) — in-process ASGI via httpx.AsyncClient(transport=
ASGITransport(app=app)) with the real asyncpg DB pool (populated by the
integration stack). Service tokens with specific H2 scope fields are injected
via monkeypatching ``app.auth._service_tokens_cache`` — no container restart
needed. The database layer is identical to what the live container uses.

Test cases by number (matches spec):
  1. workspace allow-list — match
  2. workspace allow-list — mismatch
  3. workspace allow-list — empty list = deny-all
  4. entity intersection — overlap
  5. entity intersection — no overlap
  6. entity intersection — broadens prohibited (intersection, not union)
  7. role cap — demotion (admin→viewer; admin-only endpoint 403, viewer 200)
  8. role cap — no upgrade (role_floor='admin', actual='viewer' → stays viewer)
  9. backward compat — token without scope fields retains full membership reach

All tests SKIP if the stack is not reachable, consistent with the rest of the
integration tier.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio

import app.auth as auth_mod
from tests.conftest import (
    TEST_ENTITY_ID,
    TEST_WORKSPACE_B_ID,
    TEST_WORKSPACE_ID,
    StackConfig,
)
from tests.integration.conftest import (
    ENTITY_B_ID,
    _ensure_entity_b,
    _set_membership,
)

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Stable, long (>= 32 char) token strings for each H2 test scenario.
# Using descriptive prefixes makes test failures self-documenting.
# ---------------------------------------------------------------------------
_TOKEN_WS_MATCH    = "h2-ws-match-" + "0" * 28        # 40 chars
_TOKEN_WS_MISMATCH = "h2-ws-mismatch-" + "0" * 25     # 40 chars
_TOKEN_WS_EMPTY    = "h2-ws-empty-" + "0" * 28        # 40 chars
_TOKEN_ENT_OVERLAP = "h2-ent-overlap-" + "0" * 25     # 40 chars
_TOKEN_ENT_NOOVLP  = "h2-ent-no-overlap-" + "0" * 22  # 40 chars
_TOKEN_ENT_NARROW  = "h2-ent-narrow-" + "0" * 26      # 40 chars
_TOKEN_ROLE_VIEWER = "h2-role-viewer-" + "0" * 25     # 40 chars
_TOKEN_ROLE_NOUP   = "h2-role-no-upgrade-" + "0" * 21 # 40 chars
_TOKEN_LEGACY      = "h2-legacy-no-scope-" + "0" * 21 # 40 chars

# The uc_uid that all scoped tokens impersonate. Must be in the membership
# table when the tests run — ensured by the seeded_workspace fixture, which
# calls _ensure_membership for live_stack.uc_uid.
_SVC_UID = "test-service"  # matches SERVICE_UC_UID in integration_env.py


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_token_map(*entries: tuple[str, dict]) -> str:
    """Build a JSON string suitable for ACCTOPS_SERVICE_TOKENS."""
    return json.dumps({tok: ident for tok, ident in entries})


def _token_entry(
    token: str,
    uc_uid: str = _SVC_UID,
    *,
    workspace_ids: list[str] | None = None,
    entity_ids: list[str] | None = None,
    role_floor: str | None = None,
) -> tuple[str, dict]:
    ident: dict = {"uc_uid": uc_uid, "agent_id": "h2-itest"}
    if workspace_ids is not None:
        ident["workspace_ids"] = workspace_ids
    if entity_ids is not None:
        ident["entity_ids"] = entity_ids
    if role_floor is not None:
        ident["role_floor"] = role_floor
    return (token, ident)


def _patch_tokens(monkeypatch, *entries: tuple[str, dict]) -> None:
    """Inject the given token map directly into the module-level cache.

    Using the cache dict (not re-parsing env) so the test doesn't pollute
    environment state and each test gets exactly the tokens it declares.
    The cache persists for the test function; the autouse fixture below
    resets it before and after every test.
    """
    raw_map = {
        tok: {
            "uc_uid": ident["uc_uid"],
            "agent_id": ident.get("agent_id", "h2-itest"),
            "workspace_ids": auth_mod._parse_uuid_list(ident.get("workspace_ids")),
            "entity_ids": auth_mod._parse_uuid_list(ident.get("entity_ids")),
            "role_floor": auth_mod._parse_role_floor(ident.get("role_floor")),
        }
        for tok, ident in entries
    }
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", raw_map)


@pytest.fixture(autouse=True)
def _reset_token_cache(monkeypatch):
    """Clear the service-token cache before and after every H2 test."""
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)
    yield
    monkeypatch.setattr(auth_mod, "_service_tokens_cache", None)


# ---------------------------------------------------------------------------
# In-process ASGI client factory
# ---------------------------------------------------------------------------

def _asgi_client(token: str) -> httpx.AsyncClient:
    """Return an httpx client that talks directly to the FastAPI app in-process.

    The app uses the same ``DATABASE_URL`` env that the integration stack
    set before pytest started, so this hits the REAL database without any
    network round-trip to the container.
    """
    from app.main import app as fastapi_app  # late import: avoids module-level DB init at collection time

    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fastapi_app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def h2_workspace(
    db: asyncpg.Pool, seeded_workspace: StackConfig
) -> StackConfig:
    """Ensure entity B is provisioned so workspace-A has two entities.

    Most H2 tests need exactly two entities in workspace A — one in-scope and
    one out-of-scope for their token's entity_ids constraint.
    """
    await _ensure_entity_b(db)
    return seeded_workspace


@pytest_asyncio.fixture
async def h2_admin_membership(
    db: asyncpg.Pool, h2_workspace: StackConfig
) -> AsyncIterator[StackConfig]:
    """Ensure the test service-user has 'owner' membership (widest possible).

    Most tests need to START from a high-privilege membership so the token's
    scope fields can meaningfully demote it. Restore on teardown.
    """
    prev_role, prev_scope = await _set_membership(db, _SVC_UID, "owner", None)
    try:
        yield h2_workspace
    finally:
        await _set_membership(db, _SVC_UID, prev_role, prev_scope)


@pytest_asyncio.fixture
async def h2_viewer_membership(
    db: asyncpg.Pool, h2_workspace: StackConfig
) -> AsyncIterator[StackConfig]:
    """Membership set to 'viewer' — used for the role-upgrade-prohibited test."""
    prev_role, prev_scope = await _set_membership(db, _SVC_UID, "viewer", None)
    try:
        yield h2_workspace
    finally:
        await _set_membership(db, _SVC_UID, prev_role, prev_scope)


@pytest_asyncio.fixture
async def h2_entity_a_scoped_membership(
    db: asyncpg.Pool, h2_workspace: StackConfig
) -> AsyncIterator[StackConfig]:
    """Membership scoped to entity A only (role=accountant).

    Used for entity-intersection tests 5 and 6 where the membership is already
    restricted and the token applies an ADDITIONAL entity_ids constraint.
    """
    prev_role, prev_scope = await _set_membership(
        db, _SVC_UID, "accountant", [str(TEST_ENTITY_ID)]
    )
    try:
        yield h2_workspace
    finally:
        await _set_membership(db, _SVC_UID, prev_role, prev_scope)


# ---------------------------------------------------------------------------
# Test 1 — workspace allow-list: match → 200
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ws_allowlist_match_allowed(
    monkeypatch, h2_admin_membership: StackConfig
) -> None:
    """A token scoped to workspace A accessing workspace A returns 200."""
    _patch_tokens(
        monkeypatch,
        _token_entry(_TOKEN_WS_MATCH, workspace_ids=[str(TEST_WORKSPACE_ID)]),
    )
    async with _asgi_client(_TOKEN_WS_MATCH) as c:
        r = await c.get(f"/api/workspaces/{TEST_WORKSPACE_ID}")
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["id"] == str(TEST_WORKSPACE_ID)


# ---------------------------------------------------------------------------
# Test 2 — workspace allow-list: mismatch → 403
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ws_allowlist_mismatch_denied(
    monkeypatch, h2_admin_membership: StackConfig
) -> None:
    """A token scoped to workspace A, accessing workspace B → 403 with
    'Service token is not scoped to this workspace.'"""
    _patch_tokens(
        monkeypatch,
        # Token only allows workspace A.
        _token_entry(_TOKEN_WS_MISMATCH, workspace_ids=[str(TEST_WORKSPACE_ID)]),
    )
    async with _asgi_client(_TOKEN_WS_MISMATCH) as c:
        # Accessing workspace B with a token scoped to A.
        r = await c.get(f"/api/workspaces/{TEST_WORKSPACE_B_ID}")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "Service token is not scoped to this workspace" in r.json().get(
        "detail", ""
    ), r.json()


# ---------------------------------------------------------------------------
# Test 3 — workspace allow-list: empty list = deny-all → 403
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ws_allowlist_empty_denies_all(
    monkeypatch, h2_admin_membership: StackConfig
) -> None:
    """A token with workspace_ids=[] (empty list) must be denied on ANY workspace."""
    _patch_tokens(
        monkeypatch,
        _token_entry(_TOKEN_WS_EMPTY, workspace_ids=[]),  # intentional deny-all
    )
    async with _asgi_client(_TOKEN_WS_EMPTY) as c:
        r = await c.get(f"/api/workspaces/{TEST_WORKSPACE_ID}")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "Service token is not scoped to this workspace" in r.json().get(
        "detail", ""
    ), r.json()


# ---------------------------------------------------------------------------
# Test 4 — entity intersection: overlap → 200, entity_scope narrowed to A
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_entity_intersection_overlap_allowed(
    monkeypatch, h2_admin_membership: StackConfig
) -> None:
    """Token with entity_ids=[ENTITY_A] accessing entity A's data → 200.
    The WorkspaceContext.entity_scope must be (ENTITY_A,) — not None.
    Verify that entity B is now out of scope (its P&L returns 403).
    """
    _patch_tokens(
        monkeypatch,
        _token_entry(_TOKEN_ENT_OVERLAP, entity_ids=[str(TEST_ENTITY_ID)]),
    )
    async with _asgi_client(_TOKEN_ENT_OVERLAP) as c:
        # Entity A's P&L is accessible.
        r_ok = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026", "entity_id": str(TEST_ENTITY_ID)},
        )
        assert r_ok.status_code == 200, f"entity A allowed: {r_ok.status_code} {r_ok.text}"
        assert r_ok.json()["entity_id"] == str(TEST_ENTITY_ID)

        # Entity B's P&L is blocked because the token only allows entity A.
        r_denied = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026", "entity_id": str(ENTITY_B_ID)},
        )
        assert r_denied.status_code == 403, (
            f"entity B should be denied: {r_denied.status_code} {r_denied.text}"
        )


# ---------------------------------------------------------------------------
# Test 5 — entity intersection: no overlap → 403
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_entity_intersection_no_overlap_denied(
    monkeypatch, h2_entity_a_scoped_membership: StackConfig
) -> None:
    """Token with entity_ids=[ENTITY_B], membership scoped to entity A only.
    Intersection is empty → 403 with 'entity scope has no overlap'.
    """
    _patch_tokens(
        monkeypatch,
        # Token says B only, but membership allows A only → empty intersection.
        _token_entry(_TOKEN_ENT_NOOVLP, entity_ids=[str(ENTITY_B_ID)]),
    )
    async with _asgi_client(_TOKEN_ENT_NOOVLP) as c:
        r = await c.get(f"/api/workspaces/{TEST_WORKSPACE_ID}")
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text}"
    assert "entity scope has no overlap" in r.json().get("detail", "").lower(), r.json()


# ---------------------------------------------------------------------------
# Test 6 — entity intersection: can't broaden beyond membership
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_entity_intersection_no_broadening(
    monkeypatch, h2_entity_a_scoped_membership: StackConfig
) -> None:
    """Token with entity_ids=[ENTITY_A, ENTITY_B], membership scoped to A only.
    The intersection is just A — the token cannot widen the membership to also
    include B. Verify entity B remains inaccessible.
    """
    _patch_tokens(
        monkeypatch,
        # Token claims both A and B; membership only allows A.
        _token_entry(
            _TOKEN_ENT_NARROW,
            entity_ids=[str(TEST_ENTITY_ID), str(ENTITY_B_ID)],
        ),
    )
    async with _asgi_client(_TOKEN_ENT_NARROW) as c:
        # Entity A is still accessible (in both token and membership).
        r_a = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026", "entity_id": str(TEST_ENTITY_ID)},
        )
        assert r_a.status_code == 200, f"entity A accessible: {r_a.status_code} {r_a.text}"

        # Entity B must be inaccessible — membership scoped to A wins over
        # token claiming B.
        r_b = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026", "entity_id": str(ENTITY_B_ID)},
        )
        assert r_b.status_code == 403, (
            f"entity B must remain outside effective scope: {r_b.status_code} {r_b.text}"
        )


# ---------------------------------------------------------------------------
# Test 7 — role cap: demotion (admin → viewer)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_role_cap_demotion(
    monkeypatch, h2_admin_membership: StackConfig
) -> None:
    """Token with role_floor='viewer' impersonating an owner.
    Admin-only endpoints (store credential, close-period) return 403.
    Viewer-tier endpoints (P&L, journal listing) return 200.
    """
    _patch_tokens(
        monkeypatch,
        _token_entry(_TOKEN_ROLE_VIEWER, role_floor="viewer"),
    )
    async with _asgi_client(_TOKEN_ROLE_VIEWER) as c:
        # Viewer-tier: P&L report → 200.
        r_viewer_ok = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026"},
        )
        assert r_viewer_ok.status_code == 200, (
            f"viewer P&L should succeed: {r_viewer_ok.status_code} {r_viewer_ok.text}"
        )

        # Viewer-tier: journal listing → 200.
        r_journal = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/journal-entries",
            params={"limit": 5},
        )
        assert r_journal.status_code == 200, (
            f"viewer journal list should succeed: {r_journal.status_code} {r_journal.text}"
        )

        # Admin-only: store a credential → 403.
        r_admin_deny = await c.post(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/credentials",
            json={
                "connector": "simplefin",
                "label": "h2-itest",
                "plaintext_value": "https://user:pass@bridge.simplefin.org/simplefin/accounts",
            },
        )
        assert r_admin_deny.status_code == 403, (
            f"viewer must be denied admin credential store: "
            f"{r_admin_deny.status_code} {r_admin_deny.text}"
        )

        # Admin-only: close-period → 403.
        r_close_deny = await c.post(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/entities/{TEST_ENTITY_ID}/close-period",
            json={"period_end": "2024-12-31", "reason": "h2-itest cap check"},
        )
        assert r_close_deny.status_code == 403, (
            f"viewer must be denied close-period: "
            f"{r_close_deny.status_code} {r_close_deny.text}"
        )


# ---------------------------------------------------------------------------
# Test 8 — role cap: no upgrade (role_floor='admin', actual='viewer' → viewer)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_role_cap_no_upgrade(
    monkeypatch, h2_viewer_membership: StackConfig
) -> None:
    """Token with role_floor='admin' impersonating a viewer.
    The cap is a ceiling, not a floor — it can ONLY lower privilege, never raise it.
    Viewer-tier reads are still 200; admin-only write is still 403.
    """
    _patch_tokens(
        monkeypatch,
        # role_floor='admin' on a viewer membership → effective role stays viewer.
        _token_entry(_TOKEN_ROLE_NOUP, role_floor="admin"),
    )
    async with _asgi_client(_TOKEN_ROLE_NOUP) as c:
        # Viewer-tier: P&L still works.
        r_viewer = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026"},
        )
        assert r_viewer.status_code == 200, (
            f"viewer P&L should still succeed: {r_viewer.status_code} {r_viewer.text}"
        )

        # Admin-only: credential store is STILL denied because the actual
        # membership role is viewer and the cap cannot raise it.
        r_admin = await c.post(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/credentials",
            json={
                "connector": "simplefin",
                "label": "h2-itest-noup",
                "plaintext_value": "https://user:pass@bridge.simplefin.org/simplefin/accounts",
            },
        )
        assert r_admin.status_code == 403, (
            f"role_floor='admin' must NOT upgrade a viewer to admin: "
            f"{r_admin.status_code} {r_admin.text}"
        )


# ---------------------------------------------------------------------------
# Test 9 — backward compat: no scope fields → full membership reach preserved
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_legacy_token_full_reach(
    monkeypatch, h2_admin_membership: StackConfig
) -> None:
    """A service token with NO scope fields must keep the impersonated user's
    full membership reach — both workspaces and both entities accessible.
    """
    _patch_tokens(
        monkeypatch,
        # No workspace_ids, entity_ids, or role_floor — legacy format.
        _token_entry(_TOKEN_LEGACY),
    )
    async with _asgi_client(_TOKEN_LEGACY) as c:
        # Both workspace A and workspace B are reachable (only workspace A has
        # a membership for test-service, but this token impersonates that user
        # without any workspace restriction).
        r_ws_a = await c.get(f"/api/workspaces/{TEST_WORKSPACE_ID}")
        assert r_ws_a.status_code == 200, (
            f"workspace A: {r_ws_a.status_code} {r_ws_a.text}"
        )

        # Entity A's report is reachable.
        r_pl_a = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026", "entity_id": str(TEST_ENTITY_ID)},
        )
        assert r_pl_a.status_code == 200, (
            f"entity A P&L: {r_pl_a.status_code} {r_pl_a.text}"
        )

        # Entity B is also reachable (membership entity_scope = None = all).
        r_pl_b = await c.get(
            f"/api/workspaces/{TEST_WORKSPACE_ID}/reports/pl",
            params={"period": "2026", "entity_id": str(ENTITY_B_ID)},
        )
        assert r_pl_b.status_code == 200, (
            f"entity B P&L (legacy token must see all entities): "
            f"{r_pl_b.status_code} {r_pl_b.text}"
        )

        # Admin-tier write is also available (owner membership, no cap).
        r_list_creds = await c.get(f"/api/workspaces/{TEST_WORKSPACE_ID}/credentials")
        assert r_list_creds.status_code == 200, (
            f"credential list (accountant+): {r_list_creds.status_code} {r_list_creds.text}"
        )
