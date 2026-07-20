"""Unit tests for app.llm_client -- the User-Agent + failover layer.

Two bugs are being permanently pinned here:

1. **Cloudflare 1010.** The UC gateway 403s the OpenAI SDK's default
   ``User-Agent: OpenAI/Python <ver>``. Our UA must never regress to an
   SDK/urllib default, and the literal must live in exactly ONE module.

2. **No failover.** AO's resolver picks ONE provider up front; if it was down
   the request just failed. Failover must fire on infrastructure faults, must
   NOT fire on our own 4xx bugs (which would burn cloud spend on a request that
   fails there too), and must be bounded to a single extra attempt.
"""
from __future__ import annotations

import asyncio
import pathlib

import httpx
import pytest

from app import llm_client
from app.llm_client import (
    FailoverAsyncClient,
    FailoverClient,
    LlmTarget,
    acall_with_failover,
    call_with_failover,
    classify_failure,
    llm_default_headers,
    llm_user_agent,
    resolve_fallback_target,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

FALLBACK_ENV = ("LLM_FALLBACK_BASE_URL", "LLM_FALLBACK_API_KEY", "LLM_FALLBACK_MODEL")
UA_ENV = ("AO_LLM_USER_AGENT", "AO_CHAT_USER_AGENT")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from 'failover unconfigured, UA at its default'."""
    for var in FALLBACK_ENV + UA_ENV:
        monkeypatch.delenv(var, raising=False)


def _configure_fallback(monkeypatch, *, model: str | None = "cloud-model"):
    monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setenv("LLM_FALLBACK_API_KEY", "sk-or-v1-test")
    if model is not None:
        monkeypatch.setenv("LLM_FALLBACK_MODEL", model)


# ---------------------------------------------------------------------------
# Exception builders -- real httpx objects, so we test real attribute shapes.
# ---------------------------------------------------------------------------


def _status_error(status: int, body: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://ai.magicunicorn.dev/v1/chat/completions")
    response = httpx.Response(status_code=status, text=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def _cf_1010_error() -> httpx.HTTPStatusError:
    """The exact signature Cloudflare returns for a blocked User-Agent."""
    return _status_error(403, "error code: 1010")


PRIMARY = LlmTarget(
    base_url="https://ai.magicunicorn.dev/v1",
    api_key="sk-primary",
    model="qwen3.6-35b",
    label="primary",
)


# ===========================================================================
# User-Agent
# ===========================================================================


class TestUserAgent:
    def test_default_is_descriptive_and_not_an_sdk_default(self):
        ua = llm_user_agent()
        assert ua.startswith("AccountingOps/")
        assert "accounting-ops.unicorncommander.ai" in ua
        # The regression that caused the outage: any of these UAs get 403 + 1010.
        for blocked in ("OpenAI/Python", "Python-urllib", "python-requests"):
            assert blocked not in ua

    def test_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("AO_LLM_USER_AGENT", "CustomUA/9.9")
        assert llm_user_agent() == "CustomUA/9.9"

    def test_legacy_chat_ua_env_still_honored(self, monkeypatch):
        """A prod box already setting AO_CHAT_USER_AGENT must keep working."""
        monkeypatch.setenv("AO_CHAT_USER_AGENT", "LegacyUA/1.0")
        assert llm_user_agent() == "LegacyUA/1.0"

    def test_new_env_beats_legacy(self, monkeypatch):
        monkeypatch.setenv("AO_CHAT_USER_AGENT", "LegacyUA/1.0")
        monkeypatch.setenv("AO_LLM_USER_AGENT", "NewUA/2.0")
        assert llm_user_agent() == "NewUA/2.0"

    def test_default_headers_carry_ua_and_merge_extras(self):
        headers = llm_default_headers({"Content-Type": "application/json", "X-Title": "AO"})
        assert headers["User-Agent"] == llm_user_agent()
        assert headers["Content-Type"] == "application/json"
        assert headers["X-Title"] == "AO"

    def test_default_headers_drops_none_extras(self):
        assert "X-Nope" not in llm_default_headers({"X-Nope": None})

    def test_no_llm_call_site_hardcodes_a_user_agent(self):
        """Source-grep guard: LLM call sites must get their UA from llm_client.

        The point of app/llm_client.py is that a future Cloudflare rule is an env
        change, not a scavenger hunt across 12 files. Scoped to modules that talk
        to an LLM endpoint -- unrelated HTTP clients (Keycloak, Brigade,
        Contact-Ops, RSS/web scrapers) legitimately identify themselves
        differently to third parties and are NOT covered by this rule.
        """
        llm_modules = [
            REPO_ROOT / "app" / "categorization" / "llm.py",
            REPO_ROOT / "app" / "categorization" / "receipt_vlm.py",
            REPO_ROOT / "app" / "categorization" / "tax_doc_vlm.py",
            REPO_ROOT / "agents" / "chat" / "runner.py",
            *sorted((REPO_ROOT / "agents" / "tax_specialists").glob("*/run_specialist.py")),
            *sorted((REPO_ROOT / "agents" / "cpa_team").rglob("run_*.py")),
        ]
        # Guard the guard: if the layout moves, this test must not silently pass.
        assert len(llm_modules) >= 14, f"LLM call-site list looks wrong: {llm_modules}"

        offenders = []
        for path in llm_modules:
            assert path.exists(), f"expected LLM module missing: {path}"
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # prose about the UA is fine; a literal is not
                if '"User-Agent"' in line or "'User-Agent'" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
        assert not offenders, "LLM call site hardcodes a User-Agent:\n" + "\n".join(offenders)

    def test_every_openai_client_is_built_through_llm_client(self):
        """No module may construct openai.OpenAI/AsyncOpenAI directly.

        A bare constructor gets the SDK's default UA -> Cloudflare 403 + 1010.
        app/llm_client.py is the ONLY place allowed to call the constructors.
        """
        offenders = []
        for path in list((REPO_ROOT / "app").rglob("*.py")) + list(
            (REPO_ROOT / "agents").rglob("*.py")
        ):
            if path.name == "llm_client.py":
                continue
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "openai.AsyncOpenAI(" in line or "openai.OpenAI(" in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {stripped}")
        assert not offenders, (
            "bare OpenAI constructor (bypasses the User-Agent fix):\n" + "\n".join(offenders)
        )


# ===========================================================================
# Fallback resolution
# ===========================================================================


class TestResolveFallback:
    def test_unconfigured_returns_none(self):
        """Failover OFF by default -- identical to pre-failover behavior."""
        assert resolve_fallback_target("qwen3.6-35b") is None

    def test_configured_returns_target(self, monkeypatch):
        _configure_fallback(monkeypatch)
        target = resolve_fallback_target("qwen3.6-35b")
        assert target is not None
        assert target.base_url == "https://openrouter.ai/api/v1"
        assert target.api_key == "sk-or-v1-test"
        assert target.model == "cloud-model"
        assert target.label == "fallback"

    def test_inherits_primary_model_when_unset(self, monkeypatch):
        _configure_fallback(monkeypatch, model=None)
        target = resolve_fallback_target("qwen3.6-35b")
        assert target is not None and target.model == "qwen3.6-35b"

    def test_disabled_when_no_model_resolvable(self, monkeypatch):
        """Better to stay on today's behavior than send a request we know fails."""
        _configure_fallback(monkeypatch, model=None)
        assert resolve_fallback_target(None) is None

    def test_blank_base_url_is_unconfigured(self, monkeypatch):
        monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "   ")
        assert resolve_fallback_target("m") is None

    def test_redacted_never_leaks_the_key(self, monkeypatch):
        _configure_fallback(monkeypatch)
        target = resolve_fallback_target("m")
        assert "sk-or-v1-test" not in target.redacted()


