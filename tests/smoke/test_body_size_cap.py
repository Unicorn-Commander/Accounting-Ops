"""AO-UP-5: the upload cap must be enforced at the ASGI layer, not only after
Starlette has spooled the body into memory.

The 100 MB ``document_upload_max_bytes`` cap was only checked in each upload
route, AFTER ``await file.read()`` had already buffered the whole body. A
multi-GB upload exhausted memory before the 413 could fire. The FastAPI
constructor kwarg that was supposed to fix it (``max_body_size``) is swallowed
as an **extra by the locked pins (fastapi 0.136.3 / starlette 1.3.1): nothing
is enforced. The app now wraps itself with ``BodySizeCapMiddleware`` (a pure
ASGI middleware that counts streamed body bytes) registered on ``app.main.app``.

Offline: real ASGI calls through the real ``app.main.app`` with a body-reading
route mounted on it (importing app.main is safe: no network at import time);
over-cap bodies are streamed in chunks and the test asserts the 413 fires
before the handler ever runs. No network.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import PlainTextResponse

# A scratch route that READS the whole body, mounted on app.main.app for the
# duration of each test (and removed afterwards). It stands in for the upload
# routes, which read the file body the same way.
_PROBE_PATH = "/__body_cap_probe"


def _build_scope(path: str, headers: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
    }


def _streaming_body(n_bytes: int, chunk: int = 1024) -> Any:
    """An ASGI receive() that streams n_bytes in chunk-sized pieces."""
    state = {"sent": 0}

    async def receive() -> dict[str, Any]:
        if state["sent"] >= n_bytes:
            return {"type": "http.request", "body": b"", "more_body": False}
        piece = b"x" * min(chunk, n_bytes - state["sent"])
        state["sent"] += len(piece)
        return {
            "type": "http.request",
            "body": piece,
            "more_body": state["sent"] < n_bytes,
        }

    return receive


def _mount_probe() -> None:
    """Mount the body-reading probe route on the real app for the test."""
    import app.main as main

    _PROBE_STATE.update({"ran": False, "bytes": 0})

    async def probe(request: Request) -> PlainTextResponse:
        body = await request.body()
        _PROBE_STATE["ran"] = True
        _PROBE_STATE["bytes"] = len(body)
        return PlainTextResponse(f"got {len(body)}")

    main.app.add_api_route(_PROBE_PATH, probe, methods=["POST"])
    _PROBE_CLEANUP.append(lambda: main.app.router.routes.remove(
        next(r for r in main.app.router.routes if getattr(r, "path", None) == _PROBE_PATH)
    ))


def _unmount_probe() -> None:
    while _PROBE_CLEANUP:
        _PROBE_CLEANUP.pop()()


_PROBE_CLEANUP: list[Any] = []
_PROBE_STATE: dict[str, Any] = {"ran": False, "bytes": 0}


async def _post(
    body_bytes: int, *, chunked: bool = False, chunk: int = 1024
) -> tuple[list[int], bool, int, dict[bytes, bytes]]:
    """POST ``body_bytes`` to the probe on app.main.app.

    Returns (statuses, handler_ran, bytes_buffered, response_headers).
    ``statuses`` is the list of every http.response.start seen and
    ``response_headers`` is the header map of the LAST response.start (the one
    the client ends up seeing).
    """
    import app.main as main

    headers: list[tuple[bytes, bytes]] = []
    if not chunked:
        headers.append((b"content-length", str(body_bytes).encode("ascii")))

    status: list[int] = []
    last_headers: dict[bytes, bytes] = {}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])
            last_headers.clear()
            for name, value in message.get("headers") or []:
                last_headers[name.lower()] = value

    import asyncio

    try:
        await asyncio.wait_for(
            main.app(_build_scope(_PROBE_PATH, headers), _streaming_body(body_bytes, chunk), send),
            timeout=60,
        )
    except asyncio.TimeoutError:
        raise AssertionError("app call hung: the cap middleware must abort the app, not deadlock it")
    return status, _PROBE_STATE["ran"], _PROBE_STATE["bytes"], last_headers


async def test_over_cap_with_content_length_is_413_and_handler_never_runs() -> None:
    """A body whose declared Content-Length exceeds the cap is 413'd before any
    body is read; the handler never runs and nothing is buffered."""
    import app.config as config

    _mount_probe()
    try:
        cap = config.get_settings().document_upload_max_bytes
        statuses, ran, buffered, _hdrs = await _post(cap + 16, chunked=False)
    finally:
        _unmount_probe()
    assert 413 in statuses
    assert ran is False, "the handler must never run for an over-cap body"
    assert buffered == 0, "no body may be buffered before the 413"


async def test_over_cap_chunked_body_is_413_and_handler_never_runs() -> None:
    """A chunked (no Content-Length) body that crosses the cap mid-stream is
    413'd by the middleware, and the handler NEVER runs: the middleware
    aborts the app (later receives report disconnect, then raise) instead of
    delivering a truncated body, so no handler side effects happen on partial
    data."""
    import app.config as config

    _mount_probe()
    try:
        cap = config.get_settings().document_upload_max_bytes
        # Stream well past the cap in small chunks: the cap must fire
        # mid-stream, not after the whole (huge) body was read.
        statuses, ran, buffered, hdrs = await _post(cap + 4096, chunked=True, chunk=1024)
    finally:
        _unmount_probe()
    # Exactly one response start: the 413. The handler's 200 never goes out.
    assert statuses == [413], f"expected only the 413, got {statuses}"
    assert ran is False, "the handler must never run on a truncated body"
    assert buffered == 0, "the handler must not read any of the over-cap body"
    # R3-3: the 413 carries the global security headers (SecurityHeaders wraps
    # the cap middleware, so it stamps the 413 like any other response).
    assert hdrs.get(b"x-content-type-options") == b"nosniff"
    assert b"frame-ancestors" in (hdrs.get(b"content-security-policy") or b"")
    assert hdrs.get(b"x-frame-options") == b"SAMEORIGIN"


def _cap_app(max_bytes: int, probe_state: dict[str, Any]) -> Any:
    """A dedicated ASGI app so the cap is exactly ``max_bytes`` regardless of
    the configured document_upload_max_bytes. The probe records that it ran
    (the handler's side effect) in ``state``."""
    from app.body_size_cap import BodySizeCapMiddleware
    from starlette.applications import Starlette
    from starlette.routing import Route

    async def probe(request: Request) -> PlainTextResponse:
        body = await request.body()
        probe_state["ran"] = True
        probe_state["bytes"] = len(body)
        return PlainTextResponse(f"got {len(body)}")

    app = Starlette(routes=[Route(_PROBE_PATH, probe, methods=["POST"])])
    return BodySizeCapMiddleware(app, max_bytes=max_bytes)


async def _post_cap_app(
    app: Any, probe_state: dict[str, Any], body_bytes: int, *, chunk: int
) -> list[int]:
    """POST ``body_bytes`` (chunked) to the dedicated cap app.

    Returns the list of response.start statuses seen; the handler's side
    effect is recorded in ``probe_state``.
    """
    status: list[int] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    import asyncio

    try:
        await asyncio.wait_for(
            app(_build_scope(_PROBE_PATH, []), _streaming_body(body_bytes, chunk), send),
            timeout=60,
        )
    except asyncio.TimeoutError:
        raise AssertionError("app call hung: the cap middleware must abort the app, not deadlock it")
    return status


async def test_chunked_5x50_with_cap_100_is_413_and_handler_never_runs() -> None:
    """The exact R3-1 shape: chunked 5 x 50 bytes with a cap of 100. The
    running total crosses 100 on the 3rd chunk (150 > 100); the middleware
    413s and aborts the app, and the handler's ``ran`` flag stays False."""
    state: dict[str, Any] = {"ran": False, "bytes": 0}
    app = _cap_app(100, state)
    status = await _post_cap_app(app, state, 250, chunk=50)
    assert status == [413], f"expected only the 413, got {status}"
    assert state["ran"] is False, "the handler must never run on a truncated body"


async def test_chunked_11_bytes_with_cap_10_is_413_and_handler_never_runs() -> None:
    """R5-P2-1: a SINGLE 11-byte chunk at a cap of 10 crosses on the first
    read. The middleware must 413 and the handler must never run: the crossing
    chunk is never returned to the app, so no side effect happens on it."""
    state: dict[str, Any] = {"ran": False, "bytes": 0}
    app = _cap_app(10, state)
    status = await _post_cap_app(app, state, 11, chunk=11)
    assert status == [413], f"expected only the 413, got {status}"
    assert state["ran"] is False, "the handler must never run on the crossing chunk"
    assert state["bytes"] == 0, "no body may be buffered before the 413"


async def test_chunked_5_plus_6_with_cap_10_is_413_and_handler_never_runs() -> None:
    """R5-P2-1: chunks of 5 and 6 bytes at a cap of 10. The first chunk (5) is
    under the cap and the second (total 11) crosses it. The crossing chunk must
    never reach the app: 413, and the handler never ran (its side effect did
    not happen)."""
    state: dict[str, Any] = {"ran": False, "bytes": 0}
    app = _cap_app(10, state)
    status = await _post_cap_app(app, state, 11, chunk=5)
    assert status == [413], f"expected only the 413, got {status}"
    assert state["ran"] is False, "the handler must never run on the crossing chunk"
    assert state["bytes"] == 0, "no body may be buffered before the 413"


async def test_under_cap_body_passes_to_handler() -> None:
    """A body within the cap reaches the handler and is read in full."""
    _mount_probe()
    try:
        statuses, ran, buffered, _hdrs = await _post(4096, chunked=False)
    finally:
        _unmount_probe()
    assert statuses == [200]
    assert ran is True
    assert buffered == 4096


async def test_app_wires_body_size_cap_middleware() -> None:
    """The real app carries the cap middleware with the configured value."""
    import app.config as config
    import app.main as main
    from app.body_size_cap import BodySizeCapMiddleware

    cap = config.get_settings().document_upload_max_bytes
    assert cap == 100 * 1024 * 1024  # the documented default
    names = [m.cls.__name__ for m in main.app.user_middleware]
    assert "BodySizeCapMiddleware" in names, (
        f"BodySizeCapMiddleware is not registered on app.main.app (got {names})"
    )
    # The middleware instance on the app must hold the configured cap.
    instance = next(m for m in main.app.user_middleware if m.cls is BodySizeCapMiddleware)
    assert instance.kwargs.get("max_bytes") == cap


async def test_413_carries_security_headers() -> None:
    """R3-3: the 413 the cap middleware emits must carry the global security
    headers (nosniff + CSP + frame-ancestors). SecurityHeadersMiddleware wraps
    BodySizeCapMiddleware (registered after it, hence outermost), so it stamps
    the 413 exactly like any other response."""
    import app.config as config

    _mount_probe()
    try:
        cap = config.get_settings().document_upload_max_bytes
        statuses, _ran, _buffered, hdrs = await _post(cap + 16, chunked=False)
    finally:
        _unmount_probe()
    assert statuses == [413]
    assert hdrs.get(b"x-content-type-options") == b"nosniff"
    csp = hdrs.get(b"content-security-policy") or b""
    assert b"frame-ancestors" in csp, f"413 CSP missing frame-ancestors: {csp!r}"
    assert b"default-src 'self'" in csp
    assert hdrs.get(b"x-frame-options") == b"SAMEORIGIN"
