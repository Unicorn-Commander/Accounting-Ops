"""Outbound-LLM HTTP resilience: ONE User-Agent, ONE failover chain.

Why this module exists
======================

**(1) User-Agent.** The self-hosted UC gateway (``https://ai.magicunicorn.dev``)
sits behind Cloudflare bot-management, which rejects the OpenAI SDK's default
``User-Agent: OpenAI/Python <ver>`` with ``403`` + body ``error code: 1010``.
Verified by header bisection: ``python-httpx/*`` -> 200, ``AccountingOps/1.0``
-> 200, ``OpenAI/Python 1.99.1`` -> 403, ``Python-urllib/*`` -> 403. It is
UA-string-specific -- not the source IP, not the ``x-stainless-*`` headers.

Note that ``http_client=httpx.Client(headers={"User-Agent": ...})`` does **not**
fix it: the OpenAI SDK re-stamps its own UA on every request. The only thing
that works is ``default_headers={"User-Agent": ...}`` on the client constructor,
which is what :func:`build_async_openai` / :func:`build_openai` do.

The UA literal lives HERE and nowhere else, and is overridable via
``AO_LLM_USER_AGENT`` so a future Cloudflare rule that happens to block our
string can be worked around with an env change instead of a redeploy.

**(2) Failover.** Before this module AO had none: ``resolve_chat_inference`` is a
*precedence chain* (BYOK > per-workspace virtual key > platform virtual key >
OpenRouter) that picks ONE provider up front. If that provider was down, the
request just failed. This module wraps -- and never replaces -- that resolver:
whatever the resolver picked stays the primary, and an optional cloud fallback
catches infrastructure failures.

Configuration (failover is OFF unless ``LLM_FALLBACK_BASE_URL`` is set, in
which case behavior is byte-identical to before)::

    LLM_FALLBACK_BASE_URL=https://openrouter.ai/api/v1
    LLM_FALLBACK_API_KEY=sk-or-v1-...
    LLM_FALLBACK_MODEL=anthropic/claude-sonnet-4.5   # optional; else primary's

Failover fires on infrastructure faults only -- connection error, timeout,
408/425/429, 5xx, and the Cloudflare block signature on 403. It deliberately
does NOT fire on 400/401/402/404/405/409/422: those are *our* bugs (bad request,
bad key, wrong model name), and retrying them on a paid cloud provider just
burns spend on a request that will fail there too.

Bounded by construction: exactly one fallback attempt per call. The fallback
leg is a direct call, not a recursive one, so there is no retry loop and no
way to double-charge beyond that single extra attempt.

Every failover is logged at WARNING and stamped on the current OTel span
(``llm.failover``, ``llm.failover.reason``). A silent fallback that quietly
bills cloud tokens is a worse bug than the outage it papers over.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any, TypeVar

_log = logging.getLogger("acctops.llm")

T = TypeVar("T")

# ---------------------------------------------------------------------------
# User-Agent
# ---------------------------------------------------------------------------

_UA_PRODUCT = "AccountingOps"
_UA_CONTACT = "https://accounting-ops.unicorncommander.ai"


def app_version() -> str:
    """Installed package version; ``0`` when running from a source tree."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("accounting-ops")
        except PackageNotFoundError:
            return "0"
    except Exception:  # noqa: BLE001 - never let telemetry break a request
        return "0"