# ===========================================================================
# Failure classification
# ===========================================================================


class TestClassifyFailure:
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_5xx_fails_over(self, status):
        should, reason = classify_failure(_status_error(status))
        assert should is True
        assert reason == f"http_{status}"

    @pytest.mark.parametrize("status", [408, 425, 429])
    def test_retryable_4xx_fails_over(self, status):
        should, _ = classify_failure(_status_error(status))
        assert should is True

    @pytest.mark.parametrize("status", [400, 401, 402, 404, 405, 409, 422])
    def test_client_errors_do_not_fail_over(self, status):
        """These are OUR bugs -- a second provider fails identically. Don't pay twice."""
        should, reason = classify_failure(_status_error(status))
        assert should is False
        assert reason == f"http_{status}"

    def test_cloudflare_1010_fails_over(self):
        should, reason = classify_failure(_cf_1010_error())
        assert should is True
        assert reason == "cloudflare_block_403"

    @pytest.mark.parametrize(
        "body", ["error code: 1015", "error code: 1020", "Attention Required! | Cloudflare"]
    )
    def test_other_cloudflare_interstitials_fail_over(self, body):
        should, _ = classify_failure(_status_error(403, body))
        assert should is True

    def test_plain_403_does_not_fail_over(self):
        """An unauthorized / over-budget key is a real decision, not an outage."""
        should, reason = classify_failure(_status_error(403, '{"error":"invalid api key"}'))
        assert should is False
        assert reason == "http_403"

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ReadTimeout("read timed out"),
            httpx.ConnectTimeout("connect timed out"),
            httpx.PoolTimeout("pool timed out"),
        ],
    )
    def test_timeouts_fail_over(self, exc):
        should, reason = classify_failure(exc)
        assert should is True
        assert reason == "timeout"

    @pytest.mark.parametrize(
        "exc", [httpx.ConnectError("dns failure"), httpx.ReadError("peer reset")]
    )
    def test_connection_errors_fail_over(self, exc):
        should, reason = classify_failure(exc)
        assert should is True
        assert reason == "connection"

    def test_unknown_exception_does_not_fail_over(self):
        should, reason = classify_failure(ValueError("bad json"))
        assert should is False
        assert "ValueError" in reason

    def test_openai_sdk_status_error_classified(self):
        """The real SDK exception shape, when the SDK is installed."""
        openai = pytest.importorskip("openai")
        request = httpx.Request("POST", "https://ai.magicunicorn.dev/v1/chat/completions")
        response = httpx.Response(503, text="upstream down", request=request)
        exc = openai.APIStatusError("boom", response=response, body=None)
        should, reason = classify_failure(exc)
        assert should is True
        assert reason == "http_503"


