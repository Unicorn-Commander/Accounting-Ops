"""Rate limiting — per-tenant / per-principal, tiered, 429 + Retry-After.

Before this, the only HTTP middlewares were audit_span_middleware and the
mount-scoped MCPAuthMiddleware: no throttling of any kind. On a host running at
high CPU steal one abusive client could starve every other tenant.

The properties worth pinning:
  * 429 carries a usable Retry-After.
  * /healthz is NEVER throttled (throttling it breaks healthchecks + probes,
    turning a load spike into a false outage).
  * Limits are per-principal, not global — one caller cannot exhaust another's.
  * A tenant ceiling exists on top, so one workspace cannot starve the rest.
  * Expensive routes are tighter than cheap reads.
  * It can be switched off for single-tenant self-host / Community Edition.
"""
from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.rate_limit import (
    InProcessTokenBuckets,
    RateLimitConfig,
    RateLimiter,
    RateLimitMiddleware,
    classify_tier,
    extract_workspace,
    is_exempt,
    principal_key,
)

WS_A = "11111111-1111-4111-8111-111111111111"
WS_B = "22222222-2222-4222-8222-222222222222"


def _cfg(**over) -> RateLimitConfig:
    base = dict(
        enabled=True,
        per_min={"cheap": 5, "write": 3, "expensive": 2, "webhook": 50},
        tenant_factor=2.0,
        burst=1.0,  # capacity == limit, so counting is exact in tests
        trust_forwarded=True,
        redis_url=None,
    )
    base.update(over)
    return RateLimitConfig(**base)


def _limiter(**over) -> RateLimiter:
    return RateLimiter(cfg=_cfg(**over), backend=InProcessTokenBuckets())


def _hdrs(token: str | None = None) -> list[tuple[bytes, bytes]]:
    return [(b"authorization", f"Bearer {token}".encode())] if token else []


