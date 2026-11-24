"""ASGI body-size cap middleware (AO-UP-5).

The upload cap (``document_upload_max_bytes``) used to be checked only in the
upload routes, AFTER Starlette had already spooled the whole body into memory:
a multi-GB upload exhausted memory before the 413 could fire. The FastAPI
constructor's ``max_body_size`` kwarg was the intended fix, but on the locked
pins (fastapi 0.136.3 / starlette 1.3.1, installed ``--frozen`` from uv.lock)
FastAPI swallows it as an **extra and installs nothing: a 1000-byte POST to
``FastAPI(max_body_size=10)`` returns 200. So the cap is enforced here, in a
pure-ASGI middleware that wraps both ``receive`` and ``send``:

* a declared ``Content-Length`` over the cap is rejected BEFORE the downstream
  app runs (fail fast, no body read);
* otherwise the running total of streamed body bytes is counted as the
  downstream app reads them, and the moment it exceeds the cap the middleware
  stops the app's response and returns 413 instead -- the client never sees the
  handler's (partial) response, and the body is not read past the cap.

Registered on ``app.main.app`` (see app/main.py); the cap shares the same env
var the web edge reads (client_max_body_size), so edge + app stay in lockstep.
"""

from __future__ import annotations

from typing import Any

_CAP_BODY = b"Request body exceeds the maximum allowed size."


async def _send_413(send: Any) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(_CAP_BODY)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _CAP_BODY, "more_body": False})


def _declared_length(scope: dict[str, Any]) -> int | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == b"content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


class BodySizeCapMiddleware:
    """Reject request bodies over ``max_bytes`` with 413, before they buffer.

    Counts the streamed ``http.request`` body bytes as the downstream app reads
    them. A declared ``Content-Length`` over the cap is rejected up front,
    before the app runs. For a body without a declared length (chunked), the
    cap is detected mid-read; the app's response (if any) is discarded and a
    413 is sent instead, so the client never sees a partial success.
    """

    def __init__(self, app: Any, *, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = int(max_bytes)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        content_length = _declared_length(scope)
        if content_length is not None and content_length > self._max_bytes:
            # Fail fast: reject before the app runs, then drain the declared
            # body (best effort) so the server can close the connection cleanly.
            await _send_413(send)
            remaining = content_length
            while remaining > 0:
                message = await receive()
                if message["type"] != "http.request":
                    break
                remaining -= len(message.get("body", b"") or b"")
            return

        state = {"total": 0, "exceeded": False, "response_started": False}

        async def capped_receive() -> dict[str, Any]:
            message = await receive()
            if message["type"] != "http.request" or state["exceeded"]:
                return message
            state["total"] += len(message.get("body", b"") or b"")
            if state["total"] > self._max_bytes:
                state["exceeded"] = True
                # Tell the app the body ended early so it stops reading.
                return {"type": "http.request", "body": b"", "more_body": False}
            return message

        async def capped_send(message: dict[str, Any]) -> None:
            if state["exceeded"]:
                # The body crossed the cap: discard whatever response the app
                # is (or was) sending and substitute the 413 exactly once.
                if not state["response_started"]:
                    state["response_started"] = True
                    await _send_413(send)
                return
            await send(message)

        await self._app(scope, capped_receive, capped_send)
        if state["exceeded"] and not state["response_started"]:
            # The app never sent a response (e.g. it raised, or it read no body
            # at all after we cut the stream): send the 413 ourselves.
            state["response_started"] = True
            await _send_413(send)