# ===========================================================================
# Failover driver -- the money-relevant behavior
# ===========================================================================


class _Recorder:
    """Records which rungs were called, so we can assert on spend."""

    def __init__(self, *, primary_exc=None, fallback_exc=None):
        self.calls: list[str] = []
        self.primary_exc = primary_exc
        self.fallback_exc = fallback_exc

    def _run(self, target: LlmTarget):
        self.calls.append(target.label)
        exc = self.primary_exc if target.label == "primary" else self.fallback_exc
        if exc is not None:
            raise exc
        return f"ok:{target.label}:{target.model}"

    async def acall(self, target: LlmTarget):
        return self._run(target)

    def call(self, target: LlmTarget):
        return self._run(target)


class TestFailoverDriver:
    async def test_fires_on_5xx(self, monkeypatch):
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=_status_error(503))
        result = await acall_with_failover(primary=PRIMARY, call=rec.acall)
        assert result == "ok:fallback:cloud-model"
        assert rec.calls == ["primary", "fallback"]

    async def test_fires_on_timeout(self, monkeypatch):
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=httpx.ReadTimeout("timed out"))
        assert await acall_with_failover(primary=PRIMARY, call=rec.acall) == "ok:fallback:cloud-model"
        assert rec.calls == ["primary", "fallback"]

    async def test_fires_on_cloudflare_1010(self, monkeypatch):
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=_cf_1010_error())
        assert await acall_with_failover(primary=PRIMARY, call=rec.acall) == "ok:fallback:cloud-model"
        assert rec.calls == ["primary", "fallback"]

    @pytest.mark.parametrize("status", [400, 401, 404])
    async def test_does_not_fire_on_client_errors(self, monkeypatch, status):
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=_status_error(status))
        with pytest.raises(httpx.HTTPStatusError):
            await acall_with_failover(primary=PRIMARY, call=rec.acall)
        assert rec.calls == ["primary"], "must NOT burn cloud spend on our own 4xx"

    async def test_inert_when_unconfigured(self):
        """No LLM_FALLBACK_BASE_URL -> exactly today's behavior: one call, raise."""
        rec = _Recorder(primary_exc=_status_error(503))
        with pytest.raises(httpx.HTTPStatusError):
            await acall_with_failover(primary=PRIMARY, call=rec.acall)
        assert rec.calls == ["primary"]

    async def test_success_never_touches_fallback(self, monkeypatch):
        _configure_fallback(monkeypatch)
        rec = _Recorder()
        assert await acall_with_failover(primary=PRIMARY, call=rec.acall) == "ok:primary:qwen3.6-35b"
        assert rec.calls == ["primary"]

    async def test_at_most_one_fallback_attempt(self, monkeypatch):
        """Bounded: a failing fallback must NOT retry -- no loop, no double charge."""
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=_status_error(503), fallback_exc=_status_error(503))
        with pytest.raises(httpx.HTTPStatusError):
            await acall_with_failover(primary=PRIMARY, call=rec.acall)
        assert rec.calls == ["primary", "fallback"]
        assert len(rec.calls) == 2

    async def test_fallback_error_propagates(self, monkeypatch):
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=_status_error(503), fallback_exc=ValueError("cloud broke"))
        with pytest.raises(ValueError, match="cloud broke"):
            await acall_with_failover(primary=PRIMARY, call=rec.acall)

    async def test_cancellation_is_never_failed_over(self, monkeypatch):
        """A cancelled request is not an outage -- don't spend money on it."""
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await acall_with_failover(primary=PRIMARY, call=rec.acall)
        assert rec.calls == ["primary"]

    async def test_logs_loudly_on_failover(self, monkeypatch, caplog):
        """A silent fallback that bills cloud tokens is worse than the outage."""
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=_status_error(503))
        with caplog.at_level("WARNING", logger="acctops.llm"):
            await acall_with_failover(primary=PRIMARY, call=rec.acall)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings, "failover must emit a WARNING"
        msg = warnings[0].getMessage()
        assert "FAILOVER" in msg
        assert "http_503" in msg
        assert "openrouter.ai" in msg
        assert "sk-or-v1-test" not in msg, "must never log key material"
        assert "sk-primary" not in msg

    def test_sync_driver_fires(self, monkeypatch):
        _configure_fallback(monkeypatch)
        rec = _Recorder(primary_exc=_status_error(502))
        assert call_with_failover(primary=PRIMARY, call=rec.call) == "ok:fallback:cloud-model"
        assert rec.calls == ["primary", "fallback"]

    def test_sync_driver_inert_when_unconfigured(self):
        rec = _Recorder(primary_exc=_status_error(502))
        with pytest.raises(httpx.HTTPStatusError):
            call_with_failover(primary=PRIMARY, call=rec.call)
        assert rec.calls == ["primary"]