def llm_user_agent() -> str:
    """The User-Agent for every outbound LLM request.

    Precedence: ``AO_LLM_USER_AGENT`` > ``AO_CHAT_USER_AGENT`` (legacy, kept so
    an already-deployed prod env keeps working) > the built-in default.
    """
    for var in ("AO_LLM_USER_AGENT", "AO_CHAT_USER_AGENT"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    return f"{_UA_PRODUCT}/{app_version()} (+{_UA_CONTACT})"


def llm_default_headers(extra: Mapping[str, str | None] | None = None) -> dict[str, str]:
    """Headers for an outbound LLM client: our UA plus any caller extras.

    A caller-supplied ``User-Agent`` in ``extra`` wins, so a single call site can
    still special-case itself without editing this module.
    """
    headers: dict[str, str] = {"User-Agent": llm_user_agent()}
    for key, val in (extra or {}).items():
        if val is not None:
            headers[key] = val
    return headers


# ---------------------------------------------------------------------------
# Targets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmTarget:
    """One rung of the failover chain. NEVER logged with its ``api_key``."""

    base_url: str
    api_key: str
    model: str
    # "primary" | "fallback" -- for logging / span attributes / tests.
    label: str = "primary"

    def redacted(self) -> str:
        """Safe-to-log identity: endpoint + model, never the key."""
        return f"{self.label}={self.base_url} model={self.model}"


def resolve_fallback_target(primary_model: str | None = None) -> LlmTarget | None:
    """Read the fallback rung from env, or ``None`` when failover is disabled.

    Returns ``None`` -- making every helper here a pass-through -- unless
    ``LLM_FALLBACK_BASE_URL`` is set. That is what guarantees an unconfigured
    deployment behaves EXACTLY as it did before this module existed.
    """
    base_url = os.environ.get("LLM_FALLBACK_BASE_URL", "").strip()
    if not base_url:
        return None
    model = os.environ.get("LLM_FALLBACK_MODEL", "").strip() or (primary_model or "").strip()
    if not model:
        # No model to call with -> treat as unconfigured rather than sending a
        # request we know the provider will reject.
        _log.warning(
            "LLM_FALLBACK_BASE_URL is set but no fallback model could be resolved "
            "(set LLM_FALLBACK_MODEL) -- failover DISABLED."
        )
        return None
    return LlmTarget(
        base_url=base_url,
        api_key=os.environ.get("LLM_FALLBACK_API_KEY", "").strip(),
        model=model,
        label="fallback",
    )


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

# Infrastructure faults -> another provider plausibly succeeds.
_FAILOVER_STATUS = {408, 425, 429}
# Our bugs -> a second provider fails the same way, so don't pay twice.
_NEVER_FAILOVER_STATUS = {400, 401, 402, 404, 405, 409, 422}
# Cloudflare interstitials. 1010 = blocked UA (the bug this module was born for);
# 1015 = rate-limited; 1020 = firewall rule.
_CF_BLOCK_MARKERS = (
    "error code: 1010",
    "error code: 1015",
    "error code: 1020",
    "cloudflare",
    "attention required",
)


def _status_of(exc: BaseException) -> int | None:
    """HTTP status carried by an openai/httpx exception, if any."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None)
    return status if isinstance(status, int) else None


def _body_of(exc: BaseException) -> str:
    """Best-effort lowercase response body, for the Cloudflare sniff."""
    resp = getattr(exc, "response", None)
    for attr in ("text", "content"):
        raw = getattr(resp, attr, None)
        if isinstance(raw, bytes):
            try:
                return raw.decode("utf-8", "replace").lower()
            except Exception:  # noqa: BLE001
                continue
        if isinstance(raw, str) and raw:
            return raw.lower()
    body = getattr(exc, "body", None)
    if isinstance(body, str) and body:
        return body.lower()
    return str(exc).lower()


def _transport_fault(exc: BaseException) -> str | None:
    """``"timeout"`` / ``"connection"`` for transport-level faults, else None."""
    try:
        import httpx

        # TimeoutException is a subclass of TransportError -- check it first.
        if isinstance(exc, httpx.TimeoutException):
            return "timeout"
        if isinstance(exc, httpx.TransportError):
            return "connection"
    except ImportError:  # pragma: no cover - httpx is a hard dep in practice
        pass
    try:
        import openai

        # APITimeoutError is a subclass of APIConnectionError -- timeout first.
        if isinstance(exc, openai.APITimeoutError):
            return "timeout"
        if isinstance(exc, openai.APIConnectionError):
            return "connection"
    except ImportError:  # pragma: no cover
        pass
    # Duck-type fallback so a stubbed/mocked transport still classifies.
    name = type(exc).__name__
    if "Timeout" in name:
        return "timeout"
    if "Connect" in name or "Connection" in name:
        return "connection"
    return None


def classify_failure(exc: BaseException) -> tuple[bool, str]:
    """``(should_failover, reason)`` for one LLM call failure.

    ``reason`` is a short stable token suitable for logs, span attributes and
    test assertions.
    """
    status = _status_of(exc)

    if status is None:
        fault = _transport_fault(exc)
        if fault:
            return True, fault
        return False, f"unclassified:{type(exc).__name__}"

    if status == 403:
        body = _body_of(exc)
        if any(marker in body for marker in _CF_BLOCK_MARKERS):
            return True, "cloudflare_block_403"
        # A plain 403 is an authorization/budget decision -- our problem.
        return False, "http_403"

    if status in _NEVER_FAILOVER_STATUS:
        return False, f"http_{status}"
    if status in _FAILOVER_STATUS:
        return True, f"http_{status}"
    if 500 <= status < 600:
        return True, f"http_{status}"
    return False, f"http_{status}"


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------


def _annotate_span(reason: str, primary: LlmTarget, fallback: LlmTarget, op: str) -> None:
    """Stamp the live OTel span so a failover is visible in the trace."""
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - OTel optional
        return
    try:
        span = trace.get_current_span()
        if span is None or not getattr(span, "is_recording", lambda: False)():
            return
        span.set_attribute("llm.failover", True)
        span.set_attribute("llm.failover.reason", reason)
        span.set_attribute("llm.failover.op", op)
        span.set_attribute("llm.primary.base_url", primary.base_url)
        span.set_attribute("llm.primary.model", primary.model)
        span.set_attribute("llm.fallback.base_url", fallback.base_url)
        span.set_attribute("llm.fallback.model", fallback.model)
        span.add_event(
            "llm.failover",
            {"reason": reason, "op": op, "fallback.base_url": fallback.base_url},
        )
    except Exception:  # noqa: BLE001 - telemetry must never break a request
        return


def _log_failover(reason: str, exc: BaseException, primary: LlmTarget, fallback: LlmTarget, op: str) -> None:
    """LOUD warning. A silent fallback that bills cloud tokens is the worse bug."""
    _log.warning(
        "LLM FAILOVER [%s]: primary FAILED (%s: %s) -> retrying ONCE on fallback. "
        "primary=%s fallback=%s. This request will be billed to the fallback provider.",
        op,
        reason,
        exc,
        primary.redacted(),
        fallback.redacted(),
    )


def _is_fatal(exc: BaseException) -> bool:
    """Control-flow exceptions must propagate untouched."""
    return isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))


# ---------------------------------------------------------------------------
# Failover drivers
# ---------------------------------------------------------------------------


async def acall_with_failover(
    *,
    primary: LlmTarget,
    call: Callable[[LlmTarget], Awaitable[T]],
    fallback: LlmTarget | None = None,
    op: str = "chat.completions",
) -> T:
    """Await ``call(primary)``; on an infrastructure fault retry ONCE on fallback.

    ``call`` receives the target it should use, so it can pick the right client
    and substitute the target's model. Exactly one fallback attempt is made --
    the retry is a direct call, never recursive.
    """
    fb = fallback if fallback is not None else resolve_fallback_target(primary.model)
    try:
        return await call(primary)
    except Exception as exc:
        if _is_fatal(exc):
            raise
        should, reason = classify_failure(exc)
        if not should or fb is None:
            raise
        _log_failover(reason, exc, primary, fb, op)
        _annotate_span(reason, primary, fb, op)
        return await call(fb)


def call_with_failover(
    *,
    primary: LlmTarget,
    call: Callable[[LlmTarget], T],
    fallback: LlmTarget | None = None,
    op: str = "chat.completions",
) -> T:
    """Synchronous twin of :func:`acall_with_failover`."""
    fb = fallback if fallback is not None else resolve_fallback_target(primary.model)
    try:
        return call(primary)
    except Exception as exc:
        if _is_fatal(exc):
            raise
        should, reason = classify_failure(exc)
        if not should or fb is None:
            raise
        _log_failover(reason, exc, primary, fb, op)
        _annotate_span(reason, primary, fb, op)
        return call(fb)


# ---------------------------------------------------------------------------
# Client builders (UA-correct by construction)
# ---------------------------------------------------------------------------


def build_async_openai(
    target: LlmTarget,
    *,
    extra_headers: Mapping[str, str | None] | None = None,
    **kwargs: Any,
) -> Any:
    """``openai.AsyncOpenAI`` with our User-Agent actually applied.

    ``default_headers`` is the ONLY hook the SDK honors -- see module docstring.
    """
    import openai

    return openai.AsyncOpenAI(
        base_url=target.base_url,
        api_key=target.api_key,
        default_headers=llm_default_headers(extra_headers),
        **kwargs,
    )


def build_openai(
    target: LlmTarget,
    *,
    extra_headers: Mapping[str, str | None] | None = None,
    **kwargs: Any,
) -> Any:
    """Synchronous twin of :func:`build_async_openai`."""
    import openai

    return openai.OpenAI(
        base_url=target.base_url,
        api_key=target.api_key,
        default_headers=llm_default_headers(extra_headers),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Drop-in failover clients
# ---------------------------------------------------------------------------
#
# These expose just enough of the OpenAI client surface
# (``.chat.completions.create``) that an existing call site swaps its
# constructor and gains failover with no other edits. Everything else proxies
# to the primary client, so ``.models.list()`` etc. still work.


class _AsyncCompletions:
    def __init__(self, owner: "FailoverAsyncClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> Any:
        return await self._owner._create(**kwargs)


class _AsyncChat:
    def __init__(self, owner: "FailoverAsyncClient") -> None:
        self.completions = _AsyncCompletions(owner)


class _SyncCompletions:
    def __init__(self, owner: "FailoverClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        return self._owner._create(**kwargs)


class _SyncChat:
    def __init__(self, owner: "FailoverClient") -> None:
        self.completions = _SyncCompletions(owner)


class _FailoverClientBase:
    """Shared plumbing for the sync/async failover clients."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str = "",
        extra_headers: Mapping[str, str | None] | None = None,
        fallback: LlmTarget | None = None,
        op: str = "chat.completions",
        **client_kwargs: Any,
    ) -> None:
        self.primary = LlmTarget(base_url=base_url, api_key=api_key, model=model, label="primary")
        self.fallback = fallback if fallback is not None else resolve_fallback_target(model or None)
        self.op = op
        self._extra_headers = extra_headers
        self._client_kwargs = client_kwargs
        # Which rung served the most recent call -- so a caller can report the
        # model it ACTUALLY used instead of the one it asked for.
        self.last_served_by: str = "primary"
        self.last_model: str = model

    @property
    def failover_enabled(self) -> bool:
        return self.fallback is not None

    def _target_for(self, target: LlmTarget, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Per-attempt kwargs: substitute the rung's model when it has one."""
        out = dict(kwargs)
        if target.label == "fallback" and target.model:
            out["model"] = target.model
        return out

    def _note(self, target: LlmTarget, kwargs: Mapping[str, Any]) -> None:
        self.last_served_by = target.label
        self.last_model = str(kwargs.get("model") or target.model)


class FailoverAsyncClient(_FailoverClientBase):
    """Async OpenAI-compatible client with a bounded primary -> fallback chain.

    Drop-in for ``openai.AsyncOpenAI`` at call sites that only use
    ``.chat.completions.create``. Streaming is preserved: ``create(stream=True)``
    returns the ``AsyncStream`` of whichever rung answered.

    Note that failover covers *establishing* the stream, not mid-stream faults --
    once tokens have been emitted to the user we cannot silently replay the
    request on another provider without duplicating output.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._primary_client = build_async_openai(
            self.primary, extra_headers=self._extra_headers, **self._client_kwargs
        )
        self._fallback_client = (
            build_async_openai(self.fallback, extra_headers=self._extra_headers, **self._client_kwargs)
            if self.fallback is not None
            else None
        )
        self.chat = _AsyncChat(self)

    def __getattr__(self, name: str) -> Any:
        # Anything we don't wrap (models.list, embeddings, ...) hits the primary.
        return getattr(self.__dict__["_primary_client"], name)

    async def _create(self, **kwargs: Any) -> Any:
        model = str(kwargs.get("model") or self.primary.model)
        primary = replace(self.primary, model=model)

        async def _attempt(target: LlmTarget) -> Any:
            client = self._primary_client if target.label == "primary" else self._fallback_client
            call_kwargs = self._target_for(target, kwargs)
            self._note(target, call_kwargs)
            return await client.chat.completions.create(**call_kwargs)

        return await acall_with_failover(
            primary=primary, call=_attempt, fallback=self.fallback, op=self.op
        )


class FailoverClient(_FailoverClientBase):
    """Synchronous twin of :class:`FailoverAsyncClient`."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._primary_client = build_openai(
            self.primary, extra_headers=self._extra_headers, **self._client_kwargs
        )
        self._fallback_client = (
            build_openai(self.fallback, extra_headers=self._extra_headers, **self._client_kwargs)
            if self.fallback is not None
            else None
        )
        self.chat = _SyncChat(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_primary_client"], name)

    def _create(self, **kwargs: Any) -> Any:
        model = str(kwargs.get("model") or self.primary.model)
        primary = replace(self.primary, model=model)

        def _attempt(target: LlmTarget) -> Any:
            client = self._primary_client if target.label == "primary" else self._fallback_client
            call_kwargs = self._target_for(target, kwargs)
            self._note(target, call_kwargs)
            return client.chat.completions.create(**call_kwargs)

        return call_with_failover(
            primary=primary, call=_attempt, fallback=self.fallback, op=self.op
        )