async def _hit(limiter: RateLimiter, path: str, token: str | None, method="GET"):
    return await limiter.check(
        path=path, method=method, headers=_hdrs(token), client_host="10.0.0.1"
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_healthz_is_exempt():
    assert is_exempt("/healthz") is True
    assert is_exempt("/readyz") is True
    assert is_exempt("/.well-known/mcp.json") is True
    assert is_exempt("/api/workspaces/x/transactions") is False


@pytest.mark.parametrize(
    "path,method,tier",
    [
        ("/api/chat/personas", "GET", "cheap"),
        ("/api/chat/personas", "HEAD", "cheap"),
        ("/api/chat/personas", "POST", "expensive"),
        ("/api/chat/personas-extra", "GET", "expensive"),
        ("/api/chat/completions", "POST", "expensive"),
        (f"/api/workspaces/{WS_A}/chat", "POST", "expensive"),
        (f"/api/workspaces/{WS_A}/reports/p-and-l", "GET", "expensive"),
        (f"/api/workspaces/{WS_A}/irs-forms/1120/render", "POST", "expensive"),
        (f"/api/workspaces/{WS_A}/cfo-brief", "GET", "expensive"),
        (f"/api/workspaces/{WS_A}/exports", "POST", "expensive"),
        (f"/api/workspaces/{WS_A}/trial-balance", "GET", "expensive"),
        ("/api/connectors/stripe/webhook", "POST", "webhook"),
        ("/mcp", "POST", "write"),
        (f"/api/workspaces/{WS_A}/transactions", "POST", "write"),
        (f"/api/workspaces/{WS_A}/transactions", "GET", "cheap"),
        (f"/api/workspaces/{WS_A}/members", "GET", "cheap"),
    ],
)
def test_cost_tier_classification(path, method, tier):
    assert classify_tier(path, method) == tier


def test_workspace_is_parsed_from_the_raw_path():
    """Middleware runs BEFORE routing, so path_params is empty — parse the URL."""
    assert extract_workspace(f"/api/workspaces/{WS_A}/reports") == WS_A
    assert extract_workspace("/api/workspaces/not-a-uuid/x") is None
    assert extract_workspace("/healthz") is None


def test_principal_key_never_contains_the_raw_credential():
    key = principal_key(_hdrs("super-secret-jwt"), "10.0.0.1", True)
    assert "super-secret-jwt" not in key
    assert key.startswith("t:")


def test_principal_key_is_stable_and_distinct():
    a1 = principal_key(_hdrs("tok-a"), "10.0.0.1", True)
    a2 = principal_key(_hdrs("tok-a"), "10.0.0.9", True)  # different IP
    b = principal_key(_hdrs("tok-b"), "10.0.0.1", True)
    assert a1 == a2  # identity follows the credential, not the address
    assert a1 != b


def test_unauthenticated_falls_back_to_forwarded_ip():
    hdrs = [(b"x-forwarded-for", b"203.0.113.7, 10.0.0.1")]
    assert principal_key(hdrs, "10.0.0.1", True) == "i:203.0.113.7"
    assert principal_key(hdrs, "10.0.0.1", False) == "i:10.0.0.1"


# ---------------------------------------------------------------------------
# Limiting behaviour
# ---------------------------------------------------------------------------


async def test_limit_trips_and_reports_retry_after():
    lim = _limiter()
    path = f"/api/workspaces/{WS_A}/transactions"
    for _ in range(5):  # cheap tier == 5/min
        assert (await _hit(lim, path, "tok")).allowed is True
    d = await _hit(lim, path, "tok")
    assert d.allowed is False
    assert d.retry_after >= 1


async def test_limits_are_per_principal_not_global():
    """Two users of the same workspace must not share one global bucket."""
    lim = _limiter()
    path = f"/api/workspaces/{WS_A}/transactions"
    for _ in range(5):
        assert (await _hit(lim, path, "user-a")).allowed is True
    assert (await _hit(lim, path, "user-a")).allowed is False
    # user-b is untouched by user-a exhausting their own budget.
    assert (await _hit(lim, path, "user-b")).allowed is True


async def test_office_nat_does_not_punish_colleagues():
    """Same source IP, different credentials -> independent budgets."""
    lim = _limiter()
    path = f"/api/workspaces/{WS_A}/transactions"

    async def hit(tok):
        return await lim.check(
            path=path, method="GET", headers=_hdrs(tok), client_host="198.51.100.10"
        )

    for _ in range(5):
        await hit("colleague-1")
    assert (await hit("colleague-1")).allowed is False
    assert (await hit("colleague-2")).allowed is True


async def test_tenant_ceiling_stops_one_workspace_starving_others():
    """Many principals in one workspace still hit an aggregate tenant cap."""
    lim = _limiter()  # cheap=5/principal, tenant_factor=2 -> tenant cap 10
    path_a = f"/api/workspaces/{WS_A}/transactions"

    allowed = 0
    for i in range(6):  # 6 distinct principals x 5 = 30 attempts available
        for _ in range(5):
            if (await _hit(lim, path_a, f"user-{i}")).allowed:
                allowed += 1
    assert allowed == 10, f"tenant ceiling not enforced (allowed={allowed})"

    # A DIFFERENT workspace is unaffected — that is the isolation guarantee.
    assert (await _hit(lim, f"/api/workspaces/{WS_B}/transactions", "other")).allowed


async def test_tenant_rejection_is_labelled_as_such():
    lim = _limiter()
    path = f"/api/workspaces/{WS_A}/transactions"
    last = None
    for i in range(6):
        for _ in range(5):
            last = await _hit(lim, path, f"user-{i}")
    assert last.allowed is False
    assert last.scope == "tenant"


async def test_expensive_routes_are_tighter_than_cheap_reads():
    lim = _limiter()
    for _ in range(2):  # expensive tier == 2/min
        assert (await _hit(lim, f"/api/workspaces/{WS_A}/chat", "tok", "POST")).allowed
    assert not (await _hit(lim, f"/api/workspaces/{WS_A}/chat", "tok", "POST")).allowed
    # Same principal still has its full cheap budget — buckets are per-tier.
    assert (await _hit(lim, f"/api/workspaces/{WS_A}/transactions", "tok")).allowed


async def test_healthz_is_never_limited_even_under_flood():
    lim = _limiter()
    for _ in range(500):
        assert await _hit(lim, "/healthz", None) is None


async def test_disabled_limiter_admits_everything():
    lim = _limiter(enabled=False)
    for _ in range(200):
        assert await _hit(lim, f"/api/workspaces/{WS_A}/chat", "tok", "POST") is None


def test_community_edition_defaults_to_disabled(monkeypatch):
    """A single-tenant self-hoster has no neighbours to protect."""
    monkeypatch.delenv("AO_RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.setattr("app.rate_limit._community_edition", lambda: True)
    assert RateLimitConfig.from_env().enabled is False

    monkeypatch.setattr("app.rate_limit._community_edition", lambda: False)
    assert RateLimitConfig.from_env().enabled is True


def test_explicit_env_overrides_the_edition_default(monkeypatch):
    monkeypatch.setattr("app.rate_limit._community_edition", lambda: True)
    monkeypatch.setenv("AO_RATE_LIMIT_ENABLED", "true")
    assert RateLimitConfig.from_env().enabled is True


def test_env_tunes_the_tier_limits(monkeypatch):
    monkeypatch.setenv("AO_RATE_LIMIT_EXPENSIVE_PER_MIN", "7")
    monkeypatch.setenv("AO_RATE_LIMIT_TENANT_FACTOR", "9")
    cfg = RateLimitConfig.from_env()
    assert cfg.limit_for("expensive") == 7
    assert cfg.tenant_factor == 9


async def test_bucket_refills_over_time():
    buckets = InProcessTokenBuckets()
    for _ in range(60):
        await buckets.consume("k", 60, 1.0)
    allowed, _, retry = await buckets.consume("k", 60, 1.0)
    assert allowed is False and retry >= 1
    # Rewind the stored timestamp by 10s: at 60/min that is ~10 tokens back.
    tokens, ts = buckets._buckets["k"]
    buckets._buckets["k"] = (tokens, ts - 10.0)
    assert (await buckets.consume("k", 60, 1.0))[0] is True


# ---------------------------------------------------------------------------
# End-to-end through the ASGI middleware
# ---------------------------------------------------------------------------


def _app(limiter: RateLimiter) -> Starlette:
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route("/healthz", ok),
        Route(f"/api/workspaces/{WS_A}/transactions", ok),
        Route(f"/api/workspaces/{WS_A}/chat", ok, methods=["POST"]),
    ])
    app.add_middleware(RateLimitMiddleware, limiter=limiter)
    return app