# ===========================================================================
# Drop-in clients
# ===========================================================================


class _FakeCompletions:
    def __init__(self, owner):
        self._owner = owner

    async def create(self, **kwargs):
        self._owner.calls.append(kwargs)
        if self._owner.exc is not None:
            raise self._owner.exc
        return f"stream:{self._owner.name}:{kwargs.get('model')}"


class _FakeSyncCompletions(_FakeCompletions):
    def create(self, **kwargs):  # type: ignore[override]
        self._owner.calls.append(kwargs)
        if self._owner.exc is not None:
            raise self._owner.exc
        return f"resp:{self._owner.name}:{kwargs.get('model')}"


class _FakeClient:
    def __init__(self, name, exc=None, sync=False):
        self.name = name
        self.exc = exc
        self.calls: list[dict] = []
        completions = _FakeSyncCompletions(self) if sync else _FakeCompletions(self)
        self.chat = type("Chat", (), {"completions": completions})()
        self.default_headers: dict[str, str] = {}


@pytest.fixture
def fake_clients(monkeypatch):
    """Swap the real OpenAI constructors so these run without the SDK installed."""
    built: dict[str, _FakeClient] = {}

    def _factory(sync: bool):
        def _build(target, *, extra_headers=None, **kwargs):
            client = _FakeClient(target.label, sync=sync)
            client.default_headers = llm_default_headers(extra_headers)
            built[target.label] = client
            return client

        return _build

    monkeypatch.setattr(llm_client, "build_async_openai", _factory(sync=False))
    monkeypatch.setattr(llm_client, "build_openai", _factory(sync=True))
    return built


