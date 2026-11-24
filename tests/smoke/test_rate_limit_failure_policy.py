"""AO-SW-3: limiter errors are counted, alerted, and fail closed on the expensive tier."""
from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import app.rate_limit as rl

WS = "11111111-1111-4111-8111-111111111111"


class _Exploding(rl.RateLimiter):
    async def check(self, **kw):
        raise RuntimeError("backend down")


def _client() -> TestClient:
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[
        Route(f"/api/workspaces/{WS}/transactions", ok),
        Route(f"/api/workspaces/{WS}/chat", ok, methods=["POST"]),
    ])
    limiter = _Exploding(cfg=rl.RateLimitConfig.from_env(), backend=rl.InProcessTokenBuckets())
    app.add_middleware(rl.RateLimitMiddleware, limiter=limiter)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(rl, "LIMITER_FAILURES", {})
    monkeypatch.setattr(rl, "_alert_state", {"last": 0.0, "pending": 0})


def test_cheap_read_fails_open_but_is_counted_and_alerted(caplog):
    with caplog.at_level(logging.ERROR, logger="acctops.rate_limit"):
        r = _client().get(f"/api/workspaces/{WS}/transactions")
    assert r.status_code == 200
    assert rl.LIMITER_FAILURES == {("cheap", "fail_open"): 1}
    assert "rate_limit_check_failed" in caplog.text


def test_expensive_route_fails_closed_with_retry_after():
    r = _client().post(f"/api/workspaces/{WS}/chat")
    assert r.status_code == 503
    assert r.headers["retry-after"]
    assert "retry" in r.json()["detail"].lower()
    assert rl.LIMITER_FAILURES == {("expensive", "fail_closed"): 1}


def test_alert_log_is_throttled_but_every_failure_is_counted(caplog):
    client = _client()
    with caplog.at_level(logging.ERROR, logger="acctops.rate_limit"):
        for _ in range(5):
            client.get(f"/api/workspaces/{WS}/transactions")
    assert rl.LIMITER_FAILURES[("cheap", "fail_open")] == 5
    assert caplog.text.count("rate_limit_check_failed") == 1
