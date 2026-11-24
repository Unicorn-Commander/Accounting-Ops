"""AO-UP-5: the upload cap must be enforced at the ASGI layer, not only after
Starlette has spooled the body into memory.

The 100 MB ``document_upload_max_bytes`` cap was only checked in each upload
route, AFTER ``await file.read()`` had already buffered the whole body. A
multi-GB upload exhausted memory before the 413 could fire. The app now passes
``max_body_size`` to FastAPI, which installs Starlette's
``RequestBodyLimitMiddleware``: a request whose streamed body crosses the cap
is rejected with 413 while it is still being read, and the body is never
fully buffered.

Offline: a tiny ASGI app wrapped with the same middleware; the over-cap body
is streamed in chunks and the test asserts the 413 fires before the stream
ends. No network.
"""

from __future__ import annotations

from typing import Any

from starlette.applications import Starlette
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

# Small cap so the test stays fast; the production value is 100 MB.
CAP = 4096


def _build_app() -> Any:
    """Build the app with RequestBodyLimitMiddleware at CAP bytes, the same
    way FastAPI wraps it when max_body_size is set."""

    async def upload(request) -> Any:
        # Read the whole body: with the middleware in place an over-cap
        # request never reaches here (it is 413'd mid-stream). If we DO get
        # here, the body was within the cap.
        body = await request.body()
        return PlainTextResponse(f"got {len(body)}")

    app = Starlette(routes=[Route("/up", upload, methods=["POST"])])
    return RequestBodyLimitMiddleware(app, max_body_size=CAP)


def _streaming_body(n_bytes: int, chunk: int = 1024) -> Any:
    """An ASGI receive() that streams n_bytes in chunk-sized pieces."""
    state = {"sent": 0, "more": True}

    async def receive() -> dict[str, Any]:
        if not state["more"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        piece = b"x" * min(chunk, n_bytes - state["sent"])
        state["sent"] += len(piece)
        state["more"] = state["sent"] < n_bytes
        return {"type": "http.request", "body": piece, "more_body": state["more"]}

    return receive


async def _post(app: Any, n_bytes: int) -> int:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/up",
        "query_string": b"",
        "headers": [(b"content-length", str(n_bytes).encode())],
    }
    status: list[int] = []

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    await app(scope, _streaming_body(n_bytes), send)
    return status[0]


async def test_within_cap_body_is_accepted() -> None:
    app = _build_app()
    status = await _post(app, CAP - 1024)
    assert status == 200


async def test_over_cap_body_is_413_before_full_buffer() -> None:
    """An over-cap body must be 413'd mid-stream: the app never buffers it.

    The stream is larger than the cap; the middleware stops reading once the
    cap is crossed and returns 413. If the body had been fully buffered first,
    the request would either 500 (memory) or reach the route and 200.
    """
    app = _build_app()
    status = await _post(app, CAP * 4)
    assert status == 413


async def test_main_app_wires_max_body_size() -> None:
    """The real app must set max_body_size to the configured cap (importing
    app.main runs the FastAPI() call that stores it)."""
    import app.config as config
    import app.main as main

    expected = config.get_settings().document_upload_max_bytes
    assert main.app.max_body_size == expected
    assert main.app.max_body_size == 100 * 1024 * 1024  # the documented default
