"""Per-tenant / per-principal request rate limiting.

Why this exists
===============
Before this module the ONLY HTTP middlewares registered in ``app/main.py`` were
``audit_span_middleware`` and ``MCPAuthMiddleware`` (mount-scoped to ``/mcp``).
There was no per-tenant limiting, no global limiting, and no throttling of any
kind. On a production host already running at very high CPU steal, one abusive
or merely buggy client — a retry loop hammering report generation, say — can
starve every other tenant on the box. Rate limiting here is a
noisy-neighbour/tenant-isolation control, not an anti-DDoS control.

What it limits on
=================
Two independent buckets, BOTH of which must admit a request:

  1. **Tenant** — keyed on the ``workspace_id`` in the URL path. Caps what a
     single workspace can consume in aggregate, no matter how many users or
     API tokens it spreads the load across. This is the bucket that actually
     prevents cross-tenant starvation.
  2. **Principal** — keyed on a SHA-256 fingerprint of the bearer credential
     (never the credential itself). Caps a single user/agent/token, so one
     runaway script does not consume its whole workspace's allowance.

Deliberately NOT primarily IP-keyed: an accounting firm NATs its whole office
behind one address, so IP buckets would throttle innocent colleagues together
while a multi-homed abuser walks straight through. IP is used only as a
last-resort key for unauthenticated requests (OAuth callbacks, webhooks), which
have no principal to key on.

Cost tiers
==========
A trial-balance read and a 40-page PDF render are not the same load, so limits
are tiered by cost (``TIER_RULES``, first match wins):

  ``expensive``  LLM chat, form/PDF render, report + export generation, CFO
                 brief, specialist runs. Tight.
  ``write``      Any other mutating verb, plus the ``/mcp`` agent surface.
  ``webhook``    Unauthenticated provider callbacks (Stripe/eBay). Generous —
                 these are legitimately bursty and dropping one loses an event.
  ``cheap``      Everything else (ordinary reads). Loose.

Backend
=======
Default is **in-process** token buckets. This was a deliberate choice, not an
oversight: the compose stack was checked and there is **no Redis or valkey
service in it**. The only Redis-protocol container anywhere is FalkorDB, which
appears solely in ``docker-compose.selfhost.yml`` behind the optional
``graph`` profile and is documented there as a best-effort projection whose
absence is a no-op — depending on it for admission control would be wrong.

>>> CAVEAT: in-process limiting is PER-REPLICA. With N app replicas the
>>> effective ceiling is N x the configured limit. Today ``app`` runs a single
>>> replica so this is exact; if the deployment is ever scaled out, set
>>> AO_RATE_LIMIT_REDIS_URL (and install the optional ``redis`` extra) to get a
>>> single shared ceiling. The limiter logs a WARNING at startup stating which
>>> backend is live so this is never a silent assumption.

Configuration (all env, all optional)
=====================================
  AO_RATE_LIMIT_ENABLED           on|off. Default ON, except Community Edition
                                  (AO_EDITION=community), where it defaults OFF
                                  — a single-tenant self-host operator should
                                  never be throttled on their own hardware.
                                  An explicit value always wins over the
                                  edition default.
  AO_RATE_LIMIT_CHEAP_PER_MIN     default 300
  AO_RATE_LIMIT_WRITE_PER_MIN     default 90
  AO_RATE_LIMIT_EXPENSIVE_PER_MIN default 15
  AO_RATE_LIMIT_WEBHOOK_PER_MIN   default 600
  AO_RATE_LIMIT_TENANT_FACTOR     tenant limit = per-principal x factor.
                                  Default 4.0.
  AO_RATE_LIMIT_BURST             bucket capacity multiplier. Default 2.0
                                  (absorbs a page that fires several parallel
                                  reads without inflating sustained rate).
  AO_RATE_LIMIT_TRUST_FORWARDED   honor X-Forwarded-For for the IP fallback.
                                  Default ON (we sit behind Traefik).
  AO_RATE_LIMIT_REDIS_URL         opt-in shared backend. Unset = in-process.

``/healthz`` is never limited — throttling it would break container
healthchecks and monitoring probes, turning a load spike into a false outage
and possibly a restart loop. ``/readyz``, discovery, and OpenAPI docs are
exempt for the same reason.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_log = logging.getLogger("acctops.rate_limit")

# --------------------------------------------------------------------------
# Paths that are NEVER limited.
# --------------------------------------------------------------------------
EXEMPT_PREFIXES: tuple[str, ...] = (
    "/healthz",       # container healthcheck + monitoring probe — see docstring
    "/readyz",
    "/.well-known/",
    "/api/openapi.json",
    "/openapi.json",
    "/docs",
    "/redoc",
)

# --------------------------------------------------------------------------
# Cost tiers. First matching rule wins; unmatched falls back to method-based
# classification (mutating -> "write", else "cheap").
# --------------------------------------------------------------------------
TIER_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # --- expensive: LLM inference -----------------------------------------
    (re.compile(r"^/api/(workspaces/[^/]+/)?chat"), "expensive"),
    (re.compile(r"/chat/completions"), "expensive"),
    (re.compile(r"/(cfo-brief|tax-narrative)"), "expensive"),
    (re.compile(r"/specialist-runs?(/|$)"), "expensive"),
    # --- expensive: document / form / PDF rendering ------------------------
    (re.compile(r"/(irs-forms|forms-catalog)/"), "expensive"),
    (re.compile(r"/(pdf|render|prefill)(/|$)"), "expensive"),
    (re.compile(r"/work-products/[^/]+/(pdf|download)"), "expensive"),
    (re.compile(r"/esign(/|$)"), "expensive"),
    # --- expensive: reports + exports --------------------------------------
    (re.compile(r"/reports?(/|$)"), "expensive"),
    (re.compile(r"/export(s)?(/|$)"), "expensive"),
    (re.compile(r"/(consolidated|trial-balance|balance-sheet|cash-flow)"), "expensive"),
    (re.compile(r"/documents/[^/]+/(extract|ocr)"), "expensive"),
    # --- webhooks: unauthenticated provider callbacks, legitimately bursty --
    (re.compile(r"/webhook"), "webhook"),
    (re.compile(r"/(stripe|ebay)/(callback|webhook)"), "webhook"),
    # --- the MCP agent surface: moderate ----------------------------------
    (re.compile(r"^/mcp"), "write"),
)

_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Matches the workspace UUID in /api/workspaces/{uuid}/... . The rate limiter
# runs BEFORE routing, so request.path_params is empty and the id must be
# parsed out of the raw path.
_WORKSPACE_RE = re.compile(
    r"/workspaces/(?P<wid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _community_edition() -> bool:
    """True when running Community Edition. Never raises."""
    try:
        from app.config import get_settings

        return bool(get_settings().is_community)
    except Exception:  # noqa: BLE001
        return False


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool = True
    per_min: dict[str, float] = field(default_factory=dict)
    tenant_factor: float = 4.0
    burst: float = 2.0
    trust_forwarded: bool = True
    redis_url: str | None = None

    @classmethod
    def from_env(cls) -> RateLimitConfig:
        # Community Edition defaults OFF: a single-tenant self-hoster has no
        # neighbours to protect and should not be throttled on their own box.
        # An explicit AO_RATE_LIMIT_ENABLED always overrides the default.
        default_enabled = not _community_edition()
        return cls(
            enabled=_env_flag("AO_RATE_LIMIT_ENABLED", default_enabled),
            per_min={
                "cheap": _env_num("AO_RATE_LIMIT_CHEAP_PER_MIN", 300),
                "write": _env_num("AO_RATE_LIMIT_WRITE_PER_MIN", 90),
                "expensive": _env_num("AO_RATE_LIMIT_EXPENSIVE_PER_MIN", 15),
                "webhook": _env_num("AO_RATE_LIMIT_WEBHOOK_PER_MIN", 600),
            },
            tenant_factor=max(1.0, _env_num("AO_RATE_LIMIT_TENANT_FACTOR", 4.0)),
            burst=max(1.0, _env_num("AO_RATE_LIMIT_BURST", 2.0)),
            trust_forwarded=_env_flag("AO_RATE_LIMIT_TRUST_FORWARDED", True),
            redis_url=os.getenv("AO_RATE_LIMIT_REDIS_URL") or None,
        )

    def limit_for(self, tier: str) -> float:
        return self.per_min.get(tier, self.per_min.get("cheap", 300))


@dataclass
class Decision:
    allowed: bool
    limit: float
    remaining: float
    retry_after: int
    scope: str  # "tenant" | "principal" — which bucket rejected


# --------------------------------------------------------------------------
# Backends
# --------------------------------------------------------------------------


class InProcessTokenBuckets:
    """Async-safe token buckets in local memory.

    PER-REPLICA (see module docstring). Idle buckets are evicted so a long-lived
    process with many short-lived principals cannot grow without bound.
    """

    #: Drop buckets untouched for this long.
    IDLE_TTL_SECONDS = 900
    #: Amortized sweep: check for eviction every N admissions.
    SWEEP_EVERY = 2000

    def __init__(self) -> None:
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, ts)
        self._lock = asyncio.Lock()
        self._ops = 0

    async def consume(
        self, key: str, limit_per_min: float, burst: float
    ) -> tuple[bool, float, int]:
        """Take 1 token. Returns (allowed, remaining, retry_after_seconds)."""
        if limit_per_min <= 0:
            return True, float("inf"), 0
        rate = limit_per_min / 60.0
        capacity = max(1.0, limit_per_min * burst)
        now = time.monotonic()

        async with self._lock:
            tokens, last = self._buckets.get(key, (capacity, now))
            tokens = min(capacity, tokens + max(0.0, now - last) * rate)
            if tokens >= 1.0:
                tokens -= 1.0
                self._buckets[key] = (tokens, now)
                allowed, retry = True, 0
            else:
                self._buckets[key] = (tokens, now)
                allowed = False
                retry = max(1, math.ceil((1.0 - tokens) / rate))
            remaining = max(0.0, tokens)

            self._ops += 1
            if self._ops >= self.SWEEP_EVERY:
                self._ops = 0
                self._evict(now)

        return allowed, remaining, retry

    def _evict(self, now: float) -> None:
        cutoff = now - self.IDLE_TTL_SECONDS
        stale = [k for k, (_, ts) in self._buckets.items() if ts < cutoff]
        for k in stale:
            self._buckets.pop(k, None)
        if stale:
            _log.debug("rate_limit: evicted %d idle buckets", len(stale))

    def reset(self) -> None:
        """Drop all state. Test helper."""
        self._buckets.clear()
        self._ops = 0


class RedisTokenBuckets:
    """Token buckets shared across replicas via Redis/valkey.

    Only used when AO_RATE_LIMIT_REDIS_URL is set AND the optional ``redis``
    package is importable. The refill+consume is one atomic Lua script so
    concurrent replicas cannot double-spend a token.
    """

    _LUA = """
    local key = KEYS[1]
    local rate = tonumber(ARGV[1])
    local capacity = tonumber(ARGV[2])
    local now = tonumber(ARGV[3])
    local ttl = tonumber(ARGV[4])
    local state = redis.call('HMGET', key, 'tokens', 'ts')
    local tokens = tonumber(state[1])
    local ts = tonumber(state[2])
    if tokens == nil then tokens = capacity; ts = now end
    local delta = now - ts
    if delta < 0 then delta = 0 end
    tokens = math.min(capacity, tokens + delta * rate)
    local allowed = 0
    if tokens >= 1 then tokens = tokens - 1; allowed = 1 end
    redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
    redis.call('EXPIRE', key, ttl)
    return {allowed, tostring(tokens)}
    """

    def __init__(self, url: str) -> None:
        import redis.asyncio as aioredis  # lazy: optional dependency

        self._redis = aioredis.from_url(url, decode_responses=True)
        self._script = self._redis.register_script(self._LUA)

    async def consume(
        self, key: str, limit_per_min: float, burst: float
    ) -> tuple[bool, float, int]:
        if limit_per_min <= 0:
            return True, float("inf"), 0
        rate = limit_per_min / 60.0
        capacity = max(1.0, limit_per_min * burst)
        allowed, tokens_s = await self._script(
            keys=[f"aorl:{key}"],
            args=[rate, capacity, time.time(), 900],
        )
        tokens = float(tokens_s)
        retry = 0 if int(allowed) == 1 else max(1, math.ceil((1.0 - tokens) / rate))
        return bool(int(allowed)), max(0.0, tokens), retry


def build_backend(cfg: RateLimitConfig) -> Any:
    """Pick a backend. Falls back to in-process with a LOUD warning.

    Never raises: a misconfigured limiter must degrade to per-replica limiting,
    not take the API down at import time.
    """
    if cfg.redis_url:
        try:
            backend = RedisTokenBuckets(cfg.redis_url)
            _log.info("rate_limit: using SHARED Redis backend (cross-replica)")
            return backend
        except ImportError:
            _log.warning(
                "rate_limit: AO_RATE_LIMIT_REDIS_URL is set but the 'redis' "
                "package is not installed (pip install accounting-ops[ratelimit]). "
                "FALLING BACK to in-process buckets — limits are PER-REPLICA."
            )
        except Exception as e:  # noqa: BLE001
            _log.warning(
                "rate_limit: Redis backend init failed (%s). FALLING BACK to "
                "in-process buckets — limits are PER-REPLICA.", e,
            )
    else:
        _log.info(
            "rate_limit: using IN-PROCESS buckets — limits are PER-REPLICA. "
            "No Redis/valkey exists in the compose stack; set "
            "AO_RATE_LIMIT_REDIS_URL if the app is ever scaled beyond one replica."
        )
    return InProcessTokenBuckets()


# --------------------------------------------------------------------------
# Request classification
# --------------------------------------------------------------------------


def is_exempt(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in EXEMPT_PREFIXES)


def classify_tier(path: str, method: str) -> str:
    # The shell reads static persona metadata; it does not run an LLM.
    if path == "/api/chat/personas" and method.upper() in {"GET", "HEAD"}:
        return "cheap"
    for pattern, tier in TIER_RULES:
        if pattern.search(path):
            return tier
    return "write" if method.upper() in _MUTATING else "cheap"


def extract_workspace(path: str) -> str | None:
    m = _WORKSPACE_RE.search(path)
    return m.group("wid").lower() if m else None


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for k, v in headers:
        if k.lower() == name:
            return v.decode("latin-1")
    return None


def principal_key(
    headers: list[tuple[bytes, bytes]], client_host: str | None, trust_forwarded: bool
) -> str:
    """Stable, non-reversible identity for the caller.

    Prefers the bearer credential's SHA-256 fingerprint (works uniformly for
    Keycloak JWTs, PATs, and service tokens without paying to validate them
    here — validation still happens downstream). Falls back to API-key headers,
    then to IP for genuinely unauthenticated routes.

    The raw credential is NEVER stored or logged — only a truncated digest.
    """
    auth = _header(headers, b"authorization")
    if auth:
        token = auth.split(" ", 1)[1].strip() if " " in auth else auth.strip()
        if token:
            return "t:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:20]

    api_key = _header(headers, b"x-api-key")
    if api_key:
        return "k:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:20]

    ip = client_host or "unknown"
    if trust_forwarded:
        xff = _header(headers, b"x-forwarded-for")
        if xff:
            # Left-most entry is the original client (Traefik appends).
            ip = xff.split(",")[0].strip() or ip
    return f"i:{ip}"


# --------------------------------------------------------------------------
# Limiter
# --------------------------------------------------------------------------


class RateLimiter:
    """Tier-aware, two-bucket limiter. Framework-agnostic so it is unit-testable."""

    def __init__(self, cfg: RateLimitConfig | None = None, backend: Any = None) -> None:
        self.cfg = cfg or RateLimitConfig.from_env()
        self.backend = backend or build_backend(self.cfg)

    async def check(
        self,
        *,
        path: str,
        method: str,
        headers: list[tuple[bytes, bytes]],
        client_host: str | None = None,
    ) -> Decision | None:
        """Return None when the request is exempt or limiting is disabled."""
        if not self.cfg.enabled or is_exempt(path):
            return None

        tier = classify_tier(path, method)
        principal_limit = self.cfg.limit_for(tier)
        principal = principal_key(headers, client_host, self.cfg.trust_forwarded)
        workspace = extract_workspace(path)

        # Principal bucket first: the narrower, more common rejection, and it
        # avoids charging a tenant's shared allowance for one runaway caller.
        allowed, remaining, retry = await self.backend.consume(
            f"{tier}:p:{principal}", principal_limit, self.cfg.burst
        )
        if not allowed:
            return Decision(False, principal_limit, remaining, retry, "principal")

        if workspace:
            tenant_limit = principal_limit * self.cfg.tenant_factor
            t_allowed, t_remaining, t_retry = await self.backend.consume(
                f"{tier}:w:{workspace}", tenant_limit, self.cfg.burst
            )
            if not t_allowed:
                return Decision(False, tenant_limit, t_remaining, t_retry, "tenant")
            remaining = min(remaining, t_remaining)

        return Decision(True, principal_limit, remaining, 0, "principal")


class RateLimitMiddleware:
    """Pure-ASGI rate limiting.

    Pure ASGI rather than BaseHTTPMiddleware: this sits on every request and
    BaseHTTPMiddleware's per-request task+queue overhead is exactly what you do
    not want on a CPU-starved host. Register it LAST in ``main.py`` so Starlette
    places it OUTERMOST — a rejected request should cost as little as possible,
    ideally not even a tracing span.
    """

    def __init__(self, app: ASGIApp, limiter: RateLimiter | None = None) -> None:
        self.app = app
        self.limiter = limiter or RateLimiter()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        try:
            decision = await self.limiter.check(
                path=scope.get("path", ""),
                method=scope.get("method", "GET"),
                headers=list(scope.get("headers") or []),
                client_host=client[0] if client else None,
            )
        except Exception as e:  # noqa: BLE001
            # Fail OPEN. A broken limiter must never become an outage — the
            # thing it protects against is degraded service, not a breach.
            _log.warning("rate_limit: check failed (%s); allowing request", e)
            decision = None

        if decision is not None and not decision.allowed:
            await self._reject(scope, send, decision)
            return

        if decision is None:
            await self.app(scope, receive, send)
            return

        # Annotate successful responses with the caller's remaining budget so
        # a well-behaved client can self-throttle before it ever gets a 429.
        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"x-ratelimit-limit", str(int(decision.limit)).encode()))
                headers.append(
                    (b"x-ratelimit-remaining", str(int(decision.remaining)).encode())
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _reject(self, scope: Scope, send: Send, decision: Decision) -> None:
        import json

        path = scope.get("path", "")
        _log.warning(
            "rate_limit: 429 %s %s (scope=%s, limit=%.0f/min, retry_after=%ds)",
            scope.get("method", "?"), path, decision.scope,
            decision.limit, decision.retry_after,
        )
        body = json.dumps({
            "detail": (
                "Rate limit exceeded for this "
                f"{'workspace' if decision.scope == 'tenant' else 'client'}. "
                f"Retry in {decision.retry_after}s."
            ),
            "scope": decision.scope,
            "limit_per_minute": int(decision.limit),
            "retry_after_seconds": decision.retry_after,
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 429,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"retry-after", str(decision.retry_after).encode()),
                (b"x-ratelimit-limit", str(int(decision.limit)).encode()),
                (b"x-ratelimit-remaining", b"0"),
                (b"x-ratelimit-scope", decision.scope.encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
