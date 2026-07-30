"""HTTP-level contract for POST /api/me/personal-access-tokens.

Mint-side only. Proves the router refuses the two permissive defaults the
audit found, with the right status codes:

  * scopes omitted / null / [] / whitespace-only  -> 400 (NOT a silent grant
    of everything the owner's role allows).
  * ttl_days 0 or null ("never expires")          -> 400 (NOT an immortal
    credential).

The DB is stubbed out — these exercise the request-validation path, not
storage. Verification behaviour is covered in tests/smoke/test_pat_unit.py
(TestLegacyNullExpiryStillVerifies) and tests/integration/test_pat_auth.py.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import personal_access_tokens as pats
from app.auth import AuthContext, validate_bearer_token
from app.security.pat import DEFAULT_TTL_DAYS, MAX_TTL_DAYS, MintedPAT

OWNER_ID = uuid.uuid4()
OWNER_SCOPES = {"accounting:read", "accounting:write", "accounting:admin"}


class _FakeResult:
    def all(self):
        return []

    def mappings(self):
        return self


class _FakeSession:
    """No-op stand-in for AsyncSession — the router's real DB calls are all
    monkeypatched, this only satisfies the `async with SessionLocal()` shape."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def begin(self):
        return self

    async def execute(self, *_a, **_kw):
        return _FakeResult()


def _fake_session_local():
    return _FakeSession()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    """A FastAPI app carrying only the PAT router, with every DB touch stubbed.

    The last stub (``create_pat``) records what the router would have
    persisted so a test can assert on the minted shape without a database.
    """
    minted: dict[str, object] = {}

    async def _fake_ensure_user_account(uc_uid: str) -> uuid.UUID:
        return OWNER_ID

    async def _fake_owner_max_scopes(owner_user_id: uuid.UUID) -> set[str]:
        return set(OWNER_SCOPES)

    async def _fake_count_active_pats(session, owner_user_id) -> int:
        return 0

    async def _fake_create_pat(session, **kwargs) -> MintedPAT:
        minted.update(kwargs)
        return MintedPAT(
            id=uuid.uuid4(),
            name=kwargs["name"],
            secret="aops_pat_" + "x" * 43,
            prefix="aops_pat_xxx",
            scopes=kwargs["scopes"],
            workspace_scope=kwargs["workspace_scope"],
            expires_at=kwargs["expires_at"],
            created_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr(pats, "SessionLocal", _fake_session_local)
    monkeypatch.setattr(pats, "_ensure_user_account", _fake_ensure_user_account)
    monkeypatch.setattr(pats, "_owner_max_scopes", _fake_owner_max_scopes)
    monkeypatch.setattr(pats, "count_active_pats", _fake_count_active_pats)
    monkeypatch.setattr(pats, "create_pat", _fake_create_pat)

    app = FastAPI()
    app.include_router(pats.router)
    app.dependency_overrides[validate_bearer_token] = lambda: AuthContext(
        uc_uid="test-owner",
        agent_id=None,
        scopes=(),
        claims={},  # no pat_id -> a Keycloak session, allowed to mint
    )
    with TestClient(app) as c:
        c.minted = minted  # type: ignore[attr-defined]
        yield c


# ---------------------------------------------------------------------------
# Defect A — omitting scopes must not grant everything the owner has
# ---------------------------------------------------------------------------


class TestScopesAreRequired:
    def test_omitted_scopes_rejected(self, client: TestClient):
        r = client.post("/api/me/personal-access-tokens", json={"name": "x"})
        assert r.status_code == 400, r.text
        assert "scopes" in r.json()["detail"].lower()

    def test_null_scopes_rejected(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens", json={"name": "x", "scopes": None}
        )
        assert r.status_code == 400, r.text

    def test_empty_scopes_rejected(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens", json={"name": "x", "scopes": []}
        )
        assert r.status_code == 400, r.text

    def test_whitespace_only_scopes_rejected(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens",
            json={"name": "x", "scopes": ["  ", ""]},
        )
        assert r.status_code == 400, r.text

    def test_no_token_is_minted_when_scopes_are_missing(self, client: TestClient):
        client.post("/api/me/personal-access-tokens", json={"name": "x"})
        assert client.minted == {}, "a refused mint must not persist anything"

    def test_explicit_narrow_scope_succeeds(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens",
            json={"name": "x", "scopes": ["accounting:read"]},
        )
        assert r.status_code == 201, r.text
        assert r.json()["scopes"] == ["accounting:read"]

    def test_explicit_full_set_still_succeeds(self, client: TestClient):
        """Max privilege stays available — deliberately, not by default."""
        r = client.post(
            "/api/me/personal-access-tokens",
            json={"name": "x", "scopes": sorted(OWNER_SCOPES)},
        )
        assert r.status_code == 201, r.text
        assert set(r.json()["scopes"]) == OWNER_SCOPES

    def test_scope_the_owner_lacks_is_still_403(self, client: TestClient):
        """Unchanged behaviour: over-asking is a privilege problem (403), not
        a malformed request (400)."""
        r = client.post(
            "/api/me/personal-access-tokens",
            json={"name": "x", "scopes": ["sudo:everything"]},
        )
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Defect B — no never-expiring credentials from the mint path
# ---------------------------------------------------------------------------


class TestExpiryIsBounded:
    def test_ttl_zero_rejected(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens",
            json={"name": "x", "scopes": ["accounting:read"], "ttl_days": 0},
        )
        assert r.status_code == 400, r.text
        assert "ttl_days" in r.json()["detail"]

    def test_ttl_null_rejected(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens",
            json={"name": "x", "scopes": ["accounting:read"], "ttl_days": None},
        )
        assert r.status_code == 400, r.text

    def test_ttl_over_max_rejected(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens",
            json={
                "name": "x",
                "scopes": ["accounting:read"],
                "ttl_days": MAX_TTL_DAYS + 1,
            },
        )
        assert r.status_code in (400, 422), r.text

    def test_omitted_ttl_uses_the_90_day_default(self, client: TestClient):
        r = client.post(
            "/api/me/personal-access-tokens",
            json={"name": "x", "scopes": ["accounting:read"]},
        )
        assert r.status_code == 201, r.text
        assert r.json()["expires_at"] is not None
        assert client.minted["expires_at"] is not None  # type: ignore[index]

    def test_every_successful_mint_has_an_expiry(self, client: TestClient):
        for ttl in (1, 30, DEFAULT_TTL_DAYS, MAX_TTL_DAYS):
            r = client.post(
                "/api/me/personal-access-tokens",
                json={
                    "name": f"x-{ttl}",
                    "scopes": ["accounting:read"],
                    "ttl_days": ttl,
                },
            )
            assert r.status_code == 201, r.text
            assert r.json()["expires_at"] is not None, ttl


# ---------------------------------------------------------------------------
# _meta must not advertise the retired "never" option
# ---------------------------------------------------------------------------


class TestMetaCatalog:
    def test_ttl_choices_are_all_bounded(self, client: TestClient):
        r = client.get("/api/me/personal-access-tokens/_meta")
        assert r.status_code == 200, r.text
        choices = r.json()["ttl_choices_days"]
        assert 0 not in choices, "the mint UI must not offer a never-expiring token"
        assert all(0 < d <= MAX_TTL_DAYS for d in choices), choices