class TestFailoverAsyncClient:
    async def test_passes_through_on_success(self, fake_clients):
        client = FailoverAsyncClient(
            base_url=PRIMARY.base_url, api_key=PRIMARY.api_key, model=PRIMARY.model
        )
        assert client.failover_enabled is False
        result = await client.chat.completions.create(model="qwen3.6-35b", messages=[])
        assert result == "stream:primary:qwen3.6-35b"
        assert client.last_served_by == "primary"
        assert "fallback" not in fake_clients

    async def test_carries_the_user_agent(self, fake_clients):
        FailoverAsyncClient(
            base_url=PRIMARY.base_url, api_key=PRIMARY.api_key, model=PRIMARY.model
        )
        assert fake_clients["primary"].default_headers["User-Agent"] == llm_user_agent()

    async def test_merges_extra_headers_with_ua(self, fake_clients):
        FailoverAsyncClient(
            base_url=PRIMARY.base_url,
            api_key=PRIMARY.api_key,
            model=PRIMARY.model,
            extra_headers={"X-Title": "Accounting-Ops Chat"},
        )
        headers = fake_clients["primary"].default_headers
        assert headers["X-Title"] == "Accounting-Ops Chat"
        assert headers["User-Agent"] == llm_user_agent()

    async def test_fails_over_and_substitutes_the_fallback_model(self, monkeypatch, fake_clients):
        _configure_fallback(monkeypatch)
        client = FailoverAsyncClient(
            base_url=PRIMARY.base_url, api_key=PRIMARY.api_key, model=PRIMARY.model
        )
        assert client.failover_enabled is True
        fake_clients["primary"].exc = _status_error(503)

        result = await client.chat.completions.create(model="qwen3.6-35b", messages=[])

        assert result == "stream:fallback:cloud-model"
        assert client.last_served_by == "fallback"
        assert client.last_model == "cloud-model"
        # The primary's model must NOT leak onto the fallback provider.
        assert fake_clients["fallback"].calls[0]["model"] == "cloud-model"
        assert len(fake_clients["primary"].calls) == 1
        assert len(fake_clients["fallback"].calls) == 1

    async def test_streaming_kwarg_is_preserved(self, monkeypatch, fake_clients):
        _configure_fallback(monkeypatch)
        client = FailoverAsyncClient(
            base_url=PRIMARY.base_url, api_key=PRIMARY.api_key, model=PRIMARY.model
        )
        fake_clients["primary"].exc = _status_error(503)
        await client.chat.completions.create(model="qwen3.6-35b", messages=[], stream=True)
        assert fake_clients["fallback"].calls[0]["stream"] is True

    async def test_no_failover_on_400(self, monkeypatch, fake_clients):
        _configure_fallback(monkeypatch)
        client = FailoverAsyncClient(
            base_url=PRIMARY.base_url, api_key=PRIMARY.api_key, model=PRIMARY.model
        )
        fake_clients["primary"].exc = _status_error(400)
        with pytest.raises(httpx.HTTPStatusError):
            await client.chat.completions.create(model="qwen3.6-35b", messages=[])
        assert fake_clients["fallback"].calls == []


class TestFailoverSyncClient:
    def test_fails_over(self, monkeypatch, fake_clients):
        _configure_fallback(monkeypatch)
        client = FailoverClient(
            base_url=PRIMARY.base_url, api_key=PRIMARY.api_key, model=PRIMARY.model
        )
        fake_clients["primary"].exc = _status_error(500)
        assert (
            client.chat.completions.create(model="qwen3.6-35b", messages=[])
            == "resp:fallback:cloud-model"
        )
        assert client.last_served_by == "fallback"

    def test_inert_when_unconfigured(self, fake_clients):
        client = FailoverClient(
            base_url=PRIMARY.base_url, api_key=PRIMARY.api_key, model=PRIMARY.model
        )
        assert client.failover_enabled is False
        fake_clients["primary"].exc = _status_error(500)
        with pytest.raises(httpx.HTTPStatusError):
            client.chat.completions.create(model="qwen3.6-35b", messages=[])
        assert "fallback" not in fake_clients