def test_429_response_shape_and_retry_after_header():
    client = TestClient(_app(_limiter()))
    path = f"/api/workspaces/{WS_A}/transactions"
    hdr = {"Authorization": "Bearer e2e-token"}

    for _ in range(5):
        assert client.get(path, headers=hdr).status_code == 200

    resp = client.get(path, headers=hdr)
    assert resp.status_code == 429
    assert "retry-after" in resp.headers
    assert int(resp.headers["retry-after"]) >= 1
    assert resp.headers["x-ratelimit-limit"] == "5"
    assert resp.headers["x-ratelimit-remaining"] == "0"
    body = resp.json()
    assert body["retry_after_seconds"] >= 1
    assert body["scope"] in ("principal", "tenant")


def test_healthz_survives_a_flood_that_429s_the_api():
    client = TestClient(_app(_limiter()))
    path = f"/api/workspaces/{WS_A}/transactions"
    hdr = {"Authorization": "Bearer flood"}

    for _ in range(40):
        client.get(path, headers=hdr)
    assert client.get(path, headers=hdr).status_code == 429

    # The probe must stay green while the API is shedding load.
    for _ in range(50):
        r = client.get("/healthz")
        assert r.status_code == 200
        assert "retry-after" not in r.headers


def test_successful_responses_advertise_remaining_budget():
    client = TestClient(_app(_limiter()))
    r = client.get(
        f"/api/workspaces/{WS_A}/transactions",
        headers={"Authorization": "Bearer budget"},
    )
    assert r.status_code == 200
    assert r.headers["x-ratelimit-limit"] == "5"
    assert int(r.headers["x-ratelimit-remaining"]) >= 0


def test_limiter_failure_fails_open():
    """A broken limiter must never become an outage."""

    class _Exploding(RateLimiter):
        async def check(self, **kw):
            raise RuntimeError("backend down")

    client = TestClient(_app(_Exploding(cfg=_cfg(), backend=InProcessTokenBuckets())))
    r = client.get(
        f"/api/workspaces/{WS_A}/transactions",
        headers={"Authorization": "Bearer x"},
    )
    assert r.status_code == 200
