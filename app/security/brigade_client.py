"""Brigade outbound client — consumes the aggregated cross-app tool catalog.

Accounting-Ops is both PRODUCER and CONSUMER of Brigade-federated tools:

- Producer: Brigade ingests our ``/.well-known/mcps.json`` and exposes our
  ``account.*`` synthesis tools to other Brigade clients.
- Consumer (this module): we fetch Brigade's aggregated catalog at
  ``${BRIGADE_BASE_URL}/.well-known/manifest.json`` so the in-app ``/chat``
  surface can invoke ``project.*``, ``meeting.*``, ``contact.*`` tools on
  the user's behalf.

Lifecycle:

- ``init_brigade_client()`` is called at FastAPI startup (in
  ``app/main.py`` lifespan). It fires a discovery request, caches the
  catalog in memory, and schedules periodic re-discovery.
- ``get_cross_app_catalog()`` returns the current catalog (empty when
  Brigade is offline or feature is OFF).
- ``invoke_cross_app_tool()`` posts to the federated MCP endpoint with
  the caller's Brigade-issued bearer (must carry ``brigade:cross_app``).

When ``BRIGADE_BASE_URL`` is unset or the catalog fetch fails, the client
degrades cleanly:

- ``get_cross_app_catalog()`` returns ``CatalogSnapshot(tools=(), online=False, reason=...)``.
- ``invoke_cross_app_tool()`` raises ``BrigadeUnavailableError``.
- Periodic re-discovery keeps trying with exponential backoff, so when
  commander wires up the route Brigade auto-populates without restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

_log = logging.getLogger("acctops.brigade.client")

_BRIGADE_BASE_URL_ENV = "BRIGADE_BASE_URL"

# The token scope name granted by Brigade (with user consent) that authorizes
# cross-app reads. Without it on the bearer, the CFO Agent stays in
# account.* (local) tools regardless of whether Brigade's catalog is online.
_CROSS_APP_SCOPE = "brigade:cross_app"


def brigade_base_url() -> str | None:
    """Effective Brigade base URL or None when feature is OFF.

    Mirrors brigade_jwt.py's resolution (BRIGADE_TRUSTED_ISSUER is the
    canonical issuer; BRIGADE_BASE_URL is the discovery/invoke root).
    Falls back to the trusted issuer if BRIGADE_BASE_URL is unset but
    BRIGADE_TRUSTED_ISSUER is explicitly set in env — convenient when
    both happen to be the same host (the common case).
    """
    raw = os.getenv(_BRIGADE_BASE_URL_ENV, "").strip()
    if raw:
        return raw
    issuer = os.getenv("BRIGADE_TRUSTED_ISSUER", "").strip()
    return issuer or None


def is_brigade_enabled() -> bool:
    """True when BRIGADE_BASE_URL (or fallback BRIGADE_TRUSTED_ISSUER) is set."""
    return brigade_base_url() is not None


def has_cross_app_scope(scopes: tuple[str, ...]) -> bool:
    """True iff the bearer carries the ``brigade:cross_app`` scope."""
    return _CROSS_APP_SCOPE in scopes

_BRIGADE_DISCOVERY_TIMEOUT_S = 5.0
_BRIGADE_INVOKE_TIMEOUT_S = 30.0
_BRIGADE_DISCOVERY_INTERVAL_S = 300  # 5 min when up
_BRIGADE_DISCOVERY_BACKOFF_MAX_S = 1800  # 30 min when down


@dataclass(frozen=True)
class CrossAppTool:
    """One tool exposed via Brigade federation."""

    qualified_name: str  # "project.list_budgets"
    source_app: str      # "project-ops"
    description: str
    input_schema: dict[str, Any]
    invoke_url: str      # absolute URL the client POSTs to


@dataclass(frozen=True)
class CatalogSnapshot:
    """Snapshot of the Brigade-aggregated cross-app catalog."""

    tools: tuple[CrossAppTool, ...]
    online: bool
    reason: str  # "ok" / "feature_off" / "fetch_failed: ..." / etc.
    fetched_at: float = 0.0


class BrigadeUnavailableError(RuntimeError):
    """Raised when invoke_cross_app_tool is called and Brigade is offline."""


# ---------------------------------------------------------------------------
# Client state — initialized at FastAPI startup, cleared at shutdown.
# ---------------------------------------------------------------------------

_state: dict[str, Any] = {
    "snapshot": CatalogSnapshot(tools=(), online=False, reason="not_initialized"),
    "task": None,
    "running": False,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_cross_app_catalog() -> CatalogSnapshot:
    """Current cached cross-app catalog (or an offline-stub snapshot)."""
    return _state["snapshot"]


def get_cross_app_tools_for(scopes: tuple[str, ...]) -> tuple[CrossAppTool, ...]:
    """Filter the catalog by the caller's scopes.

    The CFO Agent (and any persona with ``consumes_brigade=True``) can call
    cross-app tools ONLY when the bearer carries ``brigade:cross_app``.
    """
    snap = _state["snapshot"]
    if not snap.online or not has_cross_app_scope(scopes):
        return ()
    return snap.tools


async def init_brigade_client() -> None:
    """Fire the first discovery + start the background refresh loop.

    Safe no-op when Brigade is OFF. Idempotent.
    """
    if _state["running"]:
        return
    _state["running"] = True
    if not is_brigade_enabled():
        _state["snapshot"] = CatalogSnapshot(
            tools=(),
            online=False,
            reason="feature_off (BRIGADE_BASE_URL unset)",
            fetched_at=time.time(),
        )
        _log.info("Brigade unavailable — local tools only (BRIGADE_BASE_URL unset)")
        return
    # First fetch synchronously so the catalog is hot before the first request.
    await _refresh_catalog_once()
    # Then keep refreshing in the background.
    _state["task"] = asyncio.create_task(_refresh_loop())


async def shutdown_brigade_client() -> None:
    """Cancel the refresh loop. Idempotent."""
    task: asyncio.Task | None = _state.get("task")
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    _state["task"] = None
    _state["running"] = False


async def invoke_cross_app_tool(
    qualified_name: str,
    arguments: dict[str, Any],
    bearer_token: str,
    scopes: tuple[str, ...],
) -> dict[str, Any]:
    """Invoke a Brigade-federated tool on behalf of the caller.

    ``bearer_token`` must be a Brigade-issued token carrying the
    ``brigade:cross_app`` scope. We forward it verbatim to the federated
    MCP endpoint; Brigade authorizes the cross-app call.

    Raises ``BrigadeUnavailableError`` when Brigade is offline / feature
    is off / tool is not in the catalog / caller lacks scope.
    """
    if not is_brigade_enabled():
        raise BrigadeUnavailableError("Brigade feature is OFF (BRIGADE_BASE_URL unset)")
    snap = _state["snapshot"]
    if not snap.online:
        raise BrigadeUnavailableError(f"Brigade catalog offline: {snap.reason}")
    if not has_cross_app_scope(scopes):
        raise BrigadeUnavailableError(
            "Caller lacks 'brigade:cross_app' scope on bearer token"
        )

    tool = next((t for t in snap.tools if t.qualified_name == qualified_name), None)
    if tool is None:
        raise BrigadeUnavailableError(f"Unknown cross-app tool: {qualified_name!r}")

    async with httpx.AsyncClient(timeout=_BRIGADE_INVOKE_TIMEOUT_S) as client:
        response = await client.post(
            tool.invoke_url,
            json={"arguments": arguments},
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "User-Agent": "accounting-ops/0.1 brigade-consumer",
            },
        )
        response.raise_for_status()
        return response.json()


# ---------------------------------------------------------------------------
# Internal: refresh loop + parsing
# ---------------------------------------------------------------------------


async def _refresh_loop() -> None:
    """Background loop that re-fetches the catalog on a fixed-or-backoff cadence."""
    backoff = _BRIGADE_DISCOVERY_INTERVAL_S
    while True:
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            return
        ok = await _refresh_catalog_once()
        backoff = (
            _BRIGADE_DISCOVERY_INTERVAL_S
            if ok
            else min(int(backoff * 1.5), _BRIGADE_DISCOVERY_BACKOFF_MAX_S)
        )


async def _refresh_catalog_once() -> bool:
    """Fetch the Brigade catalog once. Return True on success."""
    base = brigade_base_url()
    if base is None:
        # Feature became OFF mid-loop (env mutated). Reflect it.
        _state["snapshot"] = CatalogSnapshot(
            tools=(), online=False, reason="feature_off", fetched_at=time.time(),
        )
        return False
    url = f"{base.rstrip('/')}/.well-known/manifest.json"
    try:
        async with httpx.AsyncClient(
            timeout=_BRIGADE_DISCOVERY_TIMEOUT_S,
            headers={"User-Agent": "accounting-ops/0.1 brigade-discovery"},
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        reason = f"fetch_failed: {exc.__class__.__name__}: {exc}"
        _log.info("Brigade catalog fetch failed: %s", reason)
        _state["snapshot"] = CatalogSnapshot(
            tools=(), online=False, reason=reason, fetched_at=time.time(),
        )
        return False

    tools = _parse_brigade_manifest(payload)
    _state["snapshot"] = CatalogSnapshot(
        tools=tools,
        online=True,
        reason="ok",
        fetched_at=time.time(),
    )
    _log.info(
        "Brigade catalog refreshed: %d cross-app tools from %d apps",
        len(tools),
        len({t.source_app for t in tools}),
    )
    return True


def _parse_brigade_manifest(payload: Any) -> tuple[CrossAppTool, ...]:
    """Parse Brigade's aggregated manifest into a flat tuple of CrossAppTool.

    Expected shape (matches what Brigade is documented to publish):

        {
          "apps": [
            {
              "id": "project-ops",
              "mcp_endpoint": "https://project-ops.../mcp",
              "tools": [
                {"name": "list_budgets", "description": "...", "input_schema": {...}},
                ...
              ]
            },
            ...
          ]
        }

    Tools are returned as ``<app_short>.<tool_name>`` (e.g.
    ``project.list_budgets``). We drop our own app's tools — we are the source
    of those, no need to round-trip.
    """
    if not isinstance(payload, dict):
        return ()
    apps = payload.get("apps")
    if not isinstance(apps, list):
        return ()
    own_id = os.getenv("ACCTOPS_APP_ID", "accounting-ops")
    out: list[CrossAppTool] = []
    for app in apps:
        if not isinstance(app, dict):
            continue
        app_id = str(app.get("id") or "").strip()
        if not app_id or app_id == own_id:
            continue
        short = app_id.replace("-ops", "").replace("-app", "")
        mcp_endpoint = str(app.get("mcp_endpoint") or "").strip()
        tools = app.get("tools")
        if not isinstance(tools, list) or not mcp_endpoint:
            continue
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            qualified = f"{short}.{name}"
            out.append(
                CrossAppTool(
                    qualified_name=qualified,
                    source_app=app_id,
                    description=str(tool.get("description") or ""),
                    input_schema=tool.get("input_schema") or {"type": "object"},
                    invoke_url=f"{mcp_endpoint.rstrip('/')}/tools/{name}",
                )
            )
    return tuple(out)
